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

PAYOUT_INTERVAL_MIN = 30  # how often to apply passive income (minutes)

# --- economy tuning (adjust to taste) ---
SELL_REFUND = 0.70                 # 70% of total invested cost (purchase + upgrades)
UPGRADE_COST_BASE = 0.60           # upgrade cost factor: base_cost * UPGRADE_COST_BASE * current_level
UPGRADE_YIELD_MULT = 1.25          # each level multiplies yield by 1.25


# ---------- helpers (level, math, migration) ----------

def _effective_yield(biz: Business, level: int) -> float:
    """Return per-hour yield at a given level."""
    if level < 1:
        level = 1
    return float(biz.hourly_yield) * (UPGRADE_YIELD_MULT ** (level - 1))


def _next_upgrade_cost(biz: Business, current_level: int) -> int:
    """Cost to go from current_level -> current_level+1."""
    lvl = max(1, current_level)
    return int(round(biz.cost * UPGRADE_COST_BASE * lvl))


def _total_invested_cost(biz: Business, level: int) -> int:
    """Total credits sunk into this ownership: purchase + upgrades up to 'level'."""
    if level <= 1:
        return int(biz.cost)
    total = biz.cost
    for l in range(1, level):
        total += _next_upgrade_cost(biz, l)
    return int(round(total))


def _ensure_level_column(SessionLocal) -> None:
    """Add ownership.level if missing (simple SQL migration)."""
    try:
        with SessionLocal() as s:
            bind = s.get_bind()
            with bind.begin() as conn:
                try:
                    conn.exec_driver_sql("ALTER TABLE ownership ADD COLUMN level INTEGER NOT NULL DEFAULT 1;")
                except Exception:
                    # Column exists or DB engine rejected duplicate add; ignore
                    pass
    except Exception:
        # If anything fails, we fail soft and features that depend on the column will try to use defaults.
        pass


def _get_level(s: Session, ownership_id: int) -> int:
    row = s.execute(text("SELECT level FROM ownership WHERE id=:i"), {"i": ownership_id}).fetchone()
    if not row:
        return 1
    try:
        return int(row[0] or 1)
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
            for b in businesses[:25]  # Discord limit
        ]
        super().__init__(placeholder="Choose a business to view…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        bid = self.values[0]
        biz = self.business_map.get(bid)
        if not biz:
            return await interaction.response.send_message("Unknown business.", ephemeral=True)

        embed = discord.Embed(
            title=f"🏢 {biz.name}",
            description=(
                f"**Cost:** {biz.cost}\n"
                f"**Base Yield:** {biz.hourly_yield}/hr\n"
                f"**Upgrade (L→L+1) @ L1:** ~{_next_upgrade_cost(biz, 1)}\n"
                f"**Yield Growth/Level:** ×{UPGRADE_YIELD_MULT}"
            ),
            color=discord.Color.blurple()
        )
        await interaction.response.edit_message(embed=embed, view=self.view)  # keep view to continue browsing


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
        bid = self.values[0]
        biz = self.business_map.get(bid)
        if not biz:
            return await interaction.response.send_message("Unknown business.", ephemeral=True)

        with self.bot.SessionLocal() as s:  # type: Session
            user, bal = ensure_user(s, interaction.user.id)
            if bal.credits < biz.cost:
                return await interaction.response.send_message(
                    f"❌ Not enough credits. Need **{biz.cost}**, you have **{bal.credits}**.",
                    ephemeral=True
                )

            # Deduct and record ownership
            bal.credits -= biz.cost
            own = Ownership(user_id=interaction.user.id, business_id=biz.id)
            s.add(own)
            s.flush()  # get own.id
            # ensure level column exists and set to 1
            _set_level(s, own.id, 1)
            s.commit()

            await interaction.response.send_message(
                f"✅ Purchased **{biz.name}** for **{biz.cost}**. New balance: **{bal.credits}**.",
                ephemeral=True
            )


class BusinessSellSelect(discord.ui.Select):
    """Dropdown to sell an owned business (refunds a portion)."""
    def __init__(self, bot: commands.Bot, rows: List[tuple[Ownership, Business, int]]):
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
            # Payout accrued income up to now
            now = dt.datetime.utcnow()
            hrs = max(0.0, (now - own.last_payout_at).total_seconds() / 3600.0)
            payout_passive = int(round(_effective_yield(biz, lvl) * hrs))

            _, bal = ensure_user(s, interaction.user.id)
            bal.credits += payout_passive

            # Refund
            invested = _total_invested_cost(biz, lvl)
            refund = int(round(invested * SELL_REFUND))
            bal.credits += refund

            s.delete(own)
            s.commit()

            await interaction.response.send_message(
                f"💸 Sold **{biz.name} (L{lvl})** — passive payout **{payout_passive}**, "
                f"refund **{refund}**. New balance: **{bal.credits}**.",
                ephemeral=True
            )


class BusinessUpgradeSelect(discord.ui.Select):
    """Dropdown to upgrade an owned business."""
    def __init__(self, bot: commands.Bot, rows: List[tuple[Ownership, Business, int]]):
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
    def __init__(self, bot: commands.Bot, rows: List[tuple[Ownership, Business, int]]):
        super().__init__(timeout=180)
        self.add_item(BusinessSellSelect(bot, rows))


class UpgradeView(discord.ui.View):
    def __init__(self, bot: commands.Bot, rows: List[tuple[Ownership, Business, int]]):
        super().__init__(timeout=180)
        self.add_item(BusinessUpgradeSelect(bot, rows))


# ---------- Admin helpers ----------

def _is_admin(inter: discord.Interaction) -> bool:
    """Basic admin check — adjust to your needs (roles, IDs, etc.)."""
    return bool(inter.guild and inter.user.guild_permissions.manage_guild)


# ---------- Cog ----------

class BusinessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Ensure 'level' exists on ownership (lightweight migration)
        _ensure_level_column(self.bot.SessionLocal)
        self._payout_task.start()

    def cog_unload(self):
        if self._payout_task.is_running():
            self._payout_task.cancel()

    # ----- Autocomplete for business name -----
    async def _business_name_autocomplete(
        self,
        inter: discord.Interaction,
        current: str
    ) -> List[app_commands.Choice[str]]:
        with self.bot.SessionLocal() as s:
            q = s.query(Business).order_by(Business.name.asc())
            if current:
                # case-insensitive contains
                q = q.filter(Business.name.ilike(f"%{current}%"))
            rows = q.limit(25).all()
        return [app_commands.Choice(name=b.name, value=b.name) for b in rows]

    # ----- Admin commands -----

    @app_commands.command(name="business_set_price", description="(Admin) Set the purchase price of a business.")
    @app_commands.describe(name="Business name", new_cost="New purchase price (credits)")
    @app_commands.autocomplete(name=_business_name_autocomplete)
    async def business_set_price(self, inter: discord.Interaction, name: str, new_cost: int):
        if not _is_admin(inter):
            return await inter.response.send_message("Admins only.", ephemeral=True)
        if new_cost < 0:
            return await inter.response.send_message("Cost must be ≥ 0.", ephemeral=True)
        with self.bot.SessionLocal() as s:
            biz = s.query(Business).filter(Business.name.ilike(name)).first()
            if not biz:
                return await inter.response.send_message("Business not found.", ephemeral=True)
            old = biz.cost
            biz.cost = int(new_cost)
            s.commit()
        await inter.response.send_message(
            f"✅ Updated **{biz.name}** cost: **{old} → {biz.cost}**.",
            ephemeral=True
        )

    @app_commands.command(name="business_set_yield", description="(Admin) Set the base hourly yield of a business.")
    @app_commands.describe(name="Business name", new_yield="New base hourly yield")
    @app_commands.autocomplete(name=_business_name_autocomplete)
    async def business_set_yield(self, inter: discord.Interaction, name: str, new_yield: int):
        if not _is_admin(inter):
            return await inter.response.send_message("Admins only.", ephemeral=True)
        if new_yield < 0:
            return await inter.response.send_message("Yield must be ≥ 0.", ephemeral=True)
        with self.bot.SessionLocal() as s:
            biz = s.query(Business).filter(Business.name.ilike(name)).first()
            if not biz:
                return await inter.response.send_message("Business not found.", ephemeral=True)
            old = biz.hourly_yield
            biz.hourly_yield = int(new_yield)
            s.commit()
        await inter.response.send_message(
            f"✅ Updated **{biz.name}** yield: **{old}/hr → {biz.hourly_yield}/hr**.",
            ephemeral=True
        )

    @app_commands.command(name="business_list", description="(Admin) List all businesses with cost & yield.")
    async def business_list(self, inter: discord.Interaction):
        if not _is_admin(inter):
            return await inter.response.send_message("Admins only.", ephemeral=True)
        with self.bot.SessionLocal() as s:
            rows = s.query(Business).order_by(Business.cost.asc()).all()
        if not rows:
            return await inter.response.send_message("No businesses found.", ephemeral=True)
        lines = [f"- **{b.name}** — cost {b.cost}, yield {b.hourly_yield}/hr" for b in rows]
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    # ----- User commands -----

    @app_commands.command(name="business_catalog", description="Browse available businesses.")
    async def business_catalog(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            businesses = s.query(Business).order_by(Business.cost.asc()).all()
        if not businesses:
            return await inter.response.send_message("No businesses are configured yet.", ephemeral=True)

        desc = "Pick a business from the dropdown to see details."
        embed = discord.Embed(title="🏢 Business Catalog", description=desc, color=discord.Color.blurple())
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
            rows_lvl: List[tuple[Ownership, Business, int]] = []
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

            rows_lvl: List[tuple[Ownership, Business, int]] = []
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
        with self.bot.SessionLocal() as s:
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
