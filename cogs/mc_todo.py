# cogs/mc_todo.py
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

CONFIG_PATH = "data/mc_todo.json"
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)

PRIORITY_CHOICES = ("low", "med", "high")
DEFAULT_TZ = "America/Chicago"  # used for digest; simple handling via offset map below

# --- very lightweight TZ offset map for daily digest (no external deps) ---
# You can expand or swap to 'pytz'/'zoneinfo' later. This is a best-effort fixed mapping.
TZ_OFFSETS = {
    "America/Chicago": -5,  # CDT approx; if CST, this will be -6; good enough for a daily tick
}

def now_utc() -> datetime:
    return datetime.utcnow()

def to_tz(dt_utc: datetime, tz: str) -> datetime:
    offset = TZ_OFFSETS.get(tz, 0)
    return dt_utc + timedelta(hours=offset)

class MCTodo(commands.Cog):
    """Minecraft Server To-Do list cog with sticky summary panel and optional channel topic/digest."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"guilds": {}}
        self._load_sync()
        # background ticker for digest + auto-refresh safety
        self._ticker.start()

    def cog_unload(self):
        if self._ticker.is_running():
            self._ticker.cancel()

    # -------------------------
    # File I/O
    # -------------------------
    def _load_sync(self):
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            else:
                self._save_sync()
        except Exception:
            # if corrupt, keep in-memory default and don't crash the cog
            self.data = {"guilds": {}}

    def _save_sync(self):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)

    async def _save(self):
        async with self._lock:
            self._save_sync()

    # -------------------------
    # Data helpers
    # -------------------------
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
                        "last_sent_date": None,  # "YYYY-MM-DD"
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
        # Accept space or comma separated, normalize to lowercase and ensure startswith '#'
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

    def _fmt_task_line(self, t: Dict[str, Any]) -> str:
        pr = t.get("priority", "low")
        tag_str = " ".join(t.get("tags", [])) if t.get("tags") else ""
        return f"`#{t['id']:>03}` [{pr}] {t['text']}" + (f"  {tag_str}" if tag_str else "")

    def _find_task(self, g: Dict[str, Any], tid: int, in_done: bool = False) -> Optional[Dict[str, Any]]:
        arr = g["done"] if in_done else g["todo"]
        for t in arr:
            if int(t.get("id")) == int(tid):
                return t
        return None

    # -------------------------
    # Embeds
    # -------------------------
    def _make_panel_embed(self, guild: discord.Guild, gdata: Dict[str, Any]) -> discord.Embed:
        todo = gdata["todo"]
        done = gdata["done"]
        count = len(todo)

        # priority breakdown
        pr_counter = Counter(t.get("priority", "low") for t in todo)
        pr_str = " | ".join(f"{k}:{pr_counter.get(k,0)}" for k in ("high", "med", "low"))

        # newest few
        newest = todo[-3:] if len(todo) > 3 else todo
        newest_lines = [self._fmt_task_line(t) for t in newest] if newest else ["*(no tasks)*"]

        # recently completed
        recent = done[-3:] if len(done) > 3 else done
        recent_lines = [self._fmt_task_line(t) for t in recent] if recent else ["*(none yet)*"]

        # top tags from all tasks
        tag_counter = Counter(tag for t in (todo + done) for tag in t.get("tags", []))
        top_tags = ", ".join([f"{tag}({cnt})" for tag, cnt in tag_counter.most_common(5)]) if tag_counter else "—"

        embed = discord.Embed(
            title="🗒️ Minecraft To-Do Panel",
            description=f"**Tasks remaining:** **{count}**  •  **Priority:** {pr_str}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Newest", value="\n".join(newest_lines), inline=False)
        embed.add_field(name="Recently Completed", value="\n".join(recent_lines), inline=False)
        embed.add_field(name="Top Tags", value=top_tags, inline=False)
        embed.set_footer(text="Use /mctodo add to add a task, /mctodo list to browse.")
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
            return embed

        lines = [self._fmt_task_line(t) for t in items[:limit]]
        if len(items) > limit:
            lines.append(f"…and **{len(items) - limit}** more.")
        embed.description = "\n".join(lines)
        if note:
            embed.set_footer(text=note)
        return embed

    # -------------------------
    # External refreshers
    # -------------------------
    async def _refresh_panel(self, guild: discord.Guild):
        g = self._g(guild.id)
        s = g["settings"]
        channel_id = s.get("panel_channel_id")
        message_id = s.get("panel_message_id")
        if not channel_id or not message_id:
            return

        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        try:
            msg = await channel.fetch_message(int(message_id))
        except Exception:
            # stale message id: clear it silently
            s["panel_message_id"] = None
            await self._save()
            return

        try:
            await msg.edit(embed=self._make_panel_embed(guild, g))
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    async def _refresh_channeltopic(self, guild: discord.Guild):
        g = self._g(guild.id)
        s = g["settings"]
        if not s.get("channeltopic_enabled"):
            return
        # Use the panel channel if set; else skip
        channel_id = s.get("panel_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        todo_count = len(g.get("todo", []))
        pr_counter = Counter(t.get("priority", "low") for t in g.get("todo", []))
        tag_counter = Counter(tag for t in (g.get("todo", []) + g.get("done", [])) for tag in t.get("tags", []))
        top_tags = ",".join([tag for tag, _ in tag_counter.most_common(3)]) if tag_counter else ""

        topic = f"Tasks left: {todo_count} | High:{pr_counter.get('high',0)} Med:{pr_counter.get('med',0)} Low:{pr_counter.get('low',0)}"
        if top_tags:
            topic += f" | {top_tags}"
        try:
            await channel.edit(topic=topic[:1024])  # Discord topic limit
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    async def _refresh_outputs(self, guild: discord.Guild):
        await self._refresh_panel(guild)
        await self._refresh_channeltopic(guild)

    # -------------------------
    # Background ticker (digest + safety refresh)
    # -------------------------
    @tasks.loop(minutes=1)
    async def _ticker(self):
        # send digest at scheduled times (per guild)
        dt_utc = now_utc()
        for guild in list(self.bot.guilds):
            g = self._g(guild.id)
            d = g["settings"]["digest"]
            if not d.get("enabled"):
                continue
            tz = d.get("tz", DEFAULT_TZ)
            loc_now = to_tz(dt_utc, tz)
            if loc_now.minute != int(d.get("time_m", 0)) or loc_now.hour != int(d.get("time_h", 9)):
                continue
            today_str = loc_now.strftime("%Y-%m-%d")
            if d.get("last_sent_date") == today_str:
                continue  # already sent today
            ch_id = d.get("channel_id")
            if not ch_id:
                continue
            channel = guild.get_channel(int(ch_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue

            # Compose quick summary
            todo = g.get("todo", [])
            pr_counter = Counter(t.get("priority", "low") for t in todo)
            embed = discord.Embed(
                title="🗞️ Daily To-Do Digest",
                description=f"You have **{len(todo)}** tasks. High:{pr_counter.get('high',0)} Med:{pr_counter.get('med',0)} Low:{pr_counter.get('low',0)}",
                color=discord.Color.green(),
                timestamp=datetime.utcnow(),
            )
            newest = todo[-5:] if len(todo) > 5 else todo
            lines = [self._fmt_task_line(t) for t in newest] if newest else ["*(no tasks yet)*"]
            embed.add_field(name="Recent Tasks", value="\n".join(lines), inline=False)

            try:
                await channel.send(embed=embed)
                d["last_sent_date"] = today_str
                await self._save()
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

            # also refresh panel/topic at digest time
            await self._refresh_outputs(guild)

    @_ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    # -------------------------
    # Slash command tree
    # -------------------------
    mctodo = app_commands.Group(name="mctodo", description="Minecraft To-Do commands")

    # Add
    @mctodo.command(name="add", description="Add a task to the Minecraft To-Do list.")
    @app_commands.describe(
        text="What needs to be done?",
        priority="Task priority",
        tags="Optional tags (e.g. '#spawn #iron' or 'spawn, iron')",
    )
    @app_commands.choices(priority=[app_commands.Choice(name=p, value=p) for p in PRIORITY_CHOICES])
    async def add(self, interaction: discord.Interaction, text: str, priority: Optional[app_commands.Choice[str]] = None, tags: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        tid = self._next_id(g)
        item = {
            "id": tid,
            "text": text.strip(),
            "priority": (priority.value if priority else "low"),
            "tags": self._parse_tags(tags),
            "added_by": interaction.user.id,
            "added_at": now_utc().isoformat() + "Z",
        }
        g["todo"].append(item)
        await self._save()
        await interaction.followup.send(f"✅ Added `#{tid}`: {text}", ephemeral=True)

        # refresh panel/topic
        await self._refresh_outputs(interaction.guild)

    # List
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
        g = self._g(interaction.guild_id)
        status_val = (status.value if status else "todo")
        tag_norm = None
        if tag:
            tag_norm = tag.lower()
            if not tag_norm.startswith("#"):
                tag_norm = "#" + tag_norm
        pr = priority.value if priority else None

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

    # Done
    @mctodo.command(name="done", description="Mark a task as completed.")
    @app_commands.describe(id="Task ID")
    async def mark_done(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        t = self._find_task(g, id, in_done=False)
        if not t:
            await interaction.followup.send("❌ Task not found in To-Do.", ephemeral=True)
            return
        # move to done
        g["todo"] = [x for x in g["todo"] if int(x["id"]) != int(id)]
        t["done_by"] = interaction.user.id
        t["done_at"] = now_utc().isoformat() + "Z"
        g["done"].append(t)
        await self._save()
        await interaction.followup.send(f"✅ Completed `#{id}`.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # Undo
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
        # remove completion markers
        t.pop("done_by", None)
        t.pop("done_at", None)
        g["todo"].append(t)
        await self._save()
        await interaction.followup.send(f"↩️ Moved `#{id}` back to To-Do.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # Remove
    @mctodo.command(name="remove", description="Delete a task entirely.")
    @app_commands.describe(id="Task ID")
    async def remove(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        g = self._g(interaction.guild_id)
        before_todo = len(g["todo"])
        before_done = len(g["done"])
        g["todo"] = [x for x in g["todo"] if int(x["id"]) != int(id)]
        g["done"] = [x for x in g["done"] if int(x["id"]) != int(id)]
        after_total = len(g["todo"]) + len(g["done"])
        if (before_todo + before_done) == after_total:
            await interaction.followup.send("❌ Task not found.", ephemeral=True)
            return
        await self._save()
        await interaction.followup.send(f"🗑️ Removed `#{id}`.", ephemeral=True)
        await self._refresh_outputs(interaction.guild)

    # Clear
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

    # Stats
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

    # Summary (on-demand compact counts)
    @mctodo.command(name="summary", description="Quick summary of tasks.")
    async def summary(self, interaction: discord.Interaction):
        g = self._g(interaction.guild_id)
        embed = self._make_panel_embed(interaction.guild, g)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # Panel set/clear
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

        # If a panel exists elsewhere, try to cleanly move it
        if s.get("panel_channel_id") and s.get("panel_message_id"):
            try:
                old_ch = interaction.guild.get_channel(int(s["panel_channel_id"]))
                if old_ch:
                    old_msg = await old_ch.fetch_message(int(s["panel_message_id"]))
                    await old_msg.delete()
            except Exception:
                pass

        embed = self._make_panel_embed(interaction.guild, g)
        msg = await ch.send(embed=embed)
        s["panel_channel_id"] = msg.channel.id
        s["panel_message_id"] = msg.id
        await self._save()
        await interaction.followup.send("📌 Panel set. I’ll keep this message updated.", ephemeral=True)

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

    # Channel topic toggle
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

    # Digest set/off
    @mctodo.command(name="digest", description="Set or turn off the daily digest.")
    @app_commands.describe(
        action="Enable or disable",
        time="24h time like 09:00 (ignored for 'off')",
        channel="Which channel to post in (defaults to the panel channel if set)",
        tz="Time zone label (default America/Chicago)"
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

        # parse time HH:MM
        if not time or not re.match(r"^\d{1,2}:\d{2}$", time.strip()):
            await interaction.followup.send("❌ Provide time as `HH:MM` (24h). Example: `09:00`", ephemeral=True)
            return
        h, m = time.split(":")
        h, m = int(h), int(m)
        if h < 0 or h > 23 or m < 0 or m > 59:
            await interaction.followup.send("❌ Invalid time. Use 00:00–23:59.", ephemeral=True)
            return

        ch_id = None
        if channel:
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                ch_id = channel.id
            else:
                await interaction.followup.send("❌ Choose a text channel (or thread).", ephemeral=True)
                return
        else:
            # default to panel channel if present
            ch_id = g["settings"].get("panel_channel_id")

        if not ch_id:
            await interaction.followup.send("❌ No channel provided and no panel channel set.", ephemeral=True)
            return

        tz_val = tz or d.get("tz") or DEFAULT_TZ
        d.update({
            "enabled": True,
            "channel_id": ch_id,
            "time_h": h,
            "time_m": m,
            "tz": tz_val,
            "last_sent_date": None,
        })
        await self._save()
        await interaction.followup.send(f"🗓️ Daily digest set for **{h:02}:{m:02} {tz_val}** in <#{ch_id}>.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(MCTodo(bot))
