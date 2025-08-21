
import asyncio
import datetime as dt
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

class Reminders(commands.Cog):
    """
    Reminder system with persistence across restarts.
    - One-shot reminders are removed from the DB after firing.
    - Recurring reminders reschedule themselves.
    - Per-user dm_all setting stored in user_notes_kv (via bot.store.set_config/get_config).
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.store.db  # SQLAlchemy engine
        self._ensure_table_and_columns()
        self._startup_cleanup()
        self.loop_check.start()

    def cog_unload(self):
        self.loop_check.cancel()

    # ---------------- DB bootstrap ----------------

    def _ensure_table_and_columns(self):
        with self.db.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    dm INTEGER DEFAULT 0,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    interval INTEGER,
                    unit TEXT,
                    delivered INTEGER DEFAULT 0
                )
                """
            )
            # Add columns if migrating from older schema
            for sql in (
                "ALTER TABLE reminders ADD COLUMN dm INTEGER DEFAULT 0",
                "ALTER TABLE reminders ADD COLUMN interval INTEGER",
                "ALTER TABLE reminders ADD COLUMN unit TEXT",
                "ALTER TABLE reminders ADD COLUMN delivered INTEGER DEFAULT 0",
            ):
                try:
                    conn.exec_driver_sql(sql)
                except Exception:
                    pass  # already exists

    def _startup_cleanup(self):
        # Delete any legacy one-shots that were marked delivered previously
        with self.db.begin() as conn:
            try:
                conn.exec_driver_sql("DELETE FROM reminders WHERE delivered=1 AND (interval IS NULL OR unit IS NULL)")
            except Exception:
                pass

    # ---------------- low-level ops ----------------

    def _add_reminder(
        self,
        user_id: int,
        channel_id: Optional[int],
        dm: bool,
        text_: str,
        due_at: dt.datetime,
        interval: Optional[int] = None,
        unit: Optional[str] = None,
    ) -> int:
        with self.db.begin() as conn:
            res = conn.exec_driver_sql(
                """
                INSERT INTO reminders(user_id, channel_id, dm, text, due_at, interval, unit, delivered)
                VALUES (:u, :c, :d, :t, :due, :i, :unit, 0)
                """,
                {
                    "u": user_id,
                    "c": channel_id,
                    "d": 1 if dm else 0,
                    "t": text_,
                    "due": due_at.replace(tzinfo=None).isoformat(),
                    "i": interval,
                    "unit": unit,
                },
            )
            return res.lastrowid

    def _list_reminders(self, user_id: int):
        with self.db.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT id, text, due_at, interval, unit, dm FROM reminders WHERE user_id=:u ORDER BY id ASC",
                {"u": user_id},
            ).fetchall()
        return rows

    def _remove_reminder(self, user_id: int, rid: int) -> bool:
        with self.db.begin() as conn:
            res = conn.exec_driver_sql(
                "DELETE FROM reminders WHERE id=:i AND user_id=:u",
                {"i": rid, "u": user_id},
            )
            return res.rowcount > 0

    def _delete_reminder(self, rid: int):
        with self.db.begin() as conn:
            conn.exec_driver_sql("DELETE FROM reminders WHERE id=:i", {"i": rid})

    def _get_due(self):
        now = dt.datetime.utcnow().isoformat()
        with self.db.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT id, user_id, channel_id, dm, text, due_at, interval, unit "
                "FROM reminders WHERE due_at<=:now",
                {"now": now},
            ).fetchall()
        return rows

    def _reschedule(self, rid: int, new_due: dt.datetime):
        with self.db.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE reminders SET due_at=:d WHERE id=:i",
                {"d": new_due.replace(tzinfo=None).isoformat(), "i": rid},
            )

    # ---------------- scheduler loop ----------------

    @tasks.loop(seconds=15.0)
    async def loop_check(self):
        # Pull due reminders and dispatch.
        rows = self._get_due()
        for rid, user_id, channel_id, dm, text_, due_at, interval, unit in rows:
            user_id = int(user_id)
            mention = f"<@{user_id}>"
            user = self.bot.get_user(user_id)
            channel = self.bot.get_channel(int(channel_id)) if channel_id else None

            # Decide destination
            dest = None
            dm_all = await self._get_dm_all(user_id)
            if dm or dm_all:
                if user:
                    try:
                        dest = await user.create_dm()
                    except Exception:
                        dest = None
            if not dest and channel:
                dest = channel

            # Send with mention
            if dest:
                try:
                    await dest.send(f"{mention} ⏰ {text_}")
                except Exception:
                    pass

            # Advance recurring, delete one-shot
            if interval and unit:
                base = dt.datetime.fromisoformat(due_at)
                new_due = self._advance_due(base, int(interval), str(unit))
                self._reschedule(rid, new_due)
            else:
                self._delete_reminder(rid)

    # ---------------- utils ----------------

    async def _get_dm_all(self, user_id: int) -> bool:
        try:
            row = self.bot.store.get_config(f"reminder:dm_all:{user_id}")
            return str(row).lower() in ("1", "true", "yes", "on")
        except Exception:
            return False

    async def _set_dm_all(self, user_id: int, value: bool):
        self.bot.store.set_config(f"reminder:dm_all:{user_id}", "1" if value else "0")

    @staticmethod
    def _advance_due(base: dt.datetime, interval: int, unit: str) -> dt.datetime:
        if unit == "minutes":
            return base + dt.timedelta(minutes=interval)
        if unit == "hours":
            return base + dt.timedelta(hours=interval)
        if unit == "days":
            return base + dt.timedelta(days=interval)
        if unit == "weeks":
            return base + dt.timedelta(weeks=interval)
        return base

    @staticmethod
    def _parse_duration(s: str) -> Optional[dt.timedelta]:
        import re
        s = s.strip().lower()
        # allow "1h30m", "2d", "45s", "1w2d3h4m5s"
        pattern = re.compile(r"^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
        m = pattern.fullmatch(s)
        if not m:
            return None
        w, d, h, m_, s_ = (int(x) if x else 0 for x in m.groups())
        if not any((w, d, h, m_, s_)):
            return None
        return dt.timedelta(weeks=w, days=d, hours=h, minutes=m_, seconds=s_)

    # ---------------- commands ----------------

    group = app_commands.Group(name="remind", description="Reminder commands")

    @group.command(name="settings", description="Configure reminder settings")
    @app_commands.describe(dm_all="If true, all reminders DM you")
    async def settings(self, inter: discord.Interaction, dm_all: bool):
        await self._set_dm_all(inter.user.id, dm_all)
        await inter.response.send_message(f"✅ dm_all set to {dm_all}", ephemeral=True)

    @group.command(name="in", description="Remind you after a duration")
    @app_commands.describe(duration="e.g. 10m, 2h30m, 1d, 1w2d", text="What to remind you about", dm="Force DM this reminder")
    async def remind_in(self, inter: discord.Interaction, duration: str, text: str, dm: Optional[bool] = False):
        td = self._parse_duration(duration)
        if not td:
            return await inter.response.send_message("Invalid duration. Examples: 10m, 2h30m, 1d, 1w2d", ephemeral=True)
        due = dt.datetime.utcnow() + td
        rid = self._add_reminder(inter.user.id, inter.channel.id, bool(dm), text, due)
        await inter.response.send_message(f"⏰ Saved `{rid:04d}` for {due:%Y-%m-%d %H:%M} UTC.", ephemeral=True)

    @group.command(name="at", description="Remind you at a specific UTC time")
    @app_commands.describe(when="HH:MM or YYYY-MM-DD HH:MM (UTC)", text="What to remind you about", dm="Force DM this reminder")
    async def remind_at(self, inter: discord.Interaction, when: str, text: str, dm: Optional[bool] = False):
        try:
            when = when.strip()
            if len(when) <= 5 and ":" in when:  # HH:MM today/tomorrow
                hh, mm = map(int, when.split(":"))
                now = dt.datetime.utcnow()
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due <= now:
                    due += dt.timedelta(days=1)
            else:
                # Allow "YYYY-MM-DD HH:MM" or full ISO
                when_norm = when.replace("T", " ")
                due = dt.datetime.fromisoformat(when_norm)
        except Exception:
            return await inter.response.send_message("Invalid time. Use HH:MM or YYYY-MM-DD HH:MM", ephemeral=True)

        rid = self._add_reminder(inter.user.id, inter.channel.id, bool(dm), text, due)
        await inter.response.send_message(f"⏰ Saved `{rid:04d}` for {due:%Y-%m-%d %H:%M} UTC.", ephemeral=True)

    @group.command(name="every", description="Recurring reminder")
    @app_commands.describe(interval="Repeat interval as a number", unit="Interval unit", text="What to remind you about", start_in="Delay before the first one", dm="Force DM this reminder")
    async def remind_every(
        self,
        inter: discord.Interaction,
        interval: int,
        unit: Literal["minutes", "hours", "days", "weeks"],
        text: str,
        start_in: Optional[str] = None,
        dm: Optional[bool] = False,
    ):
        due = dt.datetime.utcnow()
        if start_in:
            td = self._parse_duration(start_in)
            if not td:
                return await inter.response.send_message("Invalid start_in format. Examples: 10m, 2h, 1d", ephemeral=True)
            due += td
        else:
            due = self._advance_due(due, interval, unit)
        rid = self._add_reminder(inter.user.id, inter.channel.id, bool(dm), text, due, interval, unit)
        await inter.response.send_message(f"🔁 Saved `{rid:04d}` every {interval} {unit}, first at {due:%Y-%m-%d %H:%M} UTC.", ephemeral=True)

    @group.command(name="list", description="List your reminders (lowest ID first)")
    async def remind_list(self, inter: discord.Interaction):
        rows = self._list_reminders(inter.user.id)
        if not rows:
            return await inter.response.send_message("You have no reminders.", ephemeral=True)
        # Clean, single-line per reminder: `0001` [DM] due 2025-08-21 18:00 UTC · every 2 hours — text
        lines = []
        for rid, text_, due_at, interval, unit, dm in rows:
            tag = "DM" if dm else "CHAN"
            recur = f" · every {interval} {unit}" if interval and unit else ""
            lines.append(f"`{rid:04d}` [{tag}] due {due_at} UTC{recur} — {text_}")
        await inter.response.send_message("**Your reminders:**\n" + "\n".join(lines), ephemeral=True)

    @group.command(name="remove", description="Remove a reminder by ID")
    async def remind_remove(self, inter: discord.Interaction, reminder_id: int):
        ok = self._remove_reminder(inter.user.id, reminder_id)
        if ok:
            await inter.response.send_message(f"🗑️ Removed `{reminder_id:04d}`", ephemeral=True)
        else:
            await inter.response.send_message("Reminder not found.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
