from __future__ import annotations
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# These imports match your existing project layout
from utils.db import Balance  # type: ignore
from utils.common import ensure_user  # type: ignore


# ------------------------------
# Utility: DB helpers (field-agnostic)
# ------------------------------

# Balance model field detection (supports: amount, balance, credits, coins, value)
_BAL_FIELDS = None
def _detect_balance_field(obj: Balance) -> str:
    global _BAL_FIELDS
    if _BAL_FIELDS:
        return _BAL_FIELDS
    for k in ("amount", "balance", "credits", "coins", "value"):
        if hasattr(obj, k):
            _BAL_FIELDS = k
            return k
    # fallback
    _BAL_FIELDS = "amount"
    return _BAL_FIELDS

def _get_balance(session, user_id: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        return 0
    fld = _detect_balance_field(bal)
    return int(getattr(bal, fld, 0))

def _set_balance(session, user_id: int, new_value: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        bal = Balance(user_id=user_id)
        session.add(bal)
        session.flush()
    fld = _detect_balance_field(bal)
    setattr(bal, fld, int(new_value))
    session.commit()
    return int(getattr(bal, fld))

def _add_balance(session, user_id: int, delta: int) -> int:
    cur = _get_balance(session, user_id)
    return _set_balance(session, user_id, cur + int(delta))

def _can_afford(session, user_id: int, amount: int) -> bool:
    return _get_balance(session, user_id) >= amount


# ------------------------------
# Game data structures
# ------------------------------

HORSE_EMOJIS = ["🐎", "🐴", "🏇", "🦄"]
TRACK_ICON = "—"
FINISH_FLAG = "🏁"

@dataclass
class Racer:
    user_id: int               # -1 for AI
    display: str               # mention or CPU name
    horse: str
    bet: int = 0               # 0 for AI
    lane: int = 0
    progress: int = 0
    is_ai: bool = False


@dataclass
class RaceState:
    guild_id: int
    channel_id: int
    author_id: int
    bet: int
    lobby_seconds: int
    track_len: int
    started: bool = False
    message_id: Optional[int] = None
    racers: Dict[int, Racer] = field(default_factory=dict)  # user_id -> Racer (human only before start)
    join_view: Optional[discord.ui.View] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancelled: bool = False
    resolved: bool = False
    total_lanes: int = 10

    @property
    def pot(self) -> int:
        # Only human bets count toward the pot
        return sum(r.bet for r in self.racers.values() if not r.is_ai)

    def render_lobby_embed(self, bot: commands.Bot, remaining: int) -> discord.Embed:
        e = discord.Embed(
            title="🏇 Horse Race — Lobby",
            description=(
                f"Bet per racer: **{self.bet:,}** credits\n"
                f"Pot so far: **{self.pot:,}**\n"
                f"Lanes: **{self.total_lanes}** (AI will fill empty lanes)\n"
                f"Time left to join: **{remaining}s**"
            ),
            color=discord.Color.gold(),
        )
        if self.racers:
            lines = []
            for r in self.racers.values():
                lines.append(f"{r.horse} **{r.display}** — bet **{r.bet:,}**")
            e.add_field(name="Racers", value="\n".join(lines), inline=False)
        else:
            e.add_field(name="Racers", value="(none yet)", inline=False)
        e.set_footer(text="Click Join to enter, Leave to withdraw. Host can start early or cancel.")
        return e

    def render_track(self, all_racers: List[Racer]) -> Tuple[str, List[str]]:
        lanes = []
        order = []
        for r in all_racers:
            # lane like: 🐎——— (horse moves) ———🏁
            left = TRACK_ICON * r.progress
            right = TRACK_ICON * (self.track_len - r.progress)
            lane = f"{left}{r.horse}{right}{FINISH_FLAG}"
            lanes.append(lane)
            order.append((r.progress, r.display))
        return "\n".join(lanes), [name for _p, name in sorted(order, key=lambda x: -x[0])]

    def render_race_embed(self, tick: int, all_racers: List[Racer]) -> discord.Embed:
        track_str, leaders = self.render_track(all_racers)
        leader_list = ", ".join(leaders[:3])
        e = discord.Embed(
            title=f"🏇 Horse Race — Lap {tick}",
            description=f"First to {self.track_len} wins. Pot: **{self.pot:,}**",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Track", value=f"```\n{track_str}\n```", inline=False)
        if leader_list:
            e.add_field(name="Leaders", value=leader_list, inline=False)
        e.set_footer(text="Who will cross the flag first?!")
        return e


# ------------------------------
# UI Views
# ------------------------------

class LobbyView(discord.ui.View):
    def __init__(self, cog: 'HorseRace', state: RaceState, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.channel and interaction.channel.id == self.state.channel_id

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="✅")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.state.lock:
            if self.state.started:
                return await interaction.response.send_message("Race already started.", ephemeral=True)
            uid = interaction.user.id
            if uid in self.state.racers:
                return await interaction.response.send_message("You're already in!", ephemeral=True)

            # Balance check & debit
            session = self.cog.bot.SessionLocal()
            try:
                ensure_user(session, uid)
                if not _can_afford(session, uid, self.state.bet):
                    return await interaction.response.send_message(
                        f"You need **{self.state.bet:,}** credits to join.", ephemeral=True
                    )
                _add_balance(session, uid, -self.state.bet)
            finally:
                session.close()

            racer = Racer(
                user_id=uid,
                display=str(interaction.user.display_name),
                horse=random.choice(HORSE_EMOJIS),
                bet=self.state.bet,
                is_ai=False,
            )
            self.state.racers[uid] = racer
            remaining = max(0, int(self.timeout or 0))
            emb = self.state.render_lobby_embed(self.cog.bot, remaining)
            await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.state.lock:
            uid = interaction.user.id
            if uid not in self.state.racers:
                return await interaction.response.send_message("You're not in.", ephemeral=True)
            if self.state.started:
                return await interaction.response.send_message("Too late—race already started!", ephemeral=True)

            bet = self.state.racers[uid].bet
            del self.state.racers[uid]

            # Refund
            session = self.cog.bot.SessionLocal()
            try:
                _add_balance(session, uid, bet)
            finally:
                session.close()

            remaining = max(0, int(self.timeout or 0))
            emb = self.state.render_lobby_embed(self.cog.bot, remaining)
            await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Start Now", style=discord.ButtonStyle.primary, emoji="🚦")
    async def start_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.author_id:
            return await interaction.response.send_message("Only the host can start early.", ephemeral=True)
        async with self.state.lock:
            self.state.started = True
        await interaction.response.defer()
        self.stop()  # end lobby early

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.author_id:
            return await interaction.response.send_message("Only the host can cancel.", ephemeral=True)
        async with self.state.lock:
            self.state.cancelled = True
        await interaction.response.defer()
        self.stop()


class SoloChoiceView(discord.ui.View):
    """Shown when only 1 human joins: choose solo double-or-nothing OR race vs AI."""
    def __init__(self, *, timeout: float = 25.0):
        super().__init__(timeout=timeout)
        self.choice: Optional[str] = None  # "solo" | "ai" | "cancel"

    @discord.ui.button(label="Start Race vs AI (Normal)", style=discord.ButtonStyle.primary, emoji="🤖")
    async def vs_ai(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "ai"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Continue Solo (Double-or-Nothing)", style=discord.ButtonStyle.success, emoji="💥")
    async def solo(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "solo"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel & Refund", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "cancel"
        await interaction.response.defer()
        self.stop()


# ------------------------------
# Cog
# ------------------------------

class HorseRace(commands.Cog):
    """Interactive horse-race betting game with AI lanes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_by_channel: Dict[int, RaceState] = {}

    def _guard_channel_free(self, channel_id: int) -> bool:
        state = self.active_by_channel.get(channel_id)
        return not state or state.resolved or state.cancelled

    @app_commands.command(name="horserace", description="Start a horse race with betting. AI fills empty lanes (up to 10).")
    @app_commands.describe(bet="Bet per player (credits)", lobby_seconds="How long to wait for racers", track_len="Track length (10-40)")
    async def horserace(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, 1_000_000],
        lobby_seconds: app_commands.Range[int, 10, 120] = 30,
        track_len: app_commands.Range[int, 10, 40] = 22,
    ):
        if not interaction.channel:
            return await interaction.response.send_message("Run this in a server channel.", ephemeral=True)

        channel_id = interaction.channel.id
        if not self._guard_channel_free(channel_id):
            return await interaction.response.send_message("There is already a race in this channel. Please wait.", ephemeral=True)

        session = self.bot.SessionLocal()
        try:
            ensure_user(session, interaction.user.id)
        finally:
            session.close()

        state = RaceState(
            guild_id=interaction.guild_id or 0,
            channel_id=channel_id,
            author_id=interaction.user.id,
            bet=bet,
            lobby_seconds=lobby_seconds,
            track_len=track_len,
        )
        self.active_by_channel[channel_id] = state

        # Post lobby
        view = LobbyView(self, state, timeout=float(lobby_seconds))
        state.join_view = view
        emb = state.render_lobby_embed(self.bot, remaining=lobby_seconds)
        await interaction.response.send_message(embed=emb, view=view)
        msg = await interaction.original_response()
        state.message_id = msg.id

        # Countdown updates
        started_at = time.monotonic()
        while not view.is_finished() and not state.cancelled:
            await asyncio.sleep(1)
            elapsed = int(time.monotonic() - started_at)
            remaining = max(0, lobby_seconds - elapsed)
            try:
                await msg.edit(embed=state.render_lobby_embed(self.bot, remaining=remaining), view=view)
            except discord.HTTPException:
                pass
            if remaining <= 0:
                view.stop()

        if state.cancelled:
            await self._refund_all(state)
            await msg.edit(content="Race cancelled. Bets refunded.", embed=None, view=None)
            self._finish(channel_id)
            return

        async with state.lock:
            state.started = True

        human_count = len(state.racers)

        if human_count == 0:
            await msg.edit(content="No racers joined. Nothing to do.", embed=None, view=None)
            self._finish(channel_id)
            return

        # Offer choice if exactly one human
        if human_count == 1:
            solo = next(iter(state.racers.values()))
            choice = SoloChoiceView(timeout=25.0)
            await msg.edit(
                content=(
                    f"Only **{solo.display}** joined.\n"
                    "• **Start Race vs AI (Normal)** — AI fill the other lanes; winner gets the pot\n"
                    "• **Continue Solo (Double-or-Nothing)** — no AI, 60% win chance for double your bet\n"
                    "• **Cancel & Refund**"
                ),
                embed=None,
                view=choice,
            )
            await choice.wait()
            if choice.choice == "cancel" or choice.choice is None:
                await self._refund_all(state)
                await msg.edit(content="Race cancelled. Bets refunded.", view=None)
                self._finish(channel_id)
                return
            if choice.choice == "solo":
                # Double or nothing solo
                session = self.bot.SessionLocal()
                try:
                    if not _can_afford(session, solo.user_id, solo.bet):
                        await msg.edit(content=f"{solo.display} doesn't have enough to double. Refunding original bet.", view=None)
                        await self._refund_all(state)
                        self._finish(channel_id)
                        return
                    _add_balance(session, solo.user_id, -solo.bet)
                    solo.bet *= 2
                finally:
                    session.close()
                win = random.random() < 0.60
                # Animate a short solo time trial for fun visuals
                await self._animate_race(msg, state, racers=[solo], track_len=track_len)
                if win:
                    await self._payout(state, winners=[solo.user_id])
                    await msg.edit(content=f"**{solo.display}** wins the solo run and takes **{state.pot:,}** credits!", view=None)
                else:
                    await msg.edit(content=f"**{solo.display}** lost the solo run. Better luck next time!", view=None)
                self._finish(channel_id)
                return
            # else proceed to race vs AI

        # Build full grid with AI filling to 10 lanes
        all_racers: List[Racer] = list(state.racers.values())  # humans
        ai_needed = max(0, state.total_lanes - len(all_racers))
        for i in range(ai_needed):
            all_racers.append(Racer(
                user_id=-1,
                display=f"CPU {i+1}",
                horse=random.choice(HORSE_EMOJIS),
                is_ai=True,
            ))

        # Shuffle starting lanes
        random.shuffle(all_racers)
        for idx, r in enumerate(all_racers):
            r.lane = idx
            r.progress = 0

        await msg.edit(content="Race starting!", embed=None, view=None)
        await self._animate_race(msg, state, racers=all_racers, track_len=track_len)

        # Winners: first to reach track_len, handle ties
        max_prog = max(r.progress for r in all_racers)
        winners = [r for r in all_racers if r.progress >= state.track_len and r.progress == max_prog]

        human_winners = [r.user_id for r in winners if not r.is_ai]
        if human_winners:
            _ = await self._payout(state, winners=human_winners)
            mentions = ", ".join(f"<@{uid}>" for uid in human_winners)
            if len(human_winners) == 1:
                summary = f"{mentions} wins **{state.pot:,}** credits!"
            else:
                share = state.pot // len(human_winners)
                summary = f"{mentions} tie and each receive **{share:,}** credits!"
        else:
            # AI took it—house keeps the pot
            cpu_names = ", ".join(r.display for r in winners[:3])
            summary = f"{cpu_names} wins. No human winners this time!"

        await msg.edit(content=f"🏁 Race finished! {summary}", view=None)
        self._finish(channel_id)

    # --------------- Internals ---------------

    def _finish(self, channel_id: int) -> None:
        state = self.active_by_channel.get(channel_id)
        if state:
            state.resolved = True
        self.active_by_channel.pop(channel_id, None)

    async def _refund_all(self, state: RaceState):
        session = self.bot.SessionLocal()
        try:
            for r in state.racers.values():
                if not r.is_ai and r.bet:
                    _add_balance(session, r.user_id, r.bet)
        finally:
            session.close()

    async def _payout(self, state: RaceState, winners: List[int]) -> Dict[int, int]:
        if not winners:
            return {}
        total = state.pot
        if total <= 0:
            return {}
        share = total // len(winners)
        session = self.bot.SessionLocal()
        try:
            paid: Dict[int, int] = {}
            for uid in winners:
                _add_balance(session, uid, share)
                paid[uid] = share
            return paid
        finally:
            session.close()

    async def _animate_race(self, msg: discord.Message, state: RaceState, *, racers: List[Racer], track_len: int) -> discord.Message:
        tick = 1
        while True:
            await asyncio.sleep(1.0)
            # advance each racer randomly (slight bias to 1-2)
            for r in racers:
                step = random.choices([0,1,2,3], weights=[0.2, 0.45, 0.27, 0.08])[0]
                r.progress = min(track_len, r.progress + step)

            emb = state.render_race_embed(tick, racers)
            try:
                await msg.edit(embed=emb)
            except discord.HTTPException:
                pass

            finished = [r for r in racers if r.progress >= track_len]
            if finished:
                return msg

            tick += 1
            if tick > 90:  # safety
                return msg


async def setup(bot: commands.Bot):
    await bot.add_cog(HorseRace(bot))
