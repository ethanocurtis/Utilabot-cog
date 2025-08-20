# cogs/business.py
from __future__ import annotations
import datetime as dt
import discord
from discord.ext import commands, tasks
from discord import app_commands
from typing import List
from sqlalchemy.orm import Session
from utils.db import Business, Ownership, Balance
from utils.common import ensure_user

PAYOUT_INTERVAL_MIN = 30  # how often to apply passive income

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
            description=f"**Cost:** {biz.cost}\n**Yield:** {biz.hourly_yield} per hour",
            color=discord.Color.blurple()
        )
        await interaction.response.edit_message(embed=embed, view=self.view)  # keep the view for further browsing


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

        # Perform the purchase atomically
        with self.bot.SessionLocal() as s:  # type: Session
            user, bal = ensure_user(s, interaction.user.id)
            if bal.credits < biz.cost:
                return await interaction.response.send_message(
                    f"❌ Not enough credits. Need **{biz.cost}**, you have **{bal.credits}**.",
                    ephemeral=True
                )

            # Deduct and record ownership
            bal.credits -= biz.cost
            s.add(Ownership(user_id=interaction.user.id, business_id=biz.id))
            s.commit()

            await interaction.response.send_message(
                f"✅ Purchased **{biz.name}** for **{biz.cost}**. New balance: **{bal.credits}**.",
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

# ---------- Cog ----------

class BusinessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
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
            hrs = max(0.0, (now - own.last_payout_at).total_seconds() / 3600.0)
            acc = int(biz.hourly_yield * hrs)
            lines.append(f"- **{biz.name}** — ~{acc} credits accrued since last payout")

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
                delta_hours = max(0.0, (now - own.last_payout_at).total_seconds() / 3600.0)
                payout = int(biz.hourly_yield * delta_hours)
                if payout > 0:
                    _, bal = ensure_user(s, own.user_id)
                    bal.credits += payout
                    own.last_payout_at = now
            s.commit()

async def setup(bot: commands.Bot):
    await bot.add_cog(BusinessCog(bot))