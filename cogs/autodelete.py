# cogs/autodelete.py
from __future__ import annotations
import asyncio
import datetime as dt
import re
from typing import Optional, Dict, List, Set

import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy import text

MIN_SECONDS = 1
MAX_SECONDS = 7 * 24 * 3600  # 7 days

# ---------- SQL (auto-migrates missing columns) ----------
CREATE_RULES_SQL = """
CREATE TABLE IF NOT EXISTS autodelete_rules (
  channel_id INTEGER PRIMARY KEY,
  seconds INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  users_mode TEXT NOT NULL DEFAULT 'all',            -- 'all' | 'only' | 'except'
  users_csv  TEXT NOT NULL DEFAULT '',               -- comma-separated user IDs
  updated_at TEXT NOT NULL
);
"""
ALTER_ADD_USERS_MODE = "ALTER TABLE autodelete_rules ADD COLUMN users_mode TEXT NOT NULL DEFAULT 'all';"
ALTER_ADD_USERS_CSV  = "ALTER TABLE autodelete_rules ADD COLUMN users_csv  TEXT NOT NULL DEFAULT '';"

# ---------- utils ----------
DUR_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s?)?\s*$", re.I)

def parse_duration(s: str) -> Optional[int]:
    s = s.strip().lower()
    if s.isdigit():
        sec = int(s)
        return max(MIN_SECONDS, min(MAX_SECONDS, sec))
    m = DUR_RE.match(s)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mm = int(m.group(2) or 0)
    ss = int(m.group(3) or 0)
    sec = h * 3600 + mm * 60 + ss
    if sec <= 0:
        return None
    return max(MIN_SECONDS, min(MAX_SECONDS, sec))

def pretty_duration(sec: int) -> str:
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return "".join(parts)

def parse_user_list(input_text: str) -> List[int]:
    ids: List[int] = []
    for token in re.split(r"[,\s]+", input_text.strip()):
        if not token:
            continue
        m = re.match(r"<@!?(?P<i>\d+)>", token)
        if m:
            ids.append(int(m.group("i")))
            continue
        if token.isdigit():
            ids.append(int(token))
    return list(dict.fromkeys(ids))

# ---------- UI Modals ----------
class SecondsModal(discord.ui.Modal, title="Set Auto-Delete Duration"):
    def __init__(self, view: "AutoDeleteView"):
        super().__init__(timeout=180)
        self.view_ref = view
        self.input = discord.ui.TextInput(
            label="Duration (e.g. 10s, 20m, 1h, 1h20m10s)",
            placeholder="e.g. 45s",
            max_length=20,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        sec = parse_duration(str(self.input.value))
        if sec is None:
            return await interaction.response.send_message("❌ Invalid duration.", ephemeral=True)

        ch = self.view_ref.channel
        cog: AutoDeleteCog = self.view_ref.cog
        await cog._set_rule(ch.id, seconds=sec, enabled=True)

        await interaction.response.defer(ephemeral=True)
        await self.view_ref.refresh(interaction)
        await interaction.followup.send(f"✅ Duration set to **{pretty_duration(sec)}** for {ch.mention}.", ephemeral=True)

class UsersFilterModal(discord.ui.Modal, title="User Filter"):
    def __init__(self, view: "AutoDeleteView", mode: str, users_csv: str):
        super().__init__(timeout=180)
        self.view_ref = view
        self.mode_in = discord.ui.TextInput(
            label="Mode ('all', 'only', 'except')",
            default=mode,
            max_length=10,
            required=True,
        )
        self.users_in = discord.ui.TextInput(
            label="Users (IDs or @mentions, comma/space separated)",
            default=users_csv,
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )
        self.add_item(self.mode_in)
        self.add_item(self.users_in)

    async def on_submit(self, interaction: discord.Interaction):
        mode = str(self.mode_in.value).strip().lower()
        if mode not in ("all", "only", "except"):
            return await interaction.response.send_message("❌ Mode must be 'all', 'only', or 'except'.", ephemeral=True)

        ids = parse_user_list(str(self.users_in.value))
        users_csv = ",".join(str(i) for i in ids)
        await self.view_ref.cog._set_filter(self.view_ref.channel.id, mode=mode, users_csv=users_csv)

        await interaction.response.defer(ephemeral=True)
        await self.view_ref.refresh(interaction)
        nice = "(none)" if not ids else ", ".join(f"<@{i}>" for i in ids)
        await interaction.followup.send(f"✅ Filter set to **{mode}** for {self.view_ref.channel.mention}: {nice}", ephemeral=True)

# ---------- View ----------
class AutoDeleteView(discord.ui.View):
    def __init__(self, cog: "AutoDeleteCog", channel: discord.TextChannel):
        super().__init__(timeout=180)
        self.cog = cog
        self.channel = channel
        self.message_id: Optional[int] = None  # ephemeral message id for modal edits

    async def refresh(self, interaction: discord.Interaction):
        rule = await self.cog._get_rule(self.channel.id)
        enabled = bool(rule and rule["enabled"])
        seconds = int(rule["seconds"]) if rule else None
        mode = (rule or {}).get("users_mode", "all")
        users_csv = (rule or {}).get("users_csv", "")
        users = [int(x) for x in users_csv.split(",") if x.strip().isdigit()]
        user_list_str = "" if mode == "all" else (", ".join(f"<@{i}>" for i in users) or "(none)")

        desc = []
        desc.append(f"**Status:** {'Enabled' if enabled else 'Disabled'}")
        if seconds is not None:
            desc.append(f"**Duration:** {pretty_duration(seconds)}")
        desc.append(f"**User filter:** `{mode}` {user_list_str}")
        desc.append("\nUse buttons below to configure this channel.")
        embed = discord.Embed(
            title=f"🧹 Auto-Delete — #{self.channel.name}",
            description="\n".join(desc),
            color=discord.Color.blurple(),
        )

        self.enable_button.disabled = enabled
        self.disable_button.disabled = not enabled

        if getattr(interaction, "message", None) is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if self.message_id:
            await interaction.followup.edit_message(self.message_id, embed=embed, view=self)

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success)
    async def enable_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        rule = await self.cog._get_rule(self.channel.id)
        sec = int(rule["seconds"]) if rule else 60
        await self.cog._set_rule(self.channel.id, sec, True)
        await self.refresh(interaction)

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger)
    async def disable_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        rule = await self.cog._get_rule(self.channel.id) or {"seconds": 60}
        await self.cog._set_rule(self.channel.id, int(rule["seconds"]), False)
        await self.refresh(interaction)

    @discord.ui.button(label="Set Duration", style=discord.ButtonStyle.primary)
    async def set_seconds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SecondsModal(self))

    @discord.ui.button(label="User Filter", style=discord.ButtonStyle.secondary)
    async def set_filter(self, interaction: discord.Interaction, button: discord.ui.Button):
        rule = await self.cog._get_rule(self.channel.id) or {"users_mode": "all", "users_csv": ""}
        await interaction.response.send_modal(UsersFilterModal(self, rule["users_mode"], rule["users_csv"]))

    @discord.ui.button(label="Backfill Recent (100)", style=discord.ButtonStyle.secondary)
    async def backfill(self, interaction: discord.Interaction, button: discord.ui.Button):
        rule = await self.cog._get_rule(self.channel.id)
        if not rule or not rule["enabled"]:
            return await interaction.response.send_message("Enable the rule first.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        scheduled = await self.cog._backfill_recent(
            self.channel,
            seconds=int(rule["seconds"]),
            users_mode=rule.get("users_mode", "all"),
            users=rule.get("users_csv", ""),
            limit=100,
        )
        await interaction.followup.send(f"⏱️ Scheduled/deleted **{scheduled}** messages.", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.refresh(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

# ---------- Cog ----------
class AutoDeleteCog(commands.Cog):
    """Per-channel auto-delete with per-message timers + user filters + purge + micro-sweeper."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rule_cache: Dict[int, Dict[str, str | int]] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
        self._seen_recent: Set[int] = set()  # message ids recently scheduled by sweeper

        with self.bot.engine.begin() as c:
            c.execute(text(CREATE_RULES_SQL))
            try: c.execute(text(ALTER_ADD_USERS_MODE))
            except Exception: pass
            try: c.execute(text(ALTER_ADD_USERS_CSV))
            except Exception: pass

        # Start micro-sweeper
        self.sweeper.start()

    def cog_unload(self):
        self.sweeper.cancel()
        for t in list(self._tasks.values()):
            try: t.cancel()
            except Exception: pass

    # ----- DB I/O -----
    async def _get_rule(self, channel_id: int) -> Optional[Dict[str, str | int]]:
        if channel_id in self._rule_cache:
            return self._rule_cache[channel_id]
        with self.bot.engine.connect() as c:
            row = c.execute(text(
                "SELECT seconds, enabled, users_mode, users_csv FROM autodelete_rules WHERE channel_id=:cid"
            ), {"cid": channel_id}).fetchone()
        if not row:
            return None
        rule = {"seconds": int(row[0]), "enabled": int(row[1]), "users_mode": row[2], "users_csv": row[3]}
        self._rule_cache[channel_id] = rule
        return rule

    async def _set_rule(self, channel_id: int, seconds: int, enabled: bool):
        seconds = max(MIN_SECONDS, min(MAX_SECONDS, int(seconds)))
        with self.bot.engine.begin() as c:
            c.execute(text(
                "INSERT INTO autodelete_rules(channel_id, seconds, enabled, updated_at) "
                "VALUES (:cid,:sec,:en,:ts) "
                "ON CONFLICT(channel_id) DO UPDATE SET seconds=excluded.seconds, enabled=excluded.enabled, updated_at=excluded.updated_at"
            ), {"cid": channel_id, "sec": seconds, "en": 1 if enabled else 0, "ts": dt.datetime.utcnow().isoformat()})
        rule = await self._get_rule(channel_id) or {}
        rule.update({"seconds": seconds, "enabled": 1 if enabled else 0})
        self._rule_cache[channel_id] = rule
        print(f"[autodelete] Rule set: channel={channel_id} seconds={seconds} enabled={enabled}")

    async def _set_filter(self, channel_id: int, *, mode: str, users_csv: str):
        with self.bot.engine.begin() as c:
            c.execute(text(
                "UPDATE autodelete_rules SET users_mode=:m, users_csv=:u, updated_at=:ts WHERE channel_id=:cid"
            ), {"m": mode, "u": users_csv, "cid": channel_id, "ts": dt.datetime.utcnow().isoformat()})
        rule = await self._get_rule(channel_id) or {}
        rule.update({"users_mode": mode, "users_csv": users_csv})
        self._rule_cache[channel_id] = rule
        print(f"[autodelete] Filter set: channel={channel_id} mode={mode} users={users_csv}")

    # ----- Filter check -----
    def _passes_user_filter(self, author_id: int, mode: str, users_csv: str) -> bool:
        if mode == "all":
            return True
        ids = [int(x) for x in users_csv.split(",") if x.strip().isdigit()]
        if mode == "only":
            return author_id in ids
        if mode == "except":
            return author_id not in ids
        return True

    # ----- Scheduling -----
    async def _schedule_delete(self, message: discord.Message, seconds: int, *, users_mode="all", users_csv=""):
        # avoid double-schedule from sweeper+on_message
        if message.id in self._tasks:
            return

        async def runner():
            try:
                now = dt.datetime.utcnow()
                age = (now - message.created_at.replace(tzinfo=None)).total_seconds()
                remain = max(0, seconds - int(age))
                if remain > 0:
                    await asyncio.sleep(remain)

                rule = await self._get_rule(message.channel.id)
                if not rule or not rule["enabled"]:
                    return
                if not self._passes_user_filter(message.author.id, rule.get("users_mode", "all"), rule.get("users_csv", "")):
                    return
                if getattr(message, "pinned", False):
                    return

                await message.delete(reason=f"Auto-delete {pretty_duration(int(rule['seconds']))}")
                print(f"[autodelete] Deleted msg {message.id} in {message.channel.id}")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                print(f"[autodelete] Missing permission to delete in channel {message.channel.id}")
            except Exception as e:
                print(f"[autodelete] Error deleting msg {message.id}: {e}")
            finally:
                self._tasks.pop(message.id, None)
                self._seen_recent.discard(message.id)

        self._tasks[message.id] = asyncio.create_task(runner())
        self._seen_recent.add(message.id)
        print(f"[autodelete] Scheduled msg {message.id} in {message.channel.id} after {seconds}s")

    async def _backfill_recent(self, channel: discord.TextChannel, seconds: int, *, users_mode: str, users: str, limit: int = 100) -> int:
        count = 0
        async for msg in channel.history(limit=limit, oldest_first=False):
            if msg.type != discord.MessageType.default:
                continue
            if getattr(msg, "pinned", False):
                continue
            if msg.author.id == getattr(self.bot.user, "id", None):
                continue
            if not self._passes_user_filter(msg.author.id, users_mode, users):
                continue

            age = (dt.datetime.utcnow() - msg.created_at.replace(tzinfo=None)).total_seconds()
            remain = int(seconds - age)
            if remain <= 0:
                try:
                    await msg.delete(reason=f"Auto-delete backfill ≤ {pretty_duration(seconds)}")
                    count += 1
                except Exception:
                    pass
            else:
                await self._schedule_delete(msg, seconds, users_mode=users_mode, users_csv=users)
                count += 1
        return count

    # ----- Events -----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        if message.author.id == getattr(self.bot.user, "id", None):
            return
        if message.type != discord.MessageType.default:
            return

        rule = await self._get_rule(message.channel.id)
        if not rule or not rule["enabled"]:
            return
        if not self._passes_user_filter(message.author.id, rule.get("users_mode", "all"), rule.get("users_csv", "")):
            return

        await self._schedule_delete(
            message,
            int(rule["seconds"]),
            users_mode=rule.get("users_mode", "all"),
            users_csv=rule.get("users_csv", ""),
        )

    # ----- Micro-sweeper (5s) -----
    @tasks.loop(seconds=5.0)
    async def sweeper(self):
        try:
            # collect enabled channels
            with self.bot.engine.connect() as c:
                rows = c.execute(text("SELECT channel_id, seconds, users_mode, users_csv FROM autodelete_rules WHERE enabled=1")).fetchall()
            for row in rows:
                ch_id, sec, mode, users = int(row[0]), int(row[1]), str(row[2]), str(row[3])
                ch = self.bot.get_channel(ch_id)
                if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                    continue
                # look at a small recent window so this is cheap
                async for msg in ch.history(limit=25, oldest_first=False):
                    if msg.type != discord.MessageType.default:
                        continue
                    if getattr(msg, "pinned", False):
                        continue
                    if msg.author.id == getattr(self.bot.user, "id", None):
                        continue
                    if not self._passes_user_filter(msg.author.id, mode, users):
                        continue
                    if msg.id in self._tasks or msg.id in self._seen_recent:
                        continue
                    # schedule if still within window
                    age = (dt.datetime.utcnow() - msg.created_at.replace(tzinfo=None)).total_seconds()
                    if age < sec:
                        await self._schedule_delete(msg, sec, users_mode=mode, users_csv=users)
        except Exception as e:
            print(f"[autodelete] sweeper error: {e}")

    @sweeper.before_loop
    async def before_sweeper(self):
        await self.bot.wait_until_ready()

    # ----- Slash: UI -----
    @app_commands.command(name="autodelete", description="Configure auto-delete rules for a channel.")
    @app_commands.default_permissions(manage_messages=True)
    async def autodelete(self, inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        ch = channel or inter.channel
        if not isinstance(ch, discord.TextChannel):
            return await inter.response.send_message("Pick a text channel.", ephemeral=True)

        view = AutoDeleteView(self, ch)
        rule = await self._get_rule(ch.id)
        enabled = bool(rule and rule["enabled"])
        seconds = int(rule["seconds"]) if rule else None
        mode = (rule or {}).get("users_mode", "all")
        users_csv = (rule or {}).get("users_csv", "")
        users_list = [int(x) for x in users_csv.split(",") if x.strip().isdigit()]
        user_list_str = "" if mode == "all" else (", ".join(f"<@{i}>" for i in users_list) or "(none)")

        desc = [
            f"**Status:** {'Enabled' if enabled else 'Disabled'}",
            f"**Duration:** {pretty_duration(seconds) if seconds is not None else '(none)'}",
            f"**User filter:** `{mode}` {user_list_str}",
            "\nUse the buttons below to configure this channel.",
        ]
        embed = discord.Embed(title=f"🧹 Auto-Delete — #{ch.name}", description="\n".join(desc), color=discord.Color.blurple())
        await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        msg = await inter.original_response()
        view.message_id = msg.id

    # ----- Slash: Purge -----
    @app_commands.command(name="purge", description="Bulk delete recent messages in this channel.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.descri