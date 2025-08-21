
import datetime as dt
from typing import Optional, List

import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import text

from utils.db import ShopItem, Inventory, Balance
from utils.common import ensure_user

DAILY_AMOUNT = 250
DAILY_COOLDOWN_HOURS = 24


def utcnow() -> dt.datetime:
    # store naive UTC in DB for simplicity
    return dt.datetime.utcnow().replace(tzinfo=None)


class ShopView(discord.ui.View):
    """Interactive shop with item select, quantity select, and a Buy button."""

    def __init__(self, bot: commands.Bot, user: discord.abc.User, session_maker, items: List[ShopItem], timeout: float = 180):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user = user
        self.SessionLocal = session_maker

        self.item_map = {str(i.id): i for i in items}
        # Build selects
        self.item_select = discord.ui.Select(
            placeholder="Choose an item to buy…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=i.name, description=f"{i.price} credits", value=str(i.id)) for i in items] or
                    [discord.SelectOption(label="No items available", description="Ask an admin to add items", value="none", default=True)]
        )
        self.qty_select = discord.ui.Select(
            placeholder="Quantity…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=str(q), value=str(q)) for q in (1, 2, 5, 10, 25, 50)]
        )

        self.item_select.callback = self.on_item_change  # type: ignore
        self.qty_select.callback = self.on_qty_change    # type: ignore

        self.add_item(self.item_select)
        self.add_item(self.qty_select)

        self.buy_button = discord.ui.Button(label="Buy", style=discord.ButtonStyle.success, emoji="🛒")
        self.buy_button.callback = self.on_buy  # type: ignore
        self.add_item(self.buy_button)

        self.refresh_button = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
        self.refresh_button.callback = self.on_refresh  # type: ignore
        self.add_item(self.refresh_button)

        self.inv_button = discord.ui.Button(label="My Inventory", style=discord.ButtonStyle.primary, emoji="🎒")
        self.inv_button.callback = self.on_inventory  # type: ignore
        self.add_item(self.inv_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This menu is only for the user who opened it.", ephemeral=True)
            return False
        return True

    async def on_item_change(self, inter: discord.Interaction):
        await inter.response.defer()

    async def on_qty_change(self, inter: discord.Interaction):
        await inter.response.defer()

    def _current_selection(self) -> tuple[Optional[ShopItem], int]:
        item_id = (self.item_select.values[0] if self.item_select.values else None)
        qty_str = (self.qty_select.values[0] if self.qty_select.values else "1")
        qty = max(1, int(qty_str))
        item = self.item_map.get(item_id) if item_id and item_id != "none" else None
        return item, qty

    async def on_buy(self, inter: discord.Interaction):
        item, qty = self._current_selection()
        if not item:
            return await inter.response.send_message("No item selected.", ephemeral=True)

        cost = item.price * qty
        with self.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            if bal.credits < cost:
                return await inter.response.send_message(f"Not enough credits. Need {cost}, you have {bal.credits}.", ephemeral=True)
            bal.credits -= cost
            inv = s.query(Inventory).filter_by(user_id=inter.user.id, item=item.name).first()
            if not inv:
                inv = Inventory(user_id=inter.user.id, item=item.name, qty=0)
                s.add(inv)
            inv.qty += qty
            s.commit()

        await inter.response.send_message(f"✅ Bought {qty}× **{item.name}** for **{cost}**. New balance: **{bal.credits}**.", ephemeral=True)

    async def on_refresh(self, inter: discord.Interaction):
        # re-query the shop in case admins changed it
        with self.SessionLocal() as s:
            items = s.query(ShopItem).all()
        self.item_map = {str(i.id): i for i in items}
        self.item_select.options = [
            discord.SelectOption(label=i.name, description=f"{i.price} credits", value=str(i.id))
            for i in items
        ] or [discord.SelectOption(label="No items available", value="none", default=True)]
        await inter.response.send_message("🔄 Shop refreshed.", ephemeral=True)

    async def on_inventory(self, inter: discord.Interaction):
        with self.SessionLocal() as s:
            entries = s.query(Inventory).filter_by(user_id=inter.user.id).all()
        if not entries:
            return await inter.response.send_message("Inventory empty.", ephemeral=True)
        desc = "\n".join([f"- {e.item} × {e.qty}" for e in entries])
        emb = discord.Embed(title="🎒 Your Inventory", description=desc)
        await inter.response.send_message(embed=emb, ephemeral=True)


class EconomyCog(commands.Cog):
    """Economy + Daily with 24h cooldown + Interactive shop."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Helpers: daily cooldown stored in a simple table via raw SQL ----------
    def _ensure_daily_table(self, s):
        # Compatible with SQLite and Postgres (uses ON CONFLICT below for upsert; SQLite >=3.24 supports it)
        s.execute(text('''
            CREATE TABLE IF NOT EXISTS daily_claims (
                user_id BIGINT PRIMARY KEY,
                last_claim TIMESTAMP
            )
        '''))

    def _get_last_claim(self, s, user_id: int) -> Optional[dt.datetime]:
        self._ensure_daily_table(s)
        row = s.execute(text("SELECT last_claim FROM daily_claims WHERE user_id = :uid"), {"uid": user_id}).fetchone()
        return row[0] if row and row[0] else None

    def _set_last_claim(self, s, user_id: int, when: dt.datetime):
        self._ensure_daily_table(s)
        # upsert
        s.execute(text('''
            INSERT INTO daily_claims (user_id, last_claim)
            VALUES (:uid, :ts)
            ON CONFLICT(user_id) DO UPDATE SET last_claim = excluded.last_claim
        '''), {"uid": user_id, "ts": when})

    # ---------- Commands ----------

    @app_commands.command(name="balance", description="Check your credits.")
    async def balance(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            s.commit()
            await inter.response.send_message(f"💰 Balance: **{bal.credits:,}** credits.")

    @app_commands.command(name="daily", description="Claim your daily credits (every 24 hours).")
    async def daily(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            now = utcnow()
            last = self._get_last_claim(s, inter.user.id)
            if last:
                elapsed = now - last
                remaining = dt.timedelta(hours=DAILY_COOLDOWN_HOURS) - elapsed
                if remaining.total_seconds() > 0:
                    # format remaining
                    total = int(remaining.total_seconds())
                    hrs = total // 3600
                    mins = (total % 3600) // 60
                    secs = total % 60
                    return await inter.response.send_message(
                        f"⏳ You can claim again in **{hrs:02d}:{mins:02d}:{secs:02d}**.",
                        ephemeral=True
                    )

            # award
            bal.credits += DAILY_AMOUNT
            self._set_last_claim(s, inter.user.id, now)
            s.commit()
            await inter.response.send_message(
                f"✅ You claimed **{DAILY_AMOUNT:,}** credits. New balance: **{bal.credits:,}**.",
            )

    @app_commands.command(name="shop", description="Open the interactive shop.")
    async def shop(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            items = s.query(ShopItem).order_by(ShopItem.price.asc()).all()

        if not items:
            emb = discord.Embed(title="🛒 Shop", description="No items for sale. Ask an admin to add some.")
            return await inter.response.send_message(embed=emb, ephemeral=True)

        # Build an embed showing a summary list
        lines = [f"• **{x.name}** — {x.price} credits" for x in items]
        emb = discord.Embed(title="🛒 Shop", description="\n".join(lines))
        view = ShopView(self.bot, inter.user, self.bot.SessionLocal, items)
        await inter.response.send_message(embed=emb, view=view, ephemeral=True)

    @app_commands.command(name="buy", description="Buy an item from the shop (fallback command).")
    async def buy(self, inter: discord.Interaction, item_name: str, qty: int = 1):
        if qty < 1:
            qty = 1
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            item = s.query(ShopItem).filter(ShopItem.name.ilike(item_name)).first()
            if not item:
                return await inter.response.send_message("Item not found.", ephemeral=True)
            cost = item.price * qty
            if bal.credits < cost:
                return await inter.response.send_message(f"Not enough credits. Need {cost}.", ephemeral=True)
            bal.credits -= cost
            inv = s.query(Inventory).filter_by(user_id=inter.user.id, item=item.name).first()
            if not inv:
                inv = Inventory(user_id=inter.user.id, item=item.name, qty=0)
                s.add(inv)
            inv.qty += qty
            s.commit()
            await inter.response.send_message(f"✅ Bought {qty}× **{item.name}** for {cost}. Balance: {bal.credits}.")

    @app_commands.command(name="inventory", description="See your items.")
    async def inventory(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            entries = s.query(Inventory).filter_by(user_id=inter.user.id).all()
            if not entries:
                return await inter.response.send_message("Inventory empty.", ephemeral=True)
            desc = "\n".join([f"- {e.item} × {e.qty}" for e in entries])
            await inter.response.send_message(embed=discord.Embed(title="🎒 Inventory", description=desc), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
