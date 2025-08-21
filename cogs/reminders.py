# cogs/reminders.py
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy.orm import Session
import datetime as dt
import pytz

from db import Reminder  # your ORM Reminder model
from db import init_engine_and_session

# Hard-coded timezone
CENTRAL = pytz.timezone("America/Chicago")


class Reminders(commands.Cog):
    """Reminders with one-off and recurring support."""

    def __init__(self, bot: commands.Bot, SessionLocal):
        self.bot = bot
        self.SessionLocal = SessionLocal
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    # -------------------- background task --------------------
    @tasks.loop(seconds=30)
    async def check_reminders(self):
        now_utc = dt.datetime.utcnow().replace(tzinfo=pytz.UTC)
        with self.SessionLocal() as s:
            due = (
                s.query(Reminder)
                .filter(Reminder.due_at <= now_utc, Reminder.delivered == False)
                .all()
            )
            for rem in due:
                try:
                    channel = self.bot.get_channel(rem.channel_id)
                    if channel:
                        await channel.send(f"<@{rem.user_id}> ⏰ {rem.text}")
                    else:
                        user = await self.bot.fetch_user(rem.user_id)
                        if user:
                            await user.send(f"⏰ {rem.text}")

                    # Handle recurring
                    if rem.recur_interval:
                        rem.due_at = rem.due_at + dt.timedelta(
                            seconds=rem.recur_interval
                        )
                    else:
                        rem.delivered = True  # one-off -> mark delivered
                    s.commit()
                except Exception as e:
                    print("Error delivering reminder:", e)

            # Cleanup delivered one-offs
            s.query(Reminder).filter(
                Reminder.delivered == True, Reminder.recur_interval == None
            ).delete()
            s.commit()

    # -------------------- commands --------------------

    @app_commands.command(name="remind", description="Set a one-time reminder")
    @app_commands.describe(
        minutes="How many minutes from now?",
        text="What to remind you about?",
        dm="Send via DM instead of channel?",
    )
    async def remind(
        self,
        inter: discord.Interaction,
        minutes: int,
        text: str,
        dm: bool = False,
    ):
        due_local = dt.datetime.now(CENTRAL) + dt.timedelta(minutes=minutes)
        due_utc = due_local.astimezone(pytz.UTC)

        channel_id = inter.channel.id if not dm else None

        with self.SessionLocal() as s:
            rem = Reminder(
                user_id=inter.user.id,
                channel_id=channel_id if channel_id else inter.user.id,
                due_at=due_utc,
                text=text,
                delivered=False,
            )
            s.add(rem)
            s.commit()

        await inter.response.send_message(
            f"⏰ Reminder set for {due_local:%Y-%m-%d %I:%M %p %Z}: **{text}**",
            ephemeral=True,
        )

    @app_commands.command(
        name="remind_recur", description="Set a recurring reminder (interval minutes)"
    )
    async def remind_recur(
        self,
        inter: discord.Interaction,
        interval_minutes: int,
        text: str,
        dm: bool = False,
    ):
        due_local = dt.datetime.now(CENTRAL) + dt.timedelta(minutes=interval_minutes)
        due_utc = due_local.astimezone(pytz.UTC)

        channel_id = inter.channel.id if not dm else None

        with self.SessionLocal() as s:
            rem = Reminder(
                user_id=inter.user.id,
                channel_id=channel_id if channel_id else inter.user.id,
                due_at=due_utc,
                text=text,
                delivered=False,
                recur_interval=interval_minutes * 60,
            )
            s.add(rem)
            s.commit()

        await inter.response.send_message(
            f"🔁 Recurring reminder every {interval_minutes} minutes.\n"
            f"Next at {due_local:%Y-%m-%d %I:%M %p %Z}: **{text}**",
            ephemeral=True,
        )

    @app_commands.command(name="remind_list", description="List your reminders")
    async def remind_list(self, inter: discord.Interaction):
        with self.SessionLocal() as s:
            rems = (
                s.query(Reminder)
                .filter(Reminder.user_id == inter.user.id, Reminder.delivered == False)
                .order_by(Reminder.id.asc())
                .all()
            )
            if not rems:
                return await inter.response.send_message(
                    "You have no active reminders.", ephemeral=True
                )

            lines = []
            for r in rems:
                due_local = r.due_at.astimezone(CENTRAL)
                recur = f" (repeats every {r.recur_interval // 60}m)" if r.recur_interval else ""
                lines.append(
                    f"`{r.id}` ⏰ {due_local:%Y-%m-%d %I:%M %p %Z}: {r.text}{recur}"
                )

            await inter.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="remind_remove", description="Remove a reminder by ID")
    async def remind_remove(self, inter: discord.Interaction, reminder_id: int):
        with self.SessionLocal() as s:
            rem = (
                s.query(Reminder)
                .filter(Reminder.id == reminder_id, Reminder.user_id == inter.user.id)
                .first()
            )
            if not rem:
                return await inter.response.send_message(
                    "No such reminder.", ephemeral=True
                )
            s.delete(rem)
            s.commit()
        await inter.response.send_message(f"🗑️ Reminder {reminder_id} removed.", ephemeral=True)


async def setup(bot: commands.Bot):
    # reuse engine/session from db.py
    engine, SessionLocal = init_engine_and_session("data/bot.sqlite")
    await bot.add_cog(Reminders(bot, SessionLocal))