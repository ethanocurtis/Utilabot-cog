# cogs/mc_todo.py
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo

CONFIG_PATH = "data/mc_todo.json"
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)

# ---- Settings / constants ----
PRIORITY_CHOICES = ("low", "med", "high")
DEFAULT_TZ = "America/Chicago"
CENTRAL = ZoneInfo(DEFAULT_TZ)  # CST/CDT auto-handled

# ---- Time helpers ----
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso_utc(dt: datetime) -> str:
    """Return ISO8601 string with Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")

def parse_iso_utc(s: str) -> Optional[datetime]:
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None

# =====================================================================
# UI: Modals & Panel View (persistent; with dropdown)
# =====================================================================

class AddTaskModal(discord.ui.Modal):
    def __init__(self, cog: "MCTodo"):
        super().__init__(title="Add Task")
        self.cog = cog

        self.task_input = discord.ui.TextInput(
            label="Task",
            placeholder="Fix iron farm at spawn",
            max_length=300
        )
        self.priority_input = discord.ui.TextInput(
            label="Priority (low/med/high) - optional",
            required=False,
            max_length=10,
            placeholder="high"
        )
        self.tags_input = discord.ui.TextInput(
            label="Tags (space or comma separated) - optional",
            required=False,
            placeholder="#spawn #iron  OR  spawn, iron"
        )

        self.add_item(self.task_input)
        self.add_item(self.priority_input)
        self.add_item(self.tags_input)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.task_input.value.strip()
        pr = (self.priority_input.value or "").strip().lower()
        if pr and pr not in PRIORITY_CHOICES:
            await interaction.response.send_message(
                "❌ Priority must be one of: low, med, high (or leave blank).",
                ephemeral=True
            )
            return
        tags = (self.tags_input.value or "").strip() or None

        g = self.cog._g(interaction.guild_id)
        tid = self.cog._next_id(g)
        item = {
            "id": tid,
            "text": text,
            "priority": pr if pr else "low",
            "tags": self.cog._parse_tags(tags),
            "added_by": interaction.user.id,
            "added_at": iso_utc(now_utc()),
        }
        g["todo"].append(item)
        await self.cog._save()
        await interaction.response.send_message(f"✅ Added `#{tid}`: {text}", ephemeral=True)
        await self.cog._refresh_outputs(interaction.guild)


class DoneTaskModal(discord.ui.Modal):
    def __init__(self, cog: "MCTodo"):
        super().__init__(title="Mark Task Done")
        self.cog = cog

        self.id_input = discord.ui.TextInput(
            label="Task ID (number)",
            placeholder="e.g., 7",
            max_length=12
        )
        self.add_item(self.id_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            tid = int(self.id_input.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Enter a numeric task ID.", ephemeral=True)
            return

        g = self.cog._g(interaction.guild_id)
        t = self.cog._find_task(g, tid, in_done=False)
        if not t:
            await interaction.response.send_message("❌ Task not found in To-Do.", ephemeral=True)
            return
        g["todo"] = [x for x in g["todo"] if int(x["id"]) != tid]
        t["done_by"] = interaction.user.id
        t["done_at"] = iso_utc(now_utc())
        g["done"].append(t)
        await self.cog._save()
        await interaction.response.send_message(f"✅ Completed `#{tid}`.", ephemeral=True)
        await self.cog._refresh_outputs(interaction.guild)


class TaskSelect(discord.ui.Select):
    """Dropdown to pick a To-Do task and mark it done."""
    CID_SELECT = "mctodo:select"

    def __init__(self, cog: "MCTodo", guild_id: Optional[int], *, placeholder: str = "Select a task to mark done"):
        # Build options from the guild's current To-Do list (latest 25)
        options: List[discord.SelectOption] = []
        if guild_id is not None:
            g = cog._g(guild_id)
            todos = list(g.get("todo", []))[-25:]  # latest 25
            # reverse to show newest first
            todos.reverse()
            for t in todos:
                label = f"#{int(t['id']):03} [{t.get('priority','low')}]"
                # trim desc to avoid huge menus
                desc_text = t.get("text", "")
                if len(desc_text) > 90:
                    desc_text = desc_text[:87] + "..."
                options.append(
                    discord.SelectOption(
                        label=label,
                        description=desc_text if desc_text else None,
                        value=str(t["id"]),
                    )
                )
        # If no options available, provide a no-op one
        if not options:
            options = [discord.SelectOption(label="No To-Do tasks available", value="noop", default=True)]

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=self.CID_SELECT
        )
        self.cog = cog
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "noop":
            await interaction.response.send_message("Nothing to do right now ✅", ephemeral=True)
            return
        try:
            tid = int(val)
        except ValueError:
            await interaction.response.send_message("❌ Invalid selection.", ephemeral=True)
            return

        g = self.cog._g(interaction.guild_id)
        t = self.cog._find_task(g, tid, in_done=False)
        if not t:
            await interaction.response.send_message("❌ Task not found in To-Do (maybe already completed).", ephemeral=True)
            # try refreshing since list may be stale
            await self.cog._refresh_outputs(interaction.guild)
            return

        g["todo"] = [x for x in g["todo"] if int(x["id"]) != tid]
        t["done_by"] = interaction.user.id
        t["done_at"] = iso_utc(now_utc())
        g["done"].append(t)
        await self.cog._save()

        await interaction.response.send_message(f"✅ Completed `#{tid}` via dropdown.", ephemeral=True)
        await self.cog._refresh_outputs(interaction.guild)


class PanelView(discord.ui.View):
    """Persistent buttons + dropdown under the panel message.

    We register a persistent instance at startup (with generic/no-op options)
    so interactions still work after restarts. When sending/updating the panel,
    we build a fresh view with guild-specific options via _panel_view(guild).
    """
    CID_ADD = "mctodo:add"
    CID_DONE = "mctodo:done"
    CID_VIEW = "mctodo:view"
    # Select uses TaskSelect.CID_SELECT

    def __init__(self, cog: "MCTodo", guild_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

        # Buttons
        btn_add = discord.ui.Button(label="Add Task", emoji="➕", style=discord.ButtonStyle.success, custom_id=self.CID_ADD)
        btn_done = discord.ui.Button(label="Mark Done", emoji="✅", style=discord.ButtonStyle.primary, custom_id=self.CID_DONE)
        btn_view = discord.ui.Button(label="View All", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=self.CID_VIEW)

        btn_add.callback = self.on_add_click
        btn_done.callback = self.on_done_click
        btn_view.callback = self.on_view_click

        self.add_item(btn_add)
        self.add_item(btn_done)
        self.add_item(btn_view)

        # Dropdown (guild-aware if provided)
        self.add_item(TaskSelect(self.cog, guild_id))

    async def on_add_click(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddTaskModal(self.cog))

    async def on_done_click(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DoneTaskModal(self.cog))

    async def on_view_click(self, interaction: discord.Interaction):
        g = self.cog._g(interaction.guild_id)
        items = g["todo"] if g["todo"] else g["done"]
        title = "To-Do (current)" if g["todo"] else "Completed (since To-Do empty)"
        embed = self.cog._make_list_embed(title, items, note="Use /mctodo list for filters.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# =====================================================================
# Cog
# =====================================================================

class MCTodo(commands.Cog):
    """Minecraft Server To-Do list with sticky panel, optional channel topic updates, daily digest, panel buttons, and dropdown."""

    # =========================
    # Init / file I/O
    # =========================
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"guilds": {}}
        self._load_sync()

        # Register a persistent view so components survive restarts.
        # (Uses a generic/no-op dropdown; live panels use a guild-aware view.)
        self.bot.add_view(PanelView(self, guild_id=None))

        self._ticker.start()

    def cog_unload(self):
        if self._ticker.is_running():
            self._ticker.cancel()

    def _load_sync(self):
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            else:
                self._save_sync()
        except Exception:
            self.data = {"guilds": {}}

    def _save_sync(self):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)

    async def _save(self):
        async with self._lock:
            self._save_sync()

    # =========================
    # Data helpers
    # =========================
    def _g(self, guild_id: int) -> Dict[str, Any]:
        gid = str(guild_id)
        g = self.data["guilds"].get(gid)
        if not g:
            g = {
                "settings": {
                    "channeltopic_enabled": False,
                    "panel_channel_id": None,
                    "panel_message_id": None,
                    "digest": {
                        "enabled": False,
                        "channel_id": None,
                        "time_h": 9,
                        "time_m": 0,
                        "tz": DEFAULT_TZ,
                        "last_sent_date": None,  # "YYYY-MM-DD" in local tz
                    },
                },
                "seq": 0,
                "todo": [],
                "done": [],
            }
            self.data["guilds"][gid] = g
            self._save_sync()
        return g

    def _next_id(self, g: Dict[str, Any]) -> int:
        g["seq"] = int(g.get("seq", 0)) + 1
        return g["seq"]

    def _parse_tags(self, tags: Optional[str]) -> List[str]:
        if not tags:
            return []
        parts = re.split(r"[,\s]+", tags.strip())
        cleaned = []
        for p in parts:
            if not p:
                continue
            p = p.lower()
            if not p.startswith("#"):
                p = "#" + p
            cleaned.append(p)
        return cleaned

    def _mention_user(self, user_id: Optional[int]) -> str:
        return f"<@{int(user_id)}>" if user_id else "Unknown"

    # =========================
    # Formatting helpers
    # =========================
    def _fmt_done_suffix(self, t: Dict[str, Any]) -> str:
        """Return '✅ by @user • YYYY-MM-DD HH:MM TZ' if done."""
        if "done_by" not in t:
            return ""
        by = self._mention_user(t.get("done_by"))
        at = t.get("done_at")
        if at:
            dt = parse_iso_utc(at)
            if dt:
                local_dt = dt.astimezone(CENTRAL)
                at_disp = local_dt.strftime("%Y-%m-%d %H:%M %Z")
            else:
                at_disp = at
            return f"  ✅ by {by} • {at_disp}"
        return f"  ✅ by {by}"

    def _fmt_task_line(self, t: Dict[str, Any]) -> str:
        pr = t.get("priority", "low")
        tag_str = " ".join(t.get("tags", [])) if t.get("tags") else ""
        base = f"`#{t['id']:>03}` [{pr}] {t['text']}" + (f"  {tag_str}" if tag_str else "")
        return base + self._fmt_done_suffix(t)

    # =========================
    # Embeds
    # =========================
    def _make_panel_embed(self, guild: discord.Guild, gdata: Dict[str, Any]) -> discord.Embed:
        todo = gdata["todo"]
        done = gdata["done"]

        pr_counter = Counter(t.get("priority", "low") for t in todo)
        pr_str = " | ".join(f"{k}:{pr_counter.get(k,0)}" for k in ("high", "med", "low"))

        newest = todo[-3:] if len(todo) > 3 else todo
        newest_lines = [self._fmt_task_line(t) for t in newest] if newest else ["*(no tasks)*"]

        recent = done[-3:] if len(done) > 3 else done
        recent_lines = [self._fmt_task_line(t) for t in recent] if recent else ["*(none yet)*"]

        tag_counter = Counter(tag for t in (todo + done) for tag in t.get("tags", []))
        top_tags = ", ".join([f"{tag}({cnt})" for tag, cnt in tag_counter.most_common(5)]) if tag_counter else "—"

        embed = discord.Embed(
            title="🗒️ Minecraft To-Do Panel",
            description=f"**Tasks remaining:** **{len(todo)}**  •  **Priority:** {pr_str}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Newest", value="\n".join(newest_lines), inline=False)
        embed.add_field(name="Recently Completed", value="\n".join(recent_lines), inline=False)
        embed.add_field(name="Top Tags", value=top_tags, inline=False)
        embed.set_footer(text="Use /mctodo add to add a task • /mctodo list to browse")
        return embed

    def _make_list_embed(
        self,
        title: str,
        items: List[Dict[str, Any]],
        note: Optional[str] = None,
        limit: int = 15,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.dark_teal())
        if not items:
            embed.description = "*(no items)*"
            if note:
                embed.set_footer(text=note)
            return embed

        lines = [self._fmt_task_line(t) for t in items[:limit]]
        if len(items) > limit:
            lines.append(f"…and **{len(items) - limit}** more.")
        embed.description = "\n".join(lines)
        if note:
            embed.set_footer(text=note)
        return embed

    # =========================
    # Output refreshers
    # =========================
    def _panel_view(self, guild: discord.Guild) -> PanelView:
        # View with guild-specific dropdown options
        return PanelView(self, guild_id=guild.id)

    async def _refresh_panel(self, guild: discord.Guild):
        g = self._g(guild.id)
        s = g["settings"]
        ch_id = s.get("panel_channel_id")
        msg_id = s.get("panel_message_id")
        if not ch_id or not msg_id:
            return

        channel = guild.get_channel(int(ch_id))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await channel.fetch_message(int(msg_id))
        except Exception:
            # stale message; unbind
            s["panel_message_id"] = None
            await self._save()
            return

        try:
            await msg.edit(embed=self._make_panel_embed(guild, g), view=self._panel_view(guild))
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _refresh_channeltopic(self, guild: discord.Guild):
        g = self._g(guild.id)
        s = g["settings"]
        if not s.get("channeltopic_enabled"):
            return
        ch_id = s.get("panel_channel_id")
        if not ch_id:
            return
        channel = guild.get_channel(int(ch_id))
        if not isinstance(channel, discord.TextChannel):
            return

        todo = g.get("todo", [])
        pr_counter = Counter(t.get("priority", "low") for t in todo)
        tag_counter = Counter(tag for t in (todo + g.get("done", [])) for tag in t.get("tags", []))
        top_tags = ",".join([tag for tag, _ in tag_counter.most_common(3)]) if tag_counter else ""
        topic = f"Tasks left: {len(todo)} | High:{pr_counter.get('high',0)} Med:{pr_counter.get('med',0)} Low:{pr_counter.get('low',0)}"
        if top_tags:
            topic += f" | {top_tags}"

        try:
            await channel.edit(topic=topic[:1024])  # topic limit
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _refresh_outputs(self, guild: discord.Guild):
        await self._refresh_panel(guild)
        await self._refresh_channeltopic(guild)

    # =========================
    # Background ticker (digest)
    # =========================
    @tasks.loop(minutes=1)
    async def _ticker(self):
        await self.bot.wait_until_ready()
        utc_now = now_utc()

        for guild in list(self.bot.guilds):
            g = self._g(guild.id)
            d = g["settings"]["digest"]
            if not d.get("enabled"):
                continue

            tzname = d.get("tz") or DEFAULT_TZ
            try:
                tz = ZoneInfo(tzname)
            except Exception:
                tz = CENTRAL  # fallback
            local_now = utc_now.astimezone(tz)
            if local_now.hour != int(d.get("time_h", 9)) or local_now.minute != int(d.get("time_m", 0)):
                continue

            today_str = local_now.strftime("%Y-%m-%d")
            if d.get("last_sent_date") == today_str:
                continue  # already sent today

            ch_id = d.get("channel_id") or g["settings"].get("panel_channel_id")
            if not ch_id:
                continue
            channel = guild.get_channel(int(ch_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue

            # Compose digest
            todo = g.get("todo", [])
            pr_counter = Counter(t.get("priority", "low") for t in todo)
            embed = discord.Embed(
                title="🗞️ Daily Minecraft To-Do Digest",
                description=(
                    f"You have **{len(todo)}** tasks.\n"
                    f"High: {pr_counter.get('high',0)} • Med: {pr_counter.get('med',0)} • Low: {pr_counter.get('low',0)}"
                ),
                color=discord.Color.green(),
                timestamp=utc_now,
            )
            recent = todo[-5:] if len(todo) > 5 else todo
            lines = [self._fmt_task_line(t) for t in recent] if recent else ["*(no tasks yet)*"]
            embed.add_field(name="Recent Tasks", value="\n".join(lines), inline=False)

            try:
                await channel.send(embed=embed)
                d["last_sent_date"] = today_str
                await self._save()
            except (discord.Forbidden, discord.HTTPException):
                pass

            # nice moment to refresh visible surfaces
            await self._refresh_outputs(guild)

    @_ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    # =========================
    # Slash commands
    # =========================
    mctodo = app_commands.Group(name="mctodo", description="Minecraft To-Do commands")

    # ---- add ----
    @mctodo.command(name="add", description="Add a task to the Minecraft To-Do list.")
    @app_commands.describe(
        text="What needs to be done?",
        priority="Task priority",
        tags="Optional tags (e.g., '#spawn #iron' or 'spawn, iron')",
    )
    @app_commands.choices(priority=[app_commands.Choice(name=p, value=p) for p in PRIORITY_CHOICES])
    async def add(
        self,
        interaction: discord.Interaction,
        text: str,
        priority: Optional[app_commands.Choice[str]] = None,
        tags: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        tid = self._next_id(g)
        item = {
            "id": tid,
            "text": text.strip(),
            "priority": (priority.value if priority else "low"),
            "tags": self._parse_tags(tags),
            "added_by": interaction.user.id,
            "added_at": iso_utc(now_utc()),
        }
        g["todo"].append(item)
        await self._save()
        await interaction.followup.send(f"✅ Added `#{tid}`: {text}", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # ---- list ----
    @mctodo.command(name="list", description="List tasks.")
    @app_commands.describe(
        status="Filter by status",
        tag="Filter by a tag (e.g., #spawn)",
        priority="Filter by priority",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="todo", value="todo"),
            app_commands.Choice(name="done", value="done"),
            app_commands.Choice(name="all", value="all"),
        ],
        priority=[app_commands.Choice(name=p, value=p) for p in PRIORITY_CHOICES],
    )
    async def list_tasks(
        self,
        interaction: discord.Interaction,
        status: Optional[app_commands.Choice[str]] = None,
        tag: Optional[str] = None,
        priority: Optional[app_commands.Choice[str]] = None,
    ):
        status_val = (status.value if status else "todo")
        tag_norm = None
        if tag:
            tag_norm = tag.lower()
            if not tag_norm.startswith("#"):
                tag_norm = "#" + tag_norm
        pr = priority.value if priority else None

        g = self._g(interaction.guild_id)

        def _match(t: Dict[str, Any]) -> bool:
            if pr and t.get("priority") != pr:
                return False
            if tag_norm and tag_norm not in t.get("tags", []):
                return False
            return True

        items: List[Dict[str, Any]] = []
        if status_val in ("todo", "all"):
            items.extend([t for t in g["todo"] if _match(t)])
        if status_val in ("done", "all"):
            items.extend([t for t in g["done"] if _match(t)])

        title = f"To-Do: {status_val.upper()}"
        embed = self._make_list_embed(title, items)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- done ----
    @mctodo.command(name="done", description="Mark a task as completed.")
    @app_commands.describe(id="Task ID")
    async def mark_done(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        t = self._find_task(g, id, in_done=False)
        if not t:
            await interaction.followup.send("❌ Task not found in To-Do.", ephemeral=True)
            return
        g["todo"] = [x for x in g["todo"] if int(x["id"]) != int(id)]
        t["done_by"] = interaction.user.id
        t["done_at"] = iso_utc(now_utc())
        g["done"].append(t)
        await self._save()
        await interaction.followup.send(f"✅ Completed `#{id}`.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # ---- undo ----
    @mctodo.command(name="undo", description="Move a completed task back to To-Do.")
    @app_commands.describe(id="Task ID")
    async def undo(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        t = self._find_task(g, id, in_done=True)
        if not t:
            await interaction.followup.send("❌ Task not found in Completed.", ephemeral=True)
            return
        g["done"] = [x for x in g["done"] if int(x["id"]) != int(id)]
        t.pop("done_by", None)
        t.pop("done_at", None)
        g["todo"].append(t)
        await self._save()
        await interaction.followup.send(f"↩️ Moved `#{id}` back to To-Do.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # ---- remove ----
    @mctodo.command(name="remove", description="Delete a task entirely.")
    @app_commands.describe(id="Task ID")
    async def remove(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        before = len(g["todo"]) + len(g["done"])
        g["todo"] = [x for x in g["todo"] if int(x["id"]) != int(id)]
        g["done"] = [x for x in g["done"] if int(x["id"]) != int(id)]
        after = len(g["todo"]) + len(g["done"])
        if before == after:
            await interaction.followup.send("❌ Task not found.", ephemeral=True)
            return
        await self._save()
        await interaction.followup.send(f"🗑️ Removed `#{id}`.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # ---- clear ----
    @mctodo.command(name="clear", description="Clear tasks in a section.")
    @app_commands.describe(section="Which section to clear")
    @app_commands.choices(
        section=[
            app_commands.Choice(name="todo", value="todo"),
            app_commands.Choice(name="done", value="done"),
            app_commands.Choice(name="all", value="all"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clear(
        self,
        interaction: discord.Interaction,
        section: app_commands.Choice[str],
    ):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        sec = section.value
        if sec in ("todo", "all"):
            g["todo"] = []
        if sec in ("done", "all"):
            g["done"] = []
        await self._save()
        await interaction.followup.send(f"🧹 Cleared **{sec}**.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # ---- stats ----
    @mctodo.command(name="stats", description="Show quick stats.")
    async def stats(self, interaction: discord.Interaction):
        g = self._g(interaction.guild_id)
        todo = g["todo"]
        done = g["done"]

        pr_counter = Counter(t.get("priority", "low") for t in todo)
        tag_counter = Counter(tag for t in (todo + done) for tag in t.get("tags", []))

        embed = discord.Embed(title="📊 To-Do Stats", color=discord.Color.gold())
        embed.add_field(
            name="Counts",
            value=f"To-Do: **{len(todo)}**\nDone: **{len(done)}**",
            inline=True,
        )
        embed.add_field(
            name="Priority (To-Do)",
            value=f"High: {pr_counter.get('high',0)}\nMed: {pr_counter.get('med',0)}\nLow: {pr_counter.get('low',0)}",
            inline=True,
        )
        top_tags = "\n".join([f"{tag}: {cnt}" for tag, cnt in tag_counter.most_common(5)]) or "—"
        embed.add_field(name="Top Tags (All Time)", value=top_tags, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- summary (panel-style, on demand) ----
    @mctodo.command(name="summary", description="Quick summary (like the sticky panel).")
    async def summary(self, interaction: discord.Interaction):
        g = self._g(interaction.guild_id)
        embed = self._make_panel_embed(interaction.guild, g)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- panel set/clear ----
    @mctodo.command(name="panel_set", description="Post and bind the sticky To-Do summary panel in this channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel_set(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        s = g["settings"]
        ch = interaction.channel
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("❌ Run this in a text channel or thread.", ephemeral=True)
            return

        # delete old panel if any
        old_ch_id = s.get("panel_channel_id")
        old_msg_id = s.get("panel_message_id")
        if old_ch_id and old_msg_id:
            try:
                old_ch = interaction.guild.get_channel(int(old_ch_id))
                if old_ch:
                    old_msg = await old_ch.fetch_message(int(old_msg_id))
                    await old_msg.delete()
            except Exception:
                pass

        embed = self._make_panel_embed(interaction.guild, g)
        msg = await ch.send(embed=embed, view=self._panel_view(interaction.guild))
        s["panel_channel_id"] = msg.channel.id
        s["panel_message_id"] = msg.id
        await self._save()
        await interaction.followup.send("📌 Panel set. Buttons & dropdown added. I’ll keep this message updated.", ephemeral=True)

    @mctodo.command(name="panel_clear", description="Unbind (and delete) the current To-Do summary panel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel_clear(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        s = g["settings"]
        ch_id = s.get("panel_channel_id")
        msg_id = s.get("panel_message_id")
        if ch_id and msg_id:
            try:
                ch = interaction.guild.get_channel(int(ch_id))
                if ch:
                    msg = await ch.fetch_message(int(msg_id))
                    await msg.delete()
            except Exception:
                pass
        s["panel_channel_id"] = None
        s["panel_message_id"] = None
        await self._save()
        await interaction.followup.send("🗑️ Panel cleared.", ephemeral=True)

    # ---- channel topic toggle ----
    @mctodo.command(name="channeltopic", description="Enable/disable channel topic updates (uses the panel channel).")
    @app_commands.describe(state="Turn channel topic updates on or off")
    @app_commands.choices(
        state=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def channeltopic(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        g["settings"]["channeltopic_enabled"] = (state.value == "on")
        await self._save()
        await interaction.followup.send(f"📝 Channel topic updates: **{state.value}**.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # ---- digest set/off ----
    @mctodo.command(name="digest", description="Set or turn off the daily digest.")
    @app_commands.describe(
        action="Enable or disable",
        time="24h time like 09:00 (ignored for 'off')",
        channel="Channel to post in (defaults to the panel channel if set)",
        tz="Time zone (IANA name, default America/Chicago)"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="set", value="set"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def digest(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        time: Optional[str] = None,
        channel: Optional[discord.abc.GuildChannel] = None,
        tz: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        d = g["settings"]["digest"]

        if action.value == "off":
            d["enabled"] = False
            await self._save()
            await interaction.followup.send("🔕 Daily digest disabled.", ephemeral=True)
            return

        if not time or not re.match(r"^\d{1,2}:\d{2}$", time.strip()):
            await interaction.followup.send("❌ Provide time as `HH:MM` (24h). Example: `09:00`", ephemeral=True)
            return
        hh, mm = time.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            await interaction.followup.send("❌ Invalid time. Use 00:00–23:59.", ephemeral=True)
            return

        if channel:
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.followup.send("❌ Choose a text channel or thread.", ephemeral=True)
                return
            ch_id = channel.id
        else:
            ch_id = g["settings"].get("panel_channel_id")

        if not ch_id:
            await interaction.followup.send("❌ No channel provided and no panel channel set.", ephemeral=True)
            return

        tzname = tz or d.get("tz") or DEFAULT_TZ
        try:
            ZoneInfo(tzname)  # validate
        except Exception:
            await interaction.followup.send("❌ Invalid time zone. Use an IANA name like `America/Chicago`.", ephemeral=True)
            return

        d.update({
            "enabled": True,
            "channel_id": ch_id,
            "time_h": hh,
            "time_m": mm,
            "tz": tzname,
            "last_sent_date": None,
        })
        await self._save()
        await interaction.followup.send(
            f"🗓️ Daily digest set for **{hh:02}:{mm:02} {tzname}** in <#{ch_id}>.",
            ephemeral=True,
        )

    # =========================
    # Internal find helper
    # =========================
    def _find_task(self, g: Dict[str, Any], tid: int, in_done: bool = False) -> Optional[Dict[str, Any]]:
        arr = g["done"] if in_done else g["todo"]
        for t in arr:
            if int(t.get("id")) == int(tid):
                return t
        return None

async def setup(bot: commands.Bot):
    await bot.add_cog(MCTodo(bot))
