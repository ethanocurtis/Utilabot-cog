from __future__ import annotations
import asyncio
import random
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import text

from utils.economy_helpers import with_session, can_afford, charge, payout, get_balance
from utils.common import ensure_user  # existing helper

RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BLACK_NUMBERS = set(range(1,37)) - RED_NUMBERS
GREEN_NUMBERS = {0}

def number_color(n: int) -> str:
    if n in RED_NUMBERS:
        return "red"
    if n in BLACK_NUMBERS:
        return "black"
    return "green"  # 0

WHEEL_SEQUENCE = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]

class Roulette(commands.Cog):
    """Roulette game using shared economy helpers. Payouts: 
    color/odd/even/low/high 2x; dozens 3x; single number 36x (bet returned via 2x/3x/36x total payouts)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # ensure table
        with self.bot.engine.begin() as conn:
            conn.execute(text(
                """CREATE TABLE IF NOT EXISTS roulette_spins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    bet_type TEXT NOT NULL,
                    bet_value TEXT NOT NULL,
                    bet INTEGER NOT NULL,
                    result_number INTEGER NOT NULL,
                    result_color TEXT NOT NULL,
                    payout INTEGER NOT NULL,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            ))

    @app_commands.command(name="roulette", description="Place a roulette bet and spin the wheel.")
    @app_commands.describe(
        bet="Bet amount (credits)",
        bet_type="Type of bet",
        number="If bet_type is 'number', choose 0–36",
    )
    @app_commands.choices(bet_type=[
        app_commands.Choice(name="Red", value="red"),
        app_commands.Choice(name="Black", value="black"),
        app_commands.Choice(name="Odd", value="odd"),
        app_commands.Choice(name="Even", value="even"),
        app_commands.Choice(name="Low (1-18)", value="low"),
        app_commands.Choice(name="High (19-36)", value="high"),
        app_commands.Choice(name="1st Dozen (1-12)", value="dozen1"),
        app_commands.Choice(name="2nd Dozen (13-24)", value="dozen2"),
        app_commands.Choice(name="3rd Dozen (25-36)", value="dozen3"),
        app_commands.Choice(name="Single Number", value="number"),
    ])
    async def roulette(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, 1_000_000],
        bet_type: app_commands.Choice[str],
        number: Optional[app_commands.Range[int, 0, 36]] = None
    ):
        # Validate input
        if bet_type.value == "number":
            if number is None:
                return await interaction.response.send_message("Pick a number 0–36 when using **Single Number** bet.", ephemeral=True)
            bet_value = str(int(number))
        else:
            bet_value = bet_type.value

        # Charge bet
        with with_session(self.bot.SessionLocal) as session:
            ensure_user(session, interaction.user.id)
            if not can_afford(session, interaction.user.id, bet):
                bal = get_balance(session, interaction.user.id)
                return await interaction.response.send_message(
                    f"Not enough credits. Your balance: **{bal:,}** (bet was **{bet:,}**)",
                    ephemeral=True
                )
            charge(session, interaction.user.id, bet)

        # Initial "spinning" embed
        wheel_preview = " ".join(str(n) for n in WHEEL_SEQUENCE[:12])
        emb = discord.Embed(
            title="🎡 Roulette — Spinning...",
            description=f"Bet: **{bet:,}** on **{bet_type.name}**{f' {number}' if number is not None else ''}\n\n`{wheel_preview} ...`",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=emb)
        msg = await interaction.original_response()

        # Animate: slide through the wheel numbers a few times, slowing down
        idx = random.randint(0, len(WHEEL_SEQUENCE)-1)
        steps = random.randint(24, 36)
        delay = 0.08
        for i in range(steps):
            idx = (idx + 1) % len(WHEEL_SEQUENCE)
            view = [WHEEL_SEQUENCE[(idx - k) % len(WHEEL_SEQUENCE)] for k in range(12)][::-1]
            wheel_preview = " ".join(f"[{n}]" if k == 11 else str(n) for k, n in enumerate(view))
            try:
                await msg.edit(embed=discord.Embed(
                    title="🎡 Roulette — Spinning...",
                    description=f"Bet: **{bet:,}** on **{bet_type.name}**{f' {number}' if number is not None else ''}\n\n`{wheel_preview}`",
                    color=discord.Color.blurple(),
                ))
            except discord.HTTPException:
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 1.12, 0.45)

        result = WHEEL_SEQUENCE[idx]
        color = number_color(result)

        # Compute payout
        multiplier = self._payout_multiplier(bet_type.value, bet_value, result, color)
        total_payout = bet * multiplier  # this is total credited back on win (includes stake)

        # Pay and log
        with with_session(self.bot.SessionLocal) as session:
            if total_payout > 0:
                payout(session, interaction.user.id, total_payout)
            with self.bot.engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO roulette_spins(user_id,guild_id,bet_type,bet_value,bet,result_number,result_color,payout) "
                    "VALUES (:u,:g,:t,:v,:b,:rn,:rc,:p)"
                ), {
                    "u": interaction.user.id,
                    "g": interaction.guild_id or 0,
                    "t": bet_type.value,
                    "v": bet_value,
                    "b": bet,
                    "rn": result,
                    "rc": color,
                    "p": total_payout
                })

        # Final result embed
        pretty = {"red":"🔴 Red", "black":"⚫ Black", "green":"🟢 Green"}
        won = total_payout > 0
        e = discord.Embed(
            title="🎡 Roulette — Result",
            description=(
                f"**Result:** {pretty[color]} **{result}**\n"
                f"**Your bet:** {bet_type.name}{(' ' + str(number)) if number is not None else ''}\n"
                f"**Bet:** {bet:,}    **Payout:** {total_payout:,}"
            ),
            color=discord.Color.green() if won else discord.Color.red(),
        )
        e.set_footer(text="Spin again with the button below.")
        await msg.edit(embed=e, view=self._again_view(interaction.user.id, bet, bet_type, number))

    def _payout_multiplier(self, bet_type: str, bet_value: str, result: int, color: str) -> int:
        if bet_type == "number":
            return 36 if int(bet_value) == result else 0
        if bet_type in ("red","black"):
            return 2 if bet_type == color else 0
        if bet_type == "odd":
            return 0 if result in GREEN_NUMBERS else (2 if result % 2 == 1 else 0)
        if bet_type == "even":
            return 0 if result in GREEN_NUMBERS else (2 if result % 2 == 0 else 0)
        if bet_type == "low":
            return 2 if 1 <= result <= 18 else 0
        if bet_type == "high":
            return 2 if 19 <= result <= 36 else 0
        if bet_type == "dozen1":
            return 3 if 1 <= result <= 12 else 0
        if bet_type == "dozen2":
            return 3 if 13 <= result <= 24 else 0
        if bet_type == "dozen3":
            return 3 if 25 <= result <= 36 else 0
        return 0

    def _again_view(self, user_id: int, bet: int, bet_type: app_commands.Choice[str], number: Optional[int]) -> discord.ui.View:
        v = discord.ui.View(timeout=30)

        async def again(inter: discord.Interaction):
            if inter.user.id != user_id:
                return await inter.response.send_message("Not your bet.", ephemeral=True)
            await self.roulette.callback(self, inter, bet, bet_type, number)

        btn = discord.ui.Button(label=f"Spin Again ({bet:,})", style=discord.ButtonStyle.primary, emoji="🎡")
        btn.callback = again
        v.add_item(btn)
        return v


async def setup(bot: commands.Bot):
    await bot.add_cog(Roulette(bot))
