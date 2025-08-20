import discord, datetime as dt
from discord.ext import commands
from discord import app_commands
from sqlalchemy.orm import Session
from utils.db import ShopItem, Inventory, Balance
from utils.common import ensure_user, add_credits

DAILY_AMOUNT = 250

class EconomyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="balance", description="Check your credits.")
    async def balance(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            s.commit()
            await inter.response.send_message(f"💰 Balance: **{bal.credits}** credits.")

    @app_commands.command(name="daily", description="Claim your daily credits.")
    async def daily(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            # simple daily with no cooldown persistence (kept minimal). You can extend with extra table
            bal.credits += DAILY_AMOUNT
            s.commit()
            await inter.response.send_message(f"✅ You claimed {DAILY_AMOUNT} credits. New balance: **{bal.credits}**.")

    @app_commands.command(name="shop", description="List shop items.")
    async def shop(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            items = s.query(ShopItem).all()
            desc = "\n".join([f"- **{x.name}** — {x.price} credits" for x in items]) or "No items."
            await inter.response.send_message(embed=discord.Embed(title="🛒 Shop", description=desc))

    @app_commands.command(name="buy", description="Buy an item from the shop.")
    async def buy(self, inter: discord.Interaction, item_name: str, qty: int = 1):
        if qty < 1: qty = 1
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
                inv = Inventory(user_id=inter.user.id, item=item.name, qty=0); s.add(inv)
            inv.qty += qty
            s.commit()
            await inter.response.send_message(f"✅ Bought {qty}x **{item.name}** for {cost}. Balance: {bal.credits}.")

    @app_commands.command(name="inventory", description="See your items.")
    async def inventory(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            entries = s.query(Inventory).filter_by(user_id=inter.user.id).all()
            if not entries:
                return await inter.response.send_message("Inventory empty.")
            desc = "\n".join([f"- {e.item} × {e.qty}" for e in entries])
            await inter.response.send_message(embed=discord.Embed(title="🎒 Inventory", description=desc))

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
