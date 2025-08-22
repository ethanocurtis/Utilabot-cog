from __future__ import annotations
import asyncio
import random
import datetime as dt
from typing import Optional, Dict, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import text

from utils.db import Balance  # your existing Balance model
from utils.common import ensure_user  # your helper

# -----------------------------
# Utility DB helpers
# -----------------------------

def _get_balance(session, user_id: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        return 0
    # auto-detect column name
    for field in ["amount", "balance", "credits", "coins", "value"]:
        if hasattr(bal, field):
            return int(getattr(bal, field) or 0)
    return 0

def _add_balance(session, user_id: int, delta: int) -> int:
    bal = session.query(Balance).filter_by(user_id=user_id).one_or_none()
    if not bal:
        bal = Balance(user_id=user_id)
        session.add(bal)
        session.flush()
    # auto-detect field
    for field in ["amount", "balance", "credits", "coins", "value"]:
        if hasattr(bal, field):
            current = int(getattr(bal, field) or 0)
            setattr(bal, field, current + delta)
            session.commit()
            return current + delta
    session.commit()
    return 0

def _can_afford(session, user_id: int, amount: int) -> bool:
    return _get_balance(session, user_id) >= amount


# -----------------------------
# Slots Cog
# -----------------------------

SYMBOLS = ["🍒", "🍋", "🔔", "⭐", "7️⃣", "💎"]
# relative weights for symbol rarity (rarer means bigger wins usually)
WEIGHTS = [28, 22, 18, 14, 10, 8]
MULTIPLIERS = {
    "🍒": 5,
    "🍋": 10,
    "🔔": 20,
    "⭐": 30,
    "7️⃣": 50,
    "💎": 100,
}

class Slots(commands.Cog):
    """Slot machine game with animated embed + leaderboards."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # ensure tables exist
        with self.bot.engine.begin() as conn:
            conn.execute(text(
                """CREATE TABLE IF NOT EXISTS slots_spins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    guild_id INTEGER,
                    bet INTEGER,
                    reels TEXT,
                    payout INTEGER,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            ))

    # ------------- Commands -------------

    @app_commands.command(name="slots", description="Spin the slot machine!")
    @app_commands.describe(bet="Amount to bet (credits)")
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 100000]):
        # charge bet
        session = self.bot.SessionLocal()
        try:
            ensure_user(session, interaction.user.id)
            if not _can_afford(session, interaction.user.id, bet):
                return await interaction.response.send_message(f"You need **{bet:,}** credits to spin.", ephemeral=True)
            _add_balance(session, interaction.user.id, -bet)
        finally:
            session.close()

        # initial message
        await interaction.response.send_message(embed=self._render_embed(["❔","❔","❔"], bet, 0))
        msg = await interaction.original_response()

        # Spin with animation, stopping reels one-by-one
        reels: List[str] = []
        deck = [s for s, w in zip(SYMBOLS, WEIGHTS) for _ in range(w)]
        for i in range(3):
            await asyncio.sleep(0.9)
            symbol = random.choice(deck)
            reels.append(symbol)
            emb = self._render_embed(reels + ["❔"]*(2-i), bet, 0)
            await msg.edit(embed=emb)

        payout = self._calculate_payout(reels, bet)

        # pay winnings + log
        session = self.bot.SessionLocal()
        try:
            if payout > 0:
                _add_balance(session, interaction.user.id, payout)
            with self.bot.engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO slots_spins(user_id,guild_id,bet,reels,payout) VALUES (:u,:g,:b,:r,:p)"),
                    {"u": interaction.user.id, "g": interaction.guild_id or 0, "b": bet, "r": "".join(reels), "p": payout},
                )
        finally:
            session.close()

        emb = self._render_embed(reels, bet, payout, final=True)
        await msg.edit(embed=emb, view=self._make_view(interaction.user.id, bet))

    @app_commands.command(name="slots_leaderboard", description="Show top winners (by net profit) in this server.")
    async def slots_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = self._fetch_leaderboard(interaction.guild_id or 0, limit=10)
        if not rows:
            return await interaction.followup.send("No spins recorded yet. Be the first!")

        # Build leaderboard embed
        e = discord.Embed(title="🎰 Slots Leaderboard — Net Profit", color=discord.Color.purple())
        lines = []
        for i, (user_id, spins, wagered, payout, net) in enumerate(rows, start=1):
            user = interaction.guild.get_member(user_id) if interaction.guild else None
            name = user.display_name if user else f"User {user_id}"
            lines.append(f"**{i}. {name}** — Net **{net:,}** (Spins: {spins}, Wagered: {wagered:,}, Won: {payout:,})")
        e.description = "\n".join(lines)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="slots_mystats", description="See your personal slots stats.")
    async def slots_mystats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        stats = self._fetch_user_stats(interaction.guild_id or 0, interaction.user.id)
        if not stats:
            return await interaction.followup.send("No spins recorded yet.", ephemeral=True)

        spins, wagered, payout, net, biggest = stats
        e = discord.Embed(title="🎰 Your Slots Stats", color=discord.Color.gold())
        e.add_field(name="Spins", value=str(spins))
        e.add_field(name="Total Wagered", value=f"{wagered:,}")
        e.add_field(name="Total Won", value=f"{payout:,}")
        e.add_field(name="Net", value=f"{net:,}")
        rtp = (payout / wagered * 100) if wagered > 0 else 0.0
        e.add_field(name="RTP", value=f"{rtp:.1f}%")
        e.add_field(name="Biggest Win", value=f"{biggest:,}")
        await interaction.followup.send(embed=e, ephemeral=True)

    # ------------- Helpers -------------

    def _calculate_payout(self, reels: List[str], bet: int) -> int:
        # three of a kind
        if reels[0] == reels[1] == reels[2]:
            return bet * MULTIPLIERS.get(reels[0], 0)
        # two cherries small payout
        if reels.count("🍒") == 2:
            return bet * 2
        return 0

    def _render_embed(self, reels: List[str], bet: int, payout: int, final: bool=False) -> discord.Embed:
        desc = " | ".join(reels)
        emb = discord.Embed(title="🎰 Slot Machine", description=f"**{desc}**", color=discord.Color.gold())
        emb.add_field(name="Bet", value=f"{bet:,}", inline=True)
        if final:
            if payout > 0:
                emb.add_field(name="Result", value=f"WIN! +{payout:,}", inline=True)
            else:
                emb.add_field(name="Result", value="No win", inline=True)
            emb.set_footer(text="Press the button below to spin again.")
        else:
            emb.add_field(name="Spinning...", value="Please wait", inline=True)
        return emb

    def _make_view(self, user_id: int, bet: int) -> discord.ui.View:
        view = discord.ui.View(timeout=30)

        async def spin_again(inter: discord.Interaction):
            if inter.user.id != user_id:
                return await inter.response.send_message("Not your session.", ephemeral=True)
            await self.slots.callback(self, inter, bet)  # rerun command with same bet

        btn = discord.ui.Button(label=f"Spin Again ({bet:,})", style=discord.ButtonStyle.primary, emoji="🎰")
        btn.callback = spin_again
        view.add_item(btn)
        return view

    def _fetch_leaderboard(self, guild_id: int, limit: int = 10) -> List[Tuple[int,int,int,int,int]]:
        # returns list of (user_id, spins, wagered, payout, net) sorted by net desc
        sql = """
            SELECT user_id,
                   COUNT(*) as spins,
                   COALESCE(SUM(bet),0) as wagered,
                   COALESCE(SUM(payout),0) as payout,
                   COALESCE(SUM(payout - bet),0) as net
            FROM slots_spins
            WHERE guild_id = :g
            GROUP BY user_id
            ORDER BY net DESC
            LIMIT :lim
        """
        with self.bot.engine.connect() as c:
            rows = c.execute(text(sql), {"g": guild_id, "lim": limit}).fetchall()
        return [(int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows]

    def _fetch_user_stats(self, guild_id: int, user_id: int) -> Optional[Tuple[int,int,int,int,int]]:
        # returns (spins, wagered, payout, net, biggest_win)
        sql = """
            SELECT COUNT(*) as spins,
                   COALESCE(SUM(bet),0) as wagered,
                   COALESCE(SUM(payout),0) as payout,
                   COALESCE(SUM(payout - bet),0) as net,
                   COALESCE(MAX(payout),0) as biggest
            FROM slots_spins
            WHERE guild_id = :g AND user_id = :u
        """
        with self.bot.engine.connect() as c:
            row = c.execute(text(sql), {"g": guild_id, "u": user_id}).fetchone()
        if not row:
            return None
        return (int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]))


async def setup(bot: commands.Bot):
    await bot.add_cog(Slots(bot))
