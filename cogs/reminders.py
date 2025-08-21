# cogs/reminders.py
import datetime as dt
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Timezone utils: prefer zoneinfo; fall back to pytz
try:
    from zoneinfo import ZoneInfo  # py3.9+
    TZ_CENTRAL = ZoneInfo("America/Chicago")
    TZ_UTC = ZoneInfo("UTC")

    def _aware_in_central(y, m, d, hh, mm):
        return dt.datetime(y, m, d, hh, mm, tzinfo=TZ_CENTRAL)

    def _utc_naive_to_central(utc_naive: dt.datetime) -> dt.datetime:
        # interpret naive as UTC, convert to Central (aware)
        return utc_naive.replace(tzinfo=TZ_UTC).astimezone(TZ_CENTRAL)

    def _central_to_utc_naive(local_aware: dt.datetime) -> dt.datetime:
        # take aware Central -> naive UTC for DB storage
        return local_aware.astimezone(TZ_UTC).replace(tzinfo=None)

except Exception:  # pragma: no cover
    import pytz
    TZ_CENTRAL = pytz.timezone("America/Chicago")
    TZ_UTC = pytz.UTC

    def _aware_in_central(y, m, d, hh, mm):
        return TZ_CENTRAL.localize(dt.datetime(y, m, d, hh, mm))

    def _utc_naive_to_central(utc_naive: dt.datetime) -> dt.datetime:
        return TZ_CENTRAL.normalize(TZ_UTC.localize(utc_naive).astimezone(TZ_CENTRAL))

    def _central_to_utc_naive(local_aware: dt.datetime) -> dt.datetime:
        return local_aware.astimezone(TZ_UTC).replace(tzinfo=None)


class Reminders(commands.Cog):
    """
    Reminders with persistence and America/Chicago user-facing times.

    - One-shot reminders are DELETED after firing.
    - Recurring reminders reschedule themselves (interval + unit).
    - Per-user 'dm_all' preference stored in user_notes_kv via bot.store.get/set_config.
    - Stores due_at in DB as a naive UTC ISO string (back-compat).
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.store.db  # SQLAlchemy engine from WxStore
        self._ensure_table()
        self._cleanup_legacy()
        self.loop_check.start()

    def cog_unload(self):
        self.loop_check.cancel()

    # ---------- DB bootstrap ----------
    def _ensure_table(self):
        with self.db.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    dm INTEGER DEFAULT 0,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,   -- naive UTC ISO
                    interval INTEGER,       -- NULL for one-shot
                    unit TEXT,              -- minutes|hours|days|weeks; NULL for one-shot
                    delivered INTEGER DEFAULT 0
                )
                """
            )
            # Add columns if migrating from older schema (no-op if exist)
            for sql in (
                "ALTER TABLE reminders ADD COLUMN dm INTEGER DEFAULT 0",
                "ALTER TABLE reminders ADD COLUMN interval INTEGER",
                "ALTER TABLE reminders ADD COLUMN unit TEXT",
                "ALTER TABLE reminders ADD COLUMN delivered INTEGER DEFAULT 0",
            ):
                try:
                    conn.exec_driver_sql(sql)
                except Exception:
                    pass

    def _cleanup_legacy(self):
        # Remove old delivered one-shots from very early versions
        with self.db.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "DELETE FROM reminders WHERE delivered=1 AND (interval IS NULL OR unit IS NULL)"
                )
            except Exception:
                pass

    # ---------- low-level ops ----------
    def _add_reminder(
        self,
        user_id: int,
        channel_id: Optional[int],
        dm: bool,
        text_: str,
        due_utc_naive: dt.datetime,
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
                    "due": due_utc_naive.isoformat(),
                    "i": interval,
                    "unit": unit,
                },
            )
            return res.lastrowid

    def _list_reminders(self, user_id: int):
        with self.db.connect() as conn:
            return conn.exec_driver_sql(
                "SELECT id, text, due_at, interval, unit, dm "
                "FROM reminders WHERE user_id=:u ORDER BY id ASC",
                {"u": user_id},
            ).fetchall()

    def _remove_reminder(self, user_id: int, rid: int) -> bool:
        with self.db.begin() as conn:
            res = conn.exec_driver_sql(
                "DELETE FROM reminders WHERE id=:i AND user_id=:u",
                {"i": rid, "u": user_id},
            )
            return res.rowcount > 0

    def _delete_by_id(self, rid: int):
        with self.db.begin() as conn:
            conn.exec_driver_sql("DELETE FROM reminders WHERE id=:i", {"i": rid})

    def _get_due(self):
        now_iso = dt.datetime.utcnow().isoformat()
        with self.db.connect() as conn:
            return conn.exec_driver_sql(
                "SELECT id, user_id, channel_id, dm, text, due_at, interval, unit "
                "FROM reminders WHERE due_at<=:now",
                {"now": now_iso},
            ).fetchall()

    def _resched(self, rid: int, new_due_utc_naive: dt.datetime):
        with self.db.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE reminders SET due_at=:d WHERE id=:i",
                {"d": new_due_utc_naive.isoformat(), "i": rid},
            )

    # ---------- scheduler ----------
    @tasks.loop(seconds=15.0)
    async def loop_check(self):
        for rid, user_id, channel_id, dm, text_, due_iso, interval, unit in self._get_due():
            user_id = int(user_id)
            user = self.bot.get_user(user_id)
            channel = self.bot.get_channel(int(channel_id)) if channel_id else None

            # Where to send?
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

            # Send (mention user)
            if dest:
                try:
                    await dest.send(f"<@{user_id}> ⏰ {text_}")
                except Exception:
                    pass

            # Recurring vs one-shot
            if interval and unit:
                base = dt.datetime.fromisoformat(due_iso)  # naive UTC
                next_due = self._advance(base, int(interval), str(unit))
                self._resched(rid, next_due)
            else:
                self._delete_by_id(rid)

    # ---------- utils ----------
    async def _get_dm_all(self, user_id: int) -> bool:
        try:
            val = self.bot.store.get_config(f"reminder:dm_all:{user_id}")
            return str(val).lower() in ("1", "true", "yes", "on")
        except Exception:
            return False

    async def _set_dm_all(self, user_id: int, value: bool):
        self.bot.store.set_config(f"reminder:dm_all:{user_id}", "1" if value else "0")

    @staticmethod
    def _advance(base_utc_naive: dt.datetime, interval: int, unit: str) -> dt.datetime:
        if unit == "minutes":
            return base_utc_naive + dt.timedelta(minutes=interval)
        if unit == "hours":
            return base_utc_naive + dt.timedelta(hours=interval)
        if unit == "days":
            return base_utc_naive + dt.timedelta(days=interval)
        if unit == "weeks":
            return base_utc_naive + dt.timedelta(weeks=interval)
        return base_utc_naive

    @staticmethod
    def _parse_duration(s: str) -> Optional[dt.timedelta]:
        import re
        s = s.strip().lower()
        m = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s)
        if not m:
            return None
        w, d, h, m_, s_ = (int(x) if x else 0 for x in m.groups())
        if not any((w, d, h, m_, s_)):
            return None
        return dt.timedelta(weeks=w, days=d, hours=h, minutes=m_, seconds=s_)

    # ---------- commands ----------
    group = app_commands.Group(name="remind", description="Reminder commands")

    @group.command(name="settings", description="Configure reminder settings")
    @app_commands.describe(dm_all="If true, all reminders DM you")
    async def settings(self, inter: discord.Interaction, dm_all: bool):
        await self._set_dm_all(inter.user.id, dm_all)
        await inter.response.send_message(f"✅ dm_all set to {dm_all}", ephemeral=True)

    @group.command(name="in", description="Remind you after a duration")
    @app_commands.describe(
        duration="e.g. 10m, 2h30m, 1d, 1w2d",
        text="What to remind you about",
        dm="Force DM this reminder",
    )
    async def remind_in(self, inter: discord.Interaction, duration: str, text: str, dm: Optional[bool] = False):
        td = self._parse_duration(duration)
        if not td:
            return await inter.response.send_message("Invalid duration. Examples: 10m, 2h30m, 1d, 1w2d", ephemeral=True)
        due_utc_naive = dt.datetime.utcnow() + td  # store as naive UTC
        rid = self._add_reminder(inter.user.id, inter.channel.id, bool(dm), text, due_utc_naive)
        due_local = _utc_naive_to_central(due_utc_naive)
        await inter.response.send_message(
            f"⏰ Saved `{rid:04d}` for {due_local:%Y-%m-%d %I:%M %p %Z}.",
            ephemeral=True,
        )

    @group.command(name="at", description="Remind you at a specific time (America/Chicago)")
    @app_commands.describe(
        when="HH:MM or YYYY-MM-DD HH:MM (America/Chicago)",
        text="What to remind you about",
        dm="Force DM this reminder",
    )
    async def remind_at(self, inter: discord.Interaction, when: str, text: str, dm: Optional[bool] = False):
        try:
            when = when.strip()
            if len(when) <= 5 and ":" in when:  # HH:MM today/tomorrow (Central)
                hh, mm = map(int, when.split(":"))
                now_local = _utc_naive_to_central(dt.datetime.utcnow())
                due_local = _aware_in_central(now_local.year, now_local.month, now_local.day, hh, mm)
                # if past for today, move to tomorrow
                if due_local <= now_local.replace(tzinfo=due_local.tzinfo):
                    tmr = now_local + dt.timedelta(days=1)
                    due_local = _aware_in_central(tmr.year, tmr.month, tmr.day, hh, mm)
            else:
                # YYYY-MM-DD HH:MM in Central
                date_part, time_part = when.replace("T", " ").split()
                y, m, d = map(int, date_part.split("-"))
                hh, mm = map(int, time_part.split(":")[:2])
                due_local = _aware_in_central(y, m, d, hh, mm)
        except Exception:
            return await inter.response.send_message(
                "Invalid time. Use HH:MM or YYYY-MM-DD HH:MM (America/Chicago).",
                ephemeral=True,
            )

        due_utc_naive = _central_to_utc_naive(due_local)
        rid = self._add_reminder(inter.user.id, inter.channel.id, bool(dm), text, due_utc_naive)
        await inter.response.send_message(
            f"⏰ Saved `{rid:04d}` for {due_local:%Y-%m-%d %I:%M %p %Z}.",
            ephemeral=True,
        )

    @group.command(name="every", description="Recurring reminder")
    @app_commands.describe(
        interval="Repeat interval as a number",
        unit="Interval unit",
        text="What to remind you about",
        start_in="Delay before the first one (e.g. 10m, 2h, 1d)",
        dm="Force DM this reminder",
    )
    async def remind_every(
        self,
        inter: discord.Interaction,
        interval: int,
        unit: Literal["minutes", "hours", "days", "weeks"],
        text: str,
        start_in: Optional[str] = None,
        dm: Optional[bool] = False,
    ):
        if start_in:
            td = self._parse_duration(start_in)
            if not td:
                return await inter.response.send_message("Invalid start_in format. Examples: 10m, 2h, 1d", ephemeral=True)
            due_utc_naive = dt.datetime.utcnow() + td
        else:
            due_utc_naive = self._advance(dt.datetime.utcnow(), interval, unit)

        rid = self._add_reminder(inter.user.id, inter.channel.id, bool(dm), text, due_utc_naive, interval, unit)
        due_local = _utc_naive_to_central(due_utc_naive)
        await inter.response.send_message(
            f"🔁 Saved `{rid:04d}` every {interval} {unit}, first at {due_local:%Y-%m-%d %I:%M %p %Z}.",
            ephemeral=True,
        )

    @group.command(name="list", description="List your reminders (America/Chicago, lowest ID first)")
    async def remind_list(self, inter: discord.Interaction):
        rows = self._list_reminders(inter.user.id)
        if not rows:
            return await inter.response.send_message("You have no reminders.", ephemeral=True)

        embed = discord.Embed(
            title="⏰ Your Reminders",
            description="Times shown in America/Chicago",
            color=discord.Color.blurple()
        )

        for rid, text_, due_iso, interval, unit, dm in rows:
            due_local = _utc_naive_to_central(dt.datetime.fromisoformat(due_iso))
            tag = "📩 DM" if dm else "💬 Channel"
            cadence = f"\n🔁 Every {interval} {unit}" if interval and unit else ""
            embed.add_field(
                name=f"#{rid:04d} — {due_local:%Y-%m-%d %I:%M %p %Z}",
                value=f"{tag}{cadence}\n**{text_}**",
                inline=False
            )

        await inter.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="remove", description="Remove a reminder by ID")
    async def remind_remove(self, inter: discord.Interaction, reminder_id: int):
        ok = self._remove_reminder(inter.user.id, reminder_id)
        if ok:
            await inter.response.send_message(f"🗑️ Removed `{reminder_id:04d}`", ephemeral=True)
        else:
            await inter.response.send_message("Reminder not found.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))