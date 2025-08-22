from __future__ import annotations
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# Project helpers (available in your repo)
from utils.db import Balance  # type: ignore
from utils.common import ensure_user  # type: ignore

# ------------------------------
# Config
# ------------------------------
MAX_LANES = 10
HORSE_SET = ["🐎", "🐴", "🏇", "🦄", "🐎", "🐴", "🏇", "🦄", "🐎", "🐴"]
TRACK_ICON = "—"
FINISH_FLAG = "🏁"


# ------------------------------
# Balance helpers (auto-detect field)
# ------------------------------
_AMT_FIELDS = ("amount", "balance", "credits", "coins", "value")

def _pick_amount_attr(bal: Balance) -> Optional[str]:
    for name in _AMT_FIELDS:
        if hasattr(bal, name):
            return name
    # fallback: first numeric attribute in SQLAlchemy model dict
    for k, v in vars(bal).items():
        if k.startswith("_"):
            continue
        if isinstance(v, (int, float)):
            return k
    return None

def _get_balance(session, user_id: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        return 0
    attr = _pick_amount_attr(bal)
    return int(getattr(bal, attr)) if attr else 0

def _set_balance(session, user_id: int, value: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        bal = Balance(user_id=user_id)
        session.add(bal)
        session.flush()
    attr = _pick_amount_attr(bal)
    if not attr:
        raise RuntimeError("Balance model has no numeric amount field I can detect.")
    setattr(bal, attr, int(value))
    session.commit()
    return int(getattr(bal, attr))

def _add_balance(session, user_id: int, delta: int) -> int:
    current = _get_balance(session, user_id)
    return _set_balance(session, user_id, current + int(delta))

def _can_afford(session, user_id: int, amount: int) -> bool:
    return _get_balance(session, user_id) >= amount


# ------------------------------
# Game data structures
# ------------------------------

@dataclass
class Racer:
    user_id: Optional[int]   # None for AI
    display: str
    horse: str
    bet: int
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
    racers: Dict[str, Racer] = field(default_factory=dict)  # key: lane id str or user id str
    join_view: Optional[discord.ui.View] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancelled: bool = False
    resolved: bool = False

    @property
    def human_racers(self) -> List[Racer]:
        return [r for r in self.racers.values() if not r.is_ai]

    @property
    def pot(self) -> int:
        # pot is humans' bets only
        return sum(r.bet for r in self.human_racers)

    def render_lobby_embed(self, remaining: int) -> discord.Embed:
        e = discord.Embed(
            title="🏇 Horse Race — Lobby",
            description=(
                f"Bet per racer: **{self.bet:,}** credits\n"
                f"Pot so far (humans): **{self.pot:,}**\n"
                f"Time left to join: **{remaining}s**"
            ),
            color=discord.Color.gold(),
        )
        if self.racers:
            lines = []
            for r in self.human_racers:
                lines.append(f"{r.horse} **{r.display}** — bet **{r.bet:,}**")
            if not lines:
                lines.append("_No human racers yet_")
            e.add_field(name="Human Racers", value="\n".join(lines), inline=False)
        else:
            e.add_field(name="Human Racers", value="(none yet)", inline=False)
        e.set_footer(text="Click Join to enter, Leave to withdraw. Host can start early or cancel.")
        return e

    def _render_lane(self, r: Racer) -> str:
        # Example: ——🐎——————🏁  (horse itself moves across track)
        progress = max(0, min(self.track_len, r.progress))
        left = TRACK_ICON * progress
        right = TRACK_ICON * (self.track_len - progress)
        return f"{left}{r.horse}{right}{FINISH_FLAG}"

    def render_track(self) -> Tuple[str, List[str]]:
        lanes: List[str] = []
        order: List[Tuple[int, str]] = []
        # Stable order by lane index
        for idx, r in enumerate(self.racers.values()):
            lane = self._render_lane(r)
            lanes.append(lane)
            # for leaders, key them by 'H:<id>' for humans; 'A:idx' for ai
            key = f"H:{r.user_id}" if r.user_id is not None else f"A:{idx}"
            order.append((r.progress, key))
        # order for display (leaders top 3)
        leaders_keys = [k for _p, k in sorted(order, key=lambda x: -x[0])]
        return "\n".join(lanes), leaders_keys

    def render_race_embed(self, tick: int) -> discord.Embed:
        track_str, leaders = self.render_track()
        # Build human-only mentions in leaders if any
        leader_mentions: List[str] = []
        for k in leaders[:3]:
            if k.startswith("H:"):
                uid = int(k.split(":", 1)[1])
                leader_mentions.append(f"<@{uid}>")
            else:
                leader_mentions.append("CPU")
        e = discord.Embed(
            title=f"🏇 Horse Race — Lap {tick}",
            description=f"First to {self.track_len} wins. Human Pot: **{self.pot:,}**",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Track", value=f"```\n{track_str}\n```", inline=False)
        if leader_mentions:
            e.add_field(name="Leaders", value=", ".join(leader_mentions), inline=False)
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
            if any((r.user_id == uid) for r in self.state.human_racers):
                return await interaction.response.send_message("You're already in!", ephemeral=True)
            if len(self.state.human_racers) >= MAX_LANES:
                return await interaction.response.send_message("All lanes are full.", ephemeral=True)

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
                horse=HORSE_SET[len(self.state.racers) % len(HORSE_SET)],
                bet=self.state.bet,
                is_ai=False,
            )
            self.state.racers[f"H:{uid}"] = racer
            remaining = max(0, int(self.timeout or 0))
            emb = self.state.render_lobby_embed(remaining)
            await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.state.lock:
            uid = interaction.user.id
            key = f"H:{uid}"
            if key not in self.state.racers:
                return await interaction.response.send_message("You're not in.", ephemeral=True)
            if self.state.started:
                return await interaction.response.send_message("Too late—race already started!", ephemeral=True)

            bet = self.state.racers[key].bet
            del self.state.racers[key]

            # Refund
            session = self.cog.bot.SessionLocal()
            try:
                _add_balance(session, uid, bet)
            finally:
                session.close()

            remaining = max(0, int(self.timeout or 0))
            emb = self.state.render_lobby_embed(remaining)
            await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Start Now", style=discord.ButtonStyle.primary, emoji="🚦")
    async def start_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.author_id:
            return await interaction.response.send_message("Only the host can start early.", ephemeral=True)
        async with self.state.lock:
            self.state.started = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.author_id:
            return await interaction.response.send_message("Only the host can cancel.", ephemeral=True)
        async with self.state.lock:
            self.state.cancelled = True
        await interaction.response.defer()
        self.stop()


# ------------------------------
# Cog
# ------------------------------

class HorseRace(commands.Cog):
    """Interactive horse-race betting game with AI fill."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_by_channel: Dict[int, RaceState] = {}

    def _guard_channel_free(self, channel_id: int) -> bool:
        state = self.active_by_channel.get(channel_id)
        return not state or state.resolved or state.cancelled

    @app_commands.command(name="horserace", description="Start a horse race with betting (AI fills empty lanes up to 10).")
    @app_commands.describe(bet="Bet per human (credits)", lobby_seconds="How long to wait for racers", track_len="Track length (10-40)")
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

        # init state
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
        emb = state.render_lobby_embed(remaining=lobby_seconds)
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
                await msg.edit(embed=state.render_lobby_embed(remaining), view=view)
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

        human_count = len(state.human_racers)
        if human_count == 0:
            await msg.edit(content="No human racers joined. Bets (if any) refunded.", embed=None, view=None)
            await self._refund_all(state)
            self._finish(channel_id)
            return

        # Fill with AI up to MAX_LANES
        ai_needed = max(0, min(MAX_LANES, MAX_LANES - human_count))
        for i in range(ai_needed):
            name = f"CPU {i+1}"
            racer = Racer(
                user_id=None,
                display=name,
                horse=HORSE_SET[(len(state.racers)) % len(HORSE_SET)],
                bet=0,
                is_ai=True,
            )
            state.racers[f"A:{i}"] = racer

        # Start the race
        await msg.edit(content="Race starting! (AI fill active)", embed=None, view=None)
        await self._animate_race(msg, state)

        # Determine winners
        max_prog = max(r.progress for r in state.racers.values())
        winners = [r for r in state.racers.values() if r.progress >= state.track_len and r.progress == max_prog]

        human_winners = [r for r in winners if not r.is_ai]
        if human_winners:
            paid = await self._payout(state, [r.user_id for r in human_winners if r.user_id is not None])
            mentions = ", ".join(f"<@{r.user_id}>" for r in human_winners if r.user_id is not None)
            if len(human_winners) == 1:
                summary = f"{mentions} wins **{state.pot:,}** credits!"
            else:
                share = state.pot // len(human_winners)
                summary = f"{mentions} tie and each receive **{share:,}** credits!"
        else:
            summary = "Only AI crossed the line first. House keeps the pot."

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
            for r in state.human_racers:
                _add_balance(session, r.user_id, r.bet)  # type: ignore[arg-type]
        finally:
            session.close()

    async def _payout(self, state: RaceState, winner_ids: List[int]) -> Dict[int, int]:
        if not winner_ids:
            return {}
        total = state.pot
        share = total // len(winner_ids)
        session = self.bot.SessionLocal()
        try:
            paid: Dict[int, int] = {}
            for uid in winner_ids:
                _add_balance(session, uid, share)
                paid[uid] = share
            return paid
        finally:
            session.close()

    async def _animate_race(self, msg: discord.Message, state: RaceState) -> discord.Message:
        # Initialize
        for i, r in enumerate(state.racers.values()):
            r.lane = i
            r.progress = 0

        tick = 1
        while True:
            await asyncio.sleep(1.0)
            # advance each racer
            for r in state.racers.values():
                # AI and humans share same distribution for fairness; tweak later if desired
                step = random.choices([0,1,2,3], weights=[0.2, 0.4, 0.3, 0.1])[0]
                r.progress = min(state.track_len, r.progress + step)

            emb = state.render_race_embed(tick)
            try:
                await msg.edit(embed=emb)
            except discord.HTTPException:
                pass

            # finish?
            finished = [r for r in state.racers.values() if r.progress >= state.track_len]
            if finished:
                return msg

            tick += 1
            if tick > 60:
                return msg


async def setup(bot: commands.Bot):
    await bot.add_cog(HorseRace(bot))
