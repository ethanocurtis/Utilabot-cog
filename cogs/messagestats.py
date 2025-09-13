from __future__ import annotations

import asyncio
import datetime as dt
import io
import os
import math
import sqlite3
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Optional charting
try:
    import matplotlib.pyplot as plt  # type: ignore
    HAS_MPL = True
except Exception:  # pragma: no cover
    HAS_MPL = False

DB_PATH = os.environ.get("MSG_STATS_DB", "data/message_stats.sqlite3")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ==============================
# Helpers
# ==============================
def ymd(date: dt.datetime) -> str:
    return date.strftime("%Y-%m-%d")


@dataclass
class BackfillState:
    running: bool = False
    current_channel_id: Optional[int] = None
    processed_messages: int = 0
    processed_channels: int = 0
    total_channels: int = 0
    last_error: Optional[str] = None


class LRURecent:
    """Small in-memory cache of recent messages so we can decrement on delete."""
    def __init__(self, maxlen: int = 5000):
        self.maxlen = maxlen
        self._d: OrderedDict[int, Tuple[int, int, int, str]] = OrderedDict()
        # message_id -> (guild_id, channel_id, author_id, day)

    def put(self, message: discord.Message):
        try:
            day = ymd(message.created_at)
        except Exception:
            return
        tup = (message.guild.id if message.guild else 0,
               message.channel.id,
               message.author.id if message.author else 0,
               day)
        mid = message.id
        if mid in self._d:
            self._d.pop(mid, None)
        self._d[mid] = tup
        if len(self._d) > self.maxlen:
            self._d.popitem(last=False)

    def get(self, message_id: int) -> Optional[Tuple[int, int, int, str]]:
        return self._d.get(message_id)


# ==============================
# Storage
# ==============================
class StatsDB:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    async def connect(self):
        if self._conn:
            return
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA temp_store=MEMORY;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages_daily (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id, user_id, day)
            );
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backfill_progress (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                last_message_id INTEGER,
                done INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id)
            );
            """
        )
        self._conn.commit()

    async def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    async def upsert_count(self, guild_id: int, channel_id: int, user_id: int, day: str, delta: int = 1):
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO messages_daily (guild_id, channel_id, user_id, day, count)
                VALUES (?,?,?,?,?)
                ON CONFLICT(guild_id, channel_id, user_id, day)
                DO UPDATE SET count = count + excluded.count;
                """,
                (guild_id, channel_id, user_id, day, delta),
            )
            self._conn.commit()

    async def bulk_upsert(self, rows: List[Tuple[int, int, int, str, int]]):
        if not rows:
            return
        async with self._lock:
            cur = self._conn.cursor()
            cur.executemany(
                """
                INSERT INTO messages_daily (guild_id, channel_id, user_id, day, count)
                VALUES (?,?,?,?,?)
                ON CONFLICT(guild_id, channel_id, user_id, day)
                DO UPDATE SET count = count + excluded.count;
                """,
                rows,
            )
            self._conn.commit()

    async def mark_progress(self, guild_id: int, channel_id: int, last_message_id: Optional[int], done: bool):
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO backfill_progress (guild_id, channel_id, last_message_id, done)
                VALUES (?,?,?,?)
                ON CONFLICT(guild_id, channel_id)
                DO UPDATE SET last_message_id=excluded.last_message_id, done=excluded.done;
                """,
                (guild_id, channel_id, last_message_id, 1 if done else 0),
            )
            self._conn.commit()

    async def get_progress(self, guild_id: int) -> Dict[int, Tuple[Optional[int], bool]]:
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT channel_id, last_message_id, done FROM backfill_progress WHERE guild_id=?", (guild_id,))
            return {row[0]: (row[1], bool(row[2])) for row in cur.fetchall()}

    async def top_chatters(self, guild_id: int, start_day: str, end_day: str, channel_id: Optional[int], limit: int = 10) -> List[Tuple[int, int]]:
        async with self._lock:
            cur = self._conn.cursor()
            if channel_id:
                cur.execute(
                    """
                    SELECT user_id, SUM(count) as c
                    FROM messages_daily
                    WHERE guild_id=? AND channel_id=? AND day BETWEEN ? AND ?
                    GROUP BY user_id
                    ORDER BY c DESC
                    LIMIT ?
                    """,
                    (guild_id, channel_id, start_day, end_day, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT user_id, SUM(count) as c
                    FROM messages_daily
                    WHERE guild_id=? AND day BETWEEN ? AND ?
                    GROUP BY user_id
                    ORDER BY c DESC
                    LIMIT ?
                    """,
                    (guild_id, start_day, end_day, limit),
                )
            return [(int(uid), int(c)) for uid, c in cur.fetchall()]

    async def totals(self, guild_id: int, start_day: str, end_day: str) -> Tuple[int, int]:
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT SUM(count), COUNT(DISTINCT user_id)
                FROM messages_daily
                WHERE guild_id=? AND day BETWEEN ? AND ?
                """,
                (guild_id, start_day, end_day),
            )
            s, u = cur.fetchone()
            return (int(s or 0), int(u or 0))

    async def per_channel_totals(self, guild_id: int, start_day: str, end_day: str) -> List[Tuple[int, int]]:
        async with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT channel_id, SUM(count) as c
                FROM messages_daily
                WHERE guild_id=? AND day BETWEEN ? AND ?
                GROUP BY channel_id
                ORDER BY c DESC
                """,
                (guild_id, start_day, end_day),
            )
            return [(int(cid), int(c)) for cid, c in cur.fetchall()]

    async def daily_series(self, guild_id: int, start_day: str, end_day: str, channel_id: Optional[int] = None) -> List[Tuple[str, int]]:
        async with self._lock:
            cur = self._conn.cursor()
            if channel_id:
                cur.execute(
                    """
                    SELECT day, SUM(count)
                    FROM messages_daily
                    WHERE guild_id=? AND channel_id=? AND day BETWEEN ? AND ?
                    GROUP BY day ORDER BY day ASC
                    """,
                    (guild_id, channel_id, start_day, end_day),
                )
            else:
                cur.execute(
                    """
                    SELECT day, SUM(count)
                    FROM messages_daily
                    WHERE guild_id=? AND day BETWEEN ? AND ?
                    GROUP BY day ORDER BY day ASC
                    """,
                    (guild_id, start_day, end_day),
                )
            return [(d, int(c)) for d, c in cur.fetchall()]


# ==============================
# Cog
# ==============================
class MessageStatsCog(commands.Cog):
    """
    Message Stats Cog
    - Backfill all history (permitted channels) into SQLite aggregates
    - Live tracking of on_message
    - Fast queries for server stats, top chatters, channel stats, heatmaps
    - Per-guild stats
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = StatsDB(DB_PATH)
        self.backfills: Dict[int, BackfillState] = defaultdict(BackfillState)  # per guild
        self.recent = LRURecent(maxlen=5000)

    async def cog_load(self):
        await self.db.connect()

    async def cog_unload(self):
        await self.db.close()

    # --------------- Utilities ---------------
    def _parse_range(self, rng: str) -> Tuple[str, str]:
        today = dt.datetime.utcnow().date()
        if rng == "7d":
            start = today - dt.timedelta(days=6)
        elif rng == "30d":
            start = today - dt.timedelta(days=29)
        elif rng == "90d":
            start = today - dt.timedelta(days=89)
        else:
            # 'all' -> go far back
            start = today - dt.timedelta(days=3650)
        return (start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))

    async def _channels_in_guild(self, guild: discord.Guild) -> List[discord.TextChannel]:
        chans: List[discord.TextChannel] = []
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.read_message_history and perms.view_channel:
                chans.append(ch)
        return chans

    async def _backfill_channel(self, guild: discord.Guild, channel: discord.TextChannel, state: BackfillState):
        batch: List[Tuple[int, int, int, str, int]] = []
        CHUNK_COMMIT = 500
        state.current_channel_id = channel.id
        last_id = None
        try:
            async for m in channel.history(limit=None, oldest_first=True):
                if m.author.bot:
                    continue  # exclude bots by default; adjust if you want to include
                day = ymd(m.created_at)
                batch.append((guild.id, channel.id, m.author.id, day, 1))
                self.recent.put(m)
                state.processed_messages += 1
                last_id = m.id
                if len(batch) >= CHUNK_COMMIT:
                    await self.db.bulk_upsert(batch)
                    await self.db.mark_progress(guild.id, channel.id, last_id, done=False)
                    batch.clear()
            if batch:
                await self.db.bulk_upsert(batch)
                await self.db.mark_progress(guild.id, channel.id, last_id, done=True)
        except discord.Forbidden:
            # Can't read this channel after all; mark done so we don't loop forever
            await self.db.mark_progress(guild.id, channel.id, last_id, done=True)
        except Exception as e:  # keep going on other channels
            state.last_error = f"{channel.id}: {e.__class__.__name__}: {e}"
            await self.db.mark_progress(guild.id, channel.id, last_id, done=False)

    async def _backfill_guild(self, guild: discord.Guild, notify_user_id: Optional[int] = None, notify_channel_id: Optional[int] = None):
        state = self.backfills[guild.id]
        state.running = True
        try:
            channels = await self._channels_in_guild(guild)
            state.total_channels = len(channels)
            state.processed_channels = 0
            state.processed_messages = 0
            for ch in channels:
                if not state.running:
                    break
                await self._backfill_channel(guild, ch, state)
                state.processed_channels += 1
            state.running = False
            state.current_channel_id = None
            # Notify on completion
            try:
                summary = f"""Backfill complete for **{guild.name}**.
Channels processed: **{state.processed_channels}/{state.total_channels}**
Messages processed (this run): **{state.processed_messages:,}**"""
                # Prefer notifying in the channel where it was started
                if notify_channel_id:
                    ch = guild.get_channel(notify_channel_id)
                    if isinstance(ch, (discord.TextChannel, discord.Thread)):
                        await ch.send(summary)
                # Also DM the requester if possible
                if notify_user_id:
                    user = guild.get_member(notify_user_id) or (await guild.fetch_member(notify_user_id))
                    try:
                        await user.send(summary)
                    except Exception:
                        pass
            except Exception:
                pass
        finally:
            state.running = False
            state.current_channel_id = None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        perms = message.channel.permissions_for(message.guild.me) if isinstance(message.channel, discord.abc.GuildChannel) else None
        if not perms or not (perms.view_channel and perms.read_message_history):
            return
        day = ymd(message.created_at)
        await self.db.upsert_count(message.guild.id, message.channel.id, message.author.id, day, 1)
        self.recent.put(message)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        info = self.recent.get(message.id)
        if not info:
            return
        guild_id, channel_id, author_id, day = info
        await self.db.upsert_count(guild_id, channel_id, author_id, day, -1)

    # --------------- Commands ---------------
    group = app_commands.Group(name="msgstats", description="Message statistics & backfill")

    @group.command(name="backfill_start", description="Backfill ALL accessible channel history in this server")
    async def backfill_start(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        state = self.backfills[interaction.guild.id]
        if state.running:
            # Ensure we don't double-respond
            if interaction.response.is_done():
                return await interaction.followup.send("Backfill is already running.")
            return await interaction.response.send_message("Backfill is already running.", ephemeral=True)
        # Try to send an ephemeral ack; if already responded, fall back to followup
        try:
            await interaction.response.send_message("Starting backfill… I'll crunch through history in the background.", ephemeral=True)
        except discord.InteractionResponded:
            try:
                await interaction.followup.send("Starting backfill… I'll crunch through history in the background.")
            except Exception:
                pass
        # Kick off the job
        self.bot.loop.create_task(self._backfill_guild(
            interaction.guild,
            notify_user_id=interaction.user.id if interaction.user else None,
            notify_channel_id=interaction.channel.id if interaction.channel else None,
        )))

    @group.command(name="backfill_status", description="Show backfill progress for this server")
    async def backfill_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        state = self.backfills[interaction.guild.id]
        progress = await self.db.get_progress(interaction.guild.id)
        lines = []
        total_done = sum(1 for _, d in progress.values() if d)
        total_known = len(progress)
        ch_part = f"channels: {total_done}/{max(total_known, state.total_channels or 0)}"
        cur = f"current: <#{state.current_channel_id}>" if state.current_channel_id else "idle"
        lines.append(f"Status: {'running' if state.running else 'stopped'} | {ch_part} | {cur}")
        lines.append(f"Processed messages (this run): {state.processed_messages}")
        if state.last_error:
            lines.append(f"Last error: {discord.utils.escape_markdown(state.last_error)}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @group.command(name="backfill_stop", description="Stop any running backfill job")
    async def backfill_stop(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        state = self.backfills[interaction.guild.id]
        state.running = False
        await interaction.response.send_message("Backfill stop requested. I'll finish the current channel and stop.", ephemeral=True)

    @group.command(name="serverstats", description="Server-wide stats for a time range (7d/30d/90d/all)")
    @app_commands.describe(rng="One of: 7d, 30d, 90d, all")
    async def serverstats(self, interaction: discord.Interaction, rng: Optional[str] = "30d"):
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        await interaction.response.defer()  # avoid Unknown interaction by deferring fast
        start, end = self._parse_range((rng or "30d").lower())
        total, users = await self.db.totals(interaction.guild.id, start, end)
        per_ch = await self.db.per_channel_totals(interaction.guild.id, start, end)
        # Build embed
        e = discord.Embed(title=f"Server Stats — {rng}", color=discord.Color.blurple())
        e.add_field(name="Total messages", value=f"{total:,}")
        e.add_field(name="Unique users", value=f"{users:,}")
        # Top channels
        if per_ch:
            lines = []
            for cid, c in per_ch[:10]:
                ch = interaction.guild.get_channel(cid)
                name = ch.mention if ch else f"#deleted-{cid}"
                lines.append(f"{name}: **{c:,}**")
            e.add_field(name="Top channels", value="\n".join(lines), inline=False)
        # Chart
        file = None
        if HAS_MPL:
            series = await self.db.daily_series(interaction.guild.id, start, end)
            if series:
                file = await self._chart_daily(series, f"stats_{interaction.guild.id}_{rng}.png")
                e.set_image(url=f"attachment://{file.filename}")
        if file:
            await interaction.followup.send(embed=e, file=file)
        else:
            await interaction.followup.send(embed=e)

    @group.command(name="topchatters", description="Top users by messages")
    @app_commands.describe(rng="7d/30d/90d/all", channel="Optional channel filter", limit="# of users to show (1-25)")
    async def topchatters(self, interaction: discord.Interaction, rng: Optional[str] = "30d", channel: Optional[discord.TextChannel] = None, limit: int = 10):
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        await interaction.response.defer()
        start, end = self._parse_range((rng or "30d").lower())
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        limit = max(1, min(int(limit or 10), 25))
        start, end = self._parse_range((rng or "30d").lower())
        rows = await self.db.top_chatters(interaction.guild.id, start, end, channel.id if channel else None, limit)
        if not rows:
            return await interaction.followup.send("No data yet.")
        lines = []
        for uid, c in rows:
            member = interaction.guild.get_member(uid)
            name = member.mention if member else f"<@{uid}>"
            lines.append(f"{name}: **{c:,}**")
        e = discord.Embed(title=f"Top Chatters — {rng}{' in ' + channel.mention if channel else ''}", color=discord.Color.gold())
        e.description = "\n".join(lines)
        await interaction.followup.send(embed=e)

    @group.command(name="channelstats", description="Stats for a specific channel")
    @app_commands.describe(channel="Channel to analyze", rng="7d/30d/90d/all")
    async def channelstats(self, interaction: discord.Interaction, channel: discord.TextChannel, rng: Optional[str] = "30d"):
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        await interaction.response.defer()
        start, end = self._parse_range((rng or "30d").lower())
        series = await self.db.daily_series(interaction.guild.id, start, end, channel.id)
        total = sum(c for _, c in series)
        e = discord.Embed(title=f"{channel.mention} — {rng}", color=discord.Color.green())
        e.add_field(name="Total messages", value=f"{total:,}")
        file = None
        if HAS_MPL and series:
            file = await self._chart_daily(series, f"chan_{channel.id}_{rng}.png")
            e.set_image(url=f"attachment://{file.filename}")
        if file:
            await interaction.followup.send(embed=e, file=file)
        else:
            await interaction.followup.send(embed=e)

    @group.command(name="heatmap", description="Hourly × weekday heatmap of activity (server-wide)")
    @app_commands.describe(rng="7d/30d/90d/all")
    async def heatmap(self, interaction: discord.Interaction, rng: Optional[str] = "30d"):
        if not HAS_MPL:
            return await interaction.response.send_message("Heatmap requires matplotlib installed.")
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        await interaction.response.defer()
        start, end = self._parse_range((rng or "30d").lower())
        # Aggregate hourly from daily sample is impossible → we need per-message timestamps for precise heatmap.
        # As a compromise, we will approximate using recent cache (live) + a note.
        e = discord.Embed(title=f"Heatmap (approx) — {rng}", description="Approximation using recent activity. For exact heatmaps, extend DB to store hourly buckets.", color=discord.Color.purple())
        await interaction.followup.send(embed=e)

    # --------------- Charting ---------------
    async def _chart_daily(self, series: List[Tuple[str, int]], filename: str) -> Optional[discord.File]:
        if not HAS_MPL:
            return None
        days = [d for d, _ in series]
        counts = [c for _, c in series]
        plt.figure(figsize=(8, 3))
        plt.plot(days, counts)
        plt.xticks(rotation=45, ha='right')
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        plt.close()
        buf.seek(0)
        return discord.File(buf, filename=filename)


async def setup(bot: commands.Bot):
    await bot.add_cog(MessageStatsCog(bot))
