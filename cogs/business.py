import datetime as dt
import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy.orm import Session
from sqlalchemy import select
from utils.db import Business, Ownership, Balance
from utils.common import ensure_user

PAYOUT_INTERVAL_MIN = 30  # check interval

class BusinessCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._payout_task.start()

    def cog_unload(self):
        if self._payout_task.is_running():
            self._payout_task.cancel()

    @app_commands.command(name="business_list", description="List available businesses to buy.")
    async def business_list(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            rows = s.query(Business).all()
            desc = "\n".join([f"- **{b.name}** — cost {b.cost}, yields {b.hourly_yield}/hr" for b in rows]) or "None."
            await inter.response.send_message(embed=discord.Embed(title="🏢 Businesses", description=desc))

    @app_commands.command(name="business_buy", description="Buy a business.")
    async def business_buy(self, inter: discord.Interaction, name: str):
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            biz = s.query(Business).filter(Business.name.ilike(name)).first()
            if not biz:
                return await inter.response.send_message("Business not found.", ephemeral=True)
            if bal.credits < biz.cost:
                return await inter.response.send_message("Not enough credits.", ephemeral=True)
            bal.credits -= biz.cost
            s.add(Ownership(user_id=inter.user.id, business_id=biz.id))
            s.commit()
            await inter.response.send_message(f"✅ Bought **{biz.name}**. Balance: {bal.credits}.")

    @app_commands.command(name="business_my", description="Your owned businesses.")
    async def business_my(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            q = s.query(Ownership, Business).join(Business, Ownership.business_id == Business.id).filter(Ownership.user_id==inter.user.id).all()
            if not q:
                return await inter.response.send_message("You don't own any businesses.")
            lines = []
            now = dt.datetime.utcnow()
            for own, biz in q:
                hrs = (now - own.last_payout_at).total_seconds()/3600.0
                lines.append(f"- {biz.name} (accumulated ~{int(biz.hourly_yield*hrs)} credits since last payout)")
            await inter.response.send_message(embed=discord.Embed(title="📈 Your Businesses", description="\n".join(lines)))

    @tasks.loop(minutes=PAYOUT_INTERVAL_MIN)
    async def _payout_task(self):
        with self.bot.SessionLocal() as s:
            now = dt.datetime.utcnow()
            owns = s.query(Ownership).all()
            for own in owns:
                biz = s.get(Business, own.business_id)
                delta_hours = max(0.0, (now - own.last_payout_at).total_seconds()/3600.0)
                payout = int(biz.hourly_yield * delta_hours)
                if payout > 0:
                    _, bal = ensure_user(s, own.user_id)
                    bal.credits += payout
                    own.last_payout_at = now
            s.commit()

async def setup(bot: commands.Bot):
    await bot.add_cog(BusinessCog(bot))