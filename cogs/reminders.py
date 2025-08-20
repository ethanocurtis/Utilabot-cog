import datetime as dt
import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy.orm import Session
from utils.db import Reminder

class RemindersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._scan.start()

    def cog_unload(self):
        if self._scan.is_running():
            self._scan.cancel()

    @app_commands.command(name="remindme", description="Remind you in N minutes.")
    async def remindme(self, inter: discord.Interaction, minutes: int, text: str):
        due = dt.datetime.utcnow() + dt.timedelta(minutes=minutes)
        with self.bot.SessionLocal() as s:
            s.add(Reminder(user_id=inter.user.id, channel_id=inter.channel.id, due_at=due, text=text))
            s.commit()
        await inter.response.send_message(f"⏰ I'll remind you in {minutes} minutes.")

    @tasks.loop(seconds=30)
    async def _scan(self):
        now = dt.datetime.utcnow()
        with self.bot.SessionLocal() as s:
            q = s.query(Reminder).filter(Reminder.delivered==False, Reminder.due_at <= now).all()
            for r in q:
                ch = self.bot.get_channel(r.channel_id)
                if ch:
                    try:
                        await ch.send(f"⏰ <@{r.user_id}> Reminder: {r.text}")
                        r.delivered = True
                    except Exception:
                        pass
            s.commit()

async def setup(bot: commands.Bot):
    await bot.add_cog(RemindersCog(bot))
