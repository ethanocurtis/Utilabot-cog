# cogs/business.py
from __future__ import annotations
import datetime as dt
from typing import List, Tuple, Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.db import Business, Ownership, Balance
from utils.common import ensure_user

PAYOUT_INTERVAL_MIN = 30  # how often to apply passive income

# --- economy tuning ---
SELL_REFUND = 0.70                 # 70% of total invested cost
UPGRADE_COST_BASE = 0.60           # 60% of base cost * current level
UPGRADE_YIELD_MULT = 1.25          # each level multiplies yield by 1.25

# ---------- helpers (level, math, migration) ----------

def _effective_yield(biz: Business, level: int) -> float:
    if level < 1:
        level = 1
    return float(biz.hourly_yield) * (UPGRADE_YIELD_MULT ** (level - 1))

def _next_upgrade_cost(biz: Business, current_level: int) -> int:
    next_level = max(1, current_level)  # cost to go from L -> L+1 scales with current level
    return int(round(biz.cost * UPGRADE_COST_BASE * next_level))

def _total_invested_cost(biz: Business, level: int) -> int:
    """Base purchase + all upgrades up to current 'level'."""
    if level <= 1:
        return int(biz.cost)
    total = biz.cost
    # Upgrades from 1->2, 2->3, ..., (level-1)->level
    for l in range(1, level):
        total += _next_upgrade_cost(biz, l)
    return int(round(total))

def _ensure_level_column(bot) -> None:
    """Add ownership.level if missing (no ORM change required)."""
    engine = bot.SessionLocal.kw['bind'] if hasattr(bot.SessionLocal, 'kw') else None  # defensive
    engine = engine or bot.SessionLocal.kwargs.get('bind') if hasattr(bot.SessionLocal, 'kwargs') else None
    engine = engine or getattr(bot, "engine", None) or getattr(bot, "db_engine", None)
    # Fall back: try to pull engine off a temp session
    try:
        with bot.SessionLocal() as s:
            engine = engine or s.get_bind()
    except Exception:
        engine = None

    if not engine:
        # Best-effort: try raw store engine (some bots expose bot.store.db)
        engine = getattr(getattr(bot, "store", object()), "db", None)

    if not engine:
        return

    with engine.begin() as conn:
        try:
            conn.exec_driver_sql("ALTER TABLE ownership ADD COLUMN level INTEGER NOT NULL DEFAULT 1;")
        except Exception:
            # Column exists or DB doesn't support ALTER in this form—ignore.
            pass

def _get_level(s: Session, ownership_id: int) -> int:
    val = s.execute(text("SELECT level FROM ownership WHERE id=:i"), {"i": ownership_id}).fetchone()
    if not val:
        return 1
    try:
        return int(val[0] or 1)
    except Exception:
        return 1

def _set_level(s: Session, ownership_id: int, level: int) -> None:
    s.execute(text("UPDATE ownership SET level=:lv WHERE id=:i"), {"lv": int(max(1, level)), "i": ownership_id})

# ---------- UI Components ----------

class BusinessCatalogSelect(discord.ui.Select):
    """Dropdown that shows details for a selected business."""
    def __init__(self, bot: commands.Bot, businesses: List[Business]):
        self.bot = bot
        self.business_map = {str(b.id): b for b in businesses}
        options = [
            discord.SelectOption(
                label=b.name[:100],
                value=str(b.id),
                description=f"Cost {b.cost} • Yields {b.hourly_yield}/hr"[:100]
            )
            for b in businesses[:25]
        ]
        super().__init__(placeholder="Choose a business to view…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        biz = self.business_map.get(self.values[0])
        if not biz:
            return await interaction.response.send_message("Unknown business.", ephemeral=True)

        embed = discord.Embed(
            title=f"🏢 {biz.name}",
            description=(
                f"**Cost:** {biz.cost}\n"
                f"**Base Yield:** {biz.hourly_yield}/hr\n"
                f"**Upgrade (L→L+1):** ~{_next_upgrade_cost(biz, 1)} credits at L1"
            ),
            color=discord.Color.blurple()
        )
        await interaction.response.edit_message(embed=embed, view=self.view)

class BusinessBuySelect(discord.ui.Select):
    """Dropdown that buys the selected business for the user."""
    def __init__(self, bot: commands.Bot, businesses: List[Business]):
        self.bot = bot
        self.business_map = {str(b.id): b for b in businesses}
        options = [
            discord.SelectOption(
                label=b.name[:100],
                value=str(b.id),
                description=f"Buy for {b.cost} • {b.hourly_yield}/hr"[:100]
            )
            for b in businesses[:25]
        ]
        super().__init__(placeholder="Pick a business to BUY…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        biz = self.business_map.get(self.values[0])
        if not biz:
            return await interaction.response.send_message("Unknown business.", ephemeral=True)

        with self.bot.SessionLocal() as s:  # type: Session
            user, bal = ensure_user(s, interaction.user.id)
            if bal.credits < biz.cost:
                return await interaction.response.send_message(
                    f"❌ Not enough credits. Need **{biz.cost}**, you have **{bal.credits}**.",
                    ephemeral=True
                )

            bal.credits -= biz.cost
            own = Ownership(user_id=interaction.user.id, business_id=biz.id)
            s.add(own)
            s.flush()  # to get id
            # ensure level column exists and is 1
            _set_level(s, own.id, 1)
            s.commit()

        await interaction.response.send_message(
            f"✅ Purchased **{biz.name}** for **{biz.cost}**. New balance: **{bal.credits}**.",
            ephemeral=True
        )

class BusinessSellSelect(discord.ui.Select):
    """Dropdown to sell an owned business (refunds a portion)."""
    def __init__(self, bot: commands.Bot, rows: List[Tuple[Ownership, Business, int]]):
        self.bot = bot
        # rows contain (own, biz, level)
        self.rows = rows
        options = []
        for own, biz, lvl in rows[:25]:
            eff = int(round(_effective_yield(biz, lvl)))
            invested = _total_invested_cost(biz, lvl)
            refund = int(round(invested * SELL_REFUND))
            options.append(discord.SelectOption(
                label=f"{biz.name} (L{lvl})",
                value=str(own.id),
                description=f"Refund {refund} • {eff}/hr"[:100]
            ))
        super().__init__(placeholder="Pick a business to SELL…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        own_id = int(self.values[0])
        with self.bot.SessionLocal() as s:
            own = s.get(Ownership, own_id)
            if not own or own.user_id != interaction.user.id:
                return await interaction.response.send_message("That ownership is not yours.", ephemeral=True)
            biz = s.get(Business, own.business_id)
            if not biz:
                return await interaction.response.send_message("Business not found.", ephemeral=True)

            lvl = _get_level(s, own.id)
            # First, pay out accrued income up to now
            now = dt.datetime.utcnow()
            hrs = max(0.0, (now - own.last_payout_at).total_seconds() / 3600.0)
            payout_passive = int(round(_effective_yield(biz, lvl) * hrs))

            _, bal = ensure_user(s, interaction.user.id)
            bal.credits += payout_passive

            # Refund based on invested amount and SELL_REFUND
            invested = _total_invested_cost(biz, lvl)
            refund = int(round(invested * SELL_REFUND))
            bal.credits += refund

            # Remove ownership
            s.delete(own)
            s.commit()

            await interaction.response.send_message(
                f"💸 Sold **{biz.name} (L{lvl})** — "
                f"passive payout **{payout_passive}**, refund **{refund}**. "
                f"New balance: **{bal.credits}**.",
                ephemeral=True
            )

class BusinessUpgradeSelect(discord.ui.Select):
    """Dropdown to upgrade an owned business."""
    def __init__(self, bot: commands.Bot, rows: List[Tuple[Ownership, Business, int]]):
        self.bot = bot
        self.rows_map = {str(own.id): (own, biz, lvl) for own, biz, lvl in rows}
        options = []
        for own, biz, lvl in rows[:25]:
            cost = _next_upgrade_cost(biz, lvl)
            new_yield = int(round(_effective_yield(biz, lvl + 1)))
            options.append(discord.SelectOption(
                label=f"{biz.name} (L{lvl} → L{lvl+1})",
                value=str(own.id),
                description=f"Upgrade cost {cost} • New yield {new_yield}/hr"[:100]
            ))
        super().__init__(placeholder="Pick a business to UPGRADE…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        own_id = self.values[0]
        own, biz, lvl = self.rows_map.get(own_id, (None, None, None))
        if not own:
            return await interaction.response.send_message("Unknown ownership.", ephemeral=True)

        with self.bot.SessionLocal() as s:
            own = s.get(Ownership, own.id)
            if not own or own.user_id != interaction.user.id:
                return await interaction.response.send_message("That ownership is not yours.", ephemeral=True)
            biz = s.get(Business, own.business_id)
            if not biz:
                return await interaction.response.send_message("Business not found.", ephemeral=True)

            lvl = _get_level(s, own.id)
            cost = _next_upgrade_cost(biz, lvl)

            _, bal = ensure_user(s, interaction.user.id)
            if bal.credits < cost:
                return await interaction.response.send_message(
                    f"❌ Not enough credits. Need **{cost}**, you have **{bal.credits}**.",
                    ephemeral=True
                )

            # Deduct and bump level
            bal.credits -= cost
            _set_level(s, own.id, lvl + 1)
            s.commit()

            new_y = int(round(_effective_yield(biz, lvl + 1)))
            await interaction.response.send_message(
                f"⬆️ Upgraded **{biz.name}** from **L{lvl} → L{lvl+1}**. "
                f"New yield: **{new_y}/hr**. Balance: **{bal.credits}**.",
                ephemeral=True
            )

class CatalogView(discord.ui.View):
    def __init__(self, bot: commands.Bot, businesses: List[Business]):
        super().__init__(timeout=180)
        self.add_item(BusinessCatalogSelect(bot, businesses))

class BuyView(discord.ui.View):
    def __init__(self, bot: commands.Bot, businesses: List[Business]):
        super().__init__(timeout=180)
        self.add_item(BusinessBuySelect(bot, businesses))

class SellView(discord.ui.View):
    def __init__(self, bot: commands.Bot, rows: List[Tuple[Ownership, Business, int]]):
        super().__init__(timeout=180)
        self.add_item(BusinessSellSelect(bot, rows))

class UpgradeView(discord.ui.View):
    def __init__(self, bot: commands.Bot, rows: List[Tuple[Ownership, Business, int]]):
        super().__init__(timeout=180)
        self.add_item(BusinessUpgradeSelect(bot, rows))

# ---------- Cog ----------

class BusinessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # One-time migration to ensure ownership.level exists
        _ensure_level_column(bot)
        self._payout_task.start()

    def cog_unload(self):
        if self._payout_task.is_running():
            self._payout_task.cancel()

    @app_commands.command(name="business_catalog", description="Browse available businesses.")
    async def business_catalog(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            businesses = s.query(Business).order_by(Business.cost.asc()).all()
        if not businesses:
            return await inter.response.send_message("No businesses are configured yet.", ephemeral=True)

        embed = discord.Embed(
            title="🏢 Business Catalog",
            description="Pick a business from the dropdown to see details.",
            color=discord.Color.blurple()
        )
        await inter.response.send_message(embed=embed, view=CatalogView(self.bot, businesses), ephemeral=True)

    @app_commands.command(name="business_buy", description="Buy a business from a dropdown.")
    async def business_buy(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            businesses = s.query(Business).order_by(Business.cost.asc()).all()
        if not businesses:
            return await inter.response.send_message("No businesses are available to buy.", ephemeral=True)

        embed = discord.Embed(
            title="🛒 Buy a Business",
            description="Select a business from the dropdown to purchase it.",
            color=discord.Color.green()
        )
        await inter.response.send_message(embed=embed, view=BuyView(self.bot, businesses), ephemeral=True)

    @app_commands.command(name="business_sell", description="Sell one of your businesses for a refund.")
    async def business_sell(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            rows = (
                s.query(Ownership, Business)
                .join(Business, Ownership.business_id == Business.id)
                .filter(Ownership.user_id == inter.user.id)
                .all()
            )
            if not rows:
                return await inter.response.send_message("You don't own any businesses.", ephemeral=True)
            # Attach levels
            rows_lvl: List[Tuple[Ownership, Business, int]] = []
            for own, biz in rows:
                lvl = _get_level(s, own.id)
                rows_lvl.append((own, biz, lvl))

        embed = discord.Embed(
            title="💸 Sell a Business",
            description="Select one of your businesses to sell for a partial refund.",
            color=discord.Color.red()
        )
        await inter.response.send_message(embed=embed, view=SellView(self.bot, rows_lvl), ephemeral=True)

    @app_commands.command(name="business_upgrade", description="Upgrade one of your businesses to increase yield.")
    async def business_upgrade(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            rows = (
                s.query(Ownership, Business)
                .join(Business, Ownership.business_id == Business.id)
                .filter(Ownership.user_id == inter.user.id)
                .all()
            )
            if not rows:
                return await inter.response.send_message("You don't own any businesses.", ephemeral=True)

            rows_lvl: List[Tuple[Ownership, Business, int]] = []
            for own, biz in rows:
                lvl = _get_level(s, own.id)
                rows_lvl.append((own, biz, lvl))

        embed = discord.Embed(
            title="⬆️ Upgrade a Business",
            description="Select one to upgrade. Cost and new yield shown in the menu.",
            color=discord.Color.orange()
        )
        await inter.response.send_message(embed=embed, view=UpgradeView(self.bot, rows_lvl), ephemeral=True)

    @app_commands.command(name="business_my", description="Show your owned businesses and accrued earnings since last payout.")
    async def business_my(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            rows = (
                s.query(Ownership, Business)
                .join(Business, Ownership.business_id == Business.id)
                .filter(Ownership.user_id == inter.user.id)
                .all()
            )
            if not rows:
                return await inter.response.send_message("You don't own any businesses yet.", ephemeral=True)

            now = dt.datetime.utcnow()
            lines = []
            for own, biz in rows:
                lvl = _get_level(s, own.id)
                hrs = max(0.0, (now - own.last_payout_at).total_seconds() / 3600.0)
                acc = int(round(_effective_yield(biz, lvl) * hrs))
                ny = int(round(_effective_yield(biz, lvl)))
                up_cost = _next_upgrade_cost(biz, lvl)
                lines.append(
                    f"- **{biz.name}** (L{lvl}) — ~{acc} accrued • {ny}/hr now • next upgrade {up_cost}"
                )

        await inter.response.send_message(
            embed=discord.Embed(title="📈 Your Businesses", description="\n".join(lines), color=discord.Color.gold()),
            ephemeral=True
        )

    @tasks.loop(minutes=PAYOUT_INTERVAL_MIN)
    async def _payout_task(self):
        with self.bot.SessionLocal() as s:
            now = dt.datetime.utcnow()
            owns = s.query(Ownership).all()
            for own in owns:
                biz = s.get(Business, own.business_id)
                if not biz:
                    continue
                lvl = _get_level(s, own.id)
                delta_hours = max(0.0, (now - own.last_payout_at).total_seconds() / 3600.0)
                payout = int(round(_effective_yield(biz, lvl) * delta_hours))
                if payout > 0:
                    _, bal = ensure_user(s, own.user_id)
                    bal.credits += payout
                    own.last_payout_at = now
            s.commit()

async def setup(bot: commands.Bot):
    await bot.add_cog(BusinessCog(bot))