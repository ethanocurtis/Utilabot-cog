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
# Utility: DB helpers
# ------------------------------

def _get_balance(session, user_id: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    return int(bal.amount) if bal else 0

def _add_balance(session, user_id: int, delta: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        bal = Balance(user_id=user_id, amount=0)
        session.add(bal)
        session.flush()
    bal.amount = int(bal.amount) + int(delta)
    session.commit()
    return int(bal.amount)

def _can_afford(session, user_id: int, amount: int) -> bool:
    return _get_balance(session, user_id) >= amount


# ------------------------------
# Game data structures
# ------------------------------

HORSE_EMOJIS = ["🐎", "🐴", "🏇", "🦄"]
TRACK_ICON = "—"
FINISH_FLAG = "🏁"
CARROT = "🥕"

@dataclass
class Racer:
    user_id: int
    display: str
    horse: str
    bet: int
    lane: int = 0
    progress: int = 0


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
    racers: Dict[int, Racer] = field(default_factory=dict)  # user_id -> Racer
    join_view: Optional[discord.ui.View] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancelled: bool = False
    resolved: bool = False

    @property
    def pot(self) -> int:
        return sum(r.bet for r in self.racers.values())

    def render_lobby_embed(self, bot: commands.Bot, remaining: int) -> discord.Embed:
        e = discord.Embed(
            title="🏇 Horse Race — Lobby",
            description=(
                f"Bet per racer: **{self.bet:,}** credits\n"
                f"Pot so far: **{self.pot:,}**\n"
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

    def render_track(self) -> Tuple[str, List[str]]:
        lanes = []
        order = []
        for r in self.racers.values():
            # Build lane like: 🐎———🥕————🏁
            lane = r.horse + TRACK_ICON * r.progress + CARROT + TRACK_ICON * (self.track_len - r.progress) + FINISH_FLAG
            lanes.append(lane)
            order.append((r.progress, r.user_id))
        # Top to bottom sorted by lane number (stable order on tie)
        return "\n".join(lanes), [str(uid) for _p, uid in sorted(order, key=lambda x: -x[0])]

    def render_race_embed(self, tick: int) -> discord.Embed:
        track_str, leaders = self.render_track()
        leader_mentions = ", ".join(f"<@{uid}>" for uid in leaders[:3])
        e = discord.Embed(
            title=f"🏇 Horse Race — Lap {tick}",
            description=f"First to {self.track_len} wins. Pot: **{self.pot:,}**",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Track", value=f"```\n{track_str}\n```", inline=False)
        if leader_mentions:
            e.add_field(name="Leaders", value=leader_mentions, inline=False)
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
        # only allow in the same channel
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
                ensure_user(session, uid)  # ensures rows exist in your schema
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
            )
            self.state.racers[uid] = racer
            # Update lobby embed
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


class SoloContinueView(discord.ui.View):
    def __init__(self, *, timeout: float = 20.0):
        super().__init__(timeout=timeout)
        self.choice: Optional[bool] = None  # True=continue solo, False=cancel

    @discord.ui.button(label="Continue Solo (Double-or-Nothing)", style=discord.ButtonStyle.success, emoji="💥")
    async def solo(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel & Refund", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = False
        await interaction.response.defer()
        self.stop()


# ------------------------------
# Cog
# ------------------------------

class HorseRace(commands.Cog):
    """Interactive horse-race betting game."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # limit to one active game per channel
        self.active_by_channel: Dict[int, RaceState] = {}

    def _guard_channel_free(self, channel_id: int) -> bool:
        state = self.active_by_channel.get(channel_id)
        return not state or state.resolved or state.cancelled

    # --------------- Slash Command ---------------
    @app_commands.command(name="horserace", description="Start a multiplayer horse race with betting.")
    @app_commands.describe(bet="Bet per player (credits)", lobby_seconds="How long to wait for racers", track_len="Track length (fun visual)")
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

        # Check host can afford to join (if they do)
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

        # Countdown (allows live edits)
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

        # Lobby ended
        if state.cancelled:
            await self._refund_all(state)
            await msg.edit(content="Race cancelled. Bets refunded.", embed=None, view=None)
            self._finish(channel_id)
            return

        async with state.lock:
            state.started = True

        # If fewer than 2 entrants, offer solo continue for double-or-nothing
        if len(state.racers) < 2:
            if not state.racers:
                await msg.edit(content="No racers joined. Nothing to do.", embed=None, view=None)
                self._finish(channel_id)
                return

            solo = next(iter(state.racers.values()))
            ask = SoloContinueView(timeout=20.0)
            await msg.edit(
                content=f"Only **{solo.display}** joined. Continue solo for **double** the winnings or cancel for a full refund?",
                embed=None,
                view=ask,
            )
            await ask.wait()
            if ask.choice is None or ask.choice is False:
                # Refund
                await self._refund_all(state)
                await msg.edit(content="Race cancelled. Bets refunded.", embed=None, view=None)
                self._finish(channel_id)
                return

            # Double the pot by doubling their bet (deduct again)
            session = self.bot.SessionLocal()
            try:
                if not _can_afford(session, solo.user_id, solo.bet):
                    await msg.edit(content=f"{solo.display} does not have enough to double the bet. Refunding original bet.", view=None)
                    await self._refund_all(state)
                    self._finish(channel_id)
                    return
                _add_balance(session, solo.user_id, -solo.bet)
                solo.bet *= 2
            finally:
                session.close()

            # Run a solo time-trial: win chance 60%
            win = random.random() < 0.60
            if win:
                _ = await self._payout(state, winners=[solo.user_id])
                track_msg = await self._animate_race(msg, state, solo_only=True)
                await track_msg.edit(content=f"**{solo.display}** wins the solo run and takes **{state.pot:,}** credits!", view=None)
            else:
                # House keeps pot (pot remains in system); nothing to do
                await msg.edit(content=f"**{solo.display}** lost the solo run. Better luck next time!", view=None)
            self._finish(channel_id)
            return

        # Run the multiplayer race
        await msg.edit(content="Race starting!", embed=None, view=None)
        await self._animate_race(msg, state)
        # Decide winners (first to finish; handle ties)
        max_prog = max(r.progress for r in state.racers.values())
        winners = [r.user_id for r in state.racers.values() if r.progress >= state.track_len and r.progress == max_prog]
        payout_map = await self._payout(state, winners=winners)

        win_mentions = ", ".join(f"<@{uid}>" for uid in winners)
        if len(winners) == 1:
            summary = f"{win_mentions} wins **{state.pot:,}** credits!"
        else:
            share = state.pot // max(1, len(winners))
            summary = f"{win_mentions} tie and each receive **{share:,}** credits!"

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
                _add_balance(session, r.user_id, r.bet)
        finally:
            session.close()

    async def _payout(self, state: RaceState, winners: List[int]) -> Dict[int, int]:
        """Return map of winner_id -> amount won (after split)."""
        if not winners:
            return {}
        total = state.pot
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

    async def _animate_race(self, msg: discord.Message, state: RaceState, *, solo_only: bool = False) -> discord.Message:
        # Initialize lanes
        for i, r in enumerate(state.racers.values()):
            r.lane = i
            r.progress = 0

        tick = 1
        # Animated loop
        while True:
            await asyncio.sleep(1.0)
            # advance each racer randomly (biased a little)
            for r in state.racers.values():
                # step 0-3 with slight bias to 1-2
                step = random.choices([0,1,2,3], weights=[0.2, 0.4, 0.3, 0.1])[0]
                r.progress = min(state.track_len, r.progress + step)

            emb = state.render_race_embed(tick)
            try:
                await msg.edit(embed=emb)
            except discord.HTTPException:
                pass

            # Check finish
            finished = [r for r in state.racers.values() if r.progress >= state.track_len]
            if finished:
                return msg

            tick += 1
            # safety cap
            if tick > 60:
                return msg


async def setup(bot: commands.Bot):
    await bot.add_cog(HorseRace(bot))
