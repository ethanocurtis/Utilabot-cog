# cogs/autodelete.py
from __future__ import annotations
import asyncio
import datetime as dt
from typing import Optional, Dict

import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import text

MIN_SECONDS = 1
MAX_SECONDS = 7 * 24 * 3600  # 7 days cap as a sanity guard

# ------------- DB helpers -------------

CREATE_RULES_SQL = """
CREATE TABLE IF NOT EXISTS autodelete_rules (
  channel_id INTEGER PRIMARY KEY,
  seconds INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
"""

SELECT_RULE_SQL = "SELECT seconds, enabled FROM autodelete_rules WHERE channel_id=:cid"
UPSERT_RULE_SQL = """
INSERT INTO autodelete_rules(channel_id, seconds, enabled, updated_at)
VALUES (:cid, :sec, :en, :ts)
ON CONFLICT(channel_id) DO UPDATE SET
  seconds=excluded.seconds,
  enabled=excluded.enabled,
  updated_at=excluded.updated_at
"""
DELETE_RULE_SQL = "DELETE FROM autodelete_rules WHERE channel_id=:cid"

# ------------- UI -------------

class SecondsModal(discord.ui.Modal, title="Set Auto-Delete Delay"):
    def __init__(self, view: "AutoDeleteView"):
        super().__init__(timeout=180)
        self.view_ref = view
        self.seconds = discord.ui.TextInput(
            label=f"Delay in seconds ({MIN_SECONDS}–{MAX_SECONDS})",
            placeholder="e.g. 45",
            max_length=10,
            required=True,
        )
        self.add_item(self.seconds)

    async def on_submit(self, interaction: discord.Interaction):
        # Parse & clamp
        try:
            sec = int(str(self.seconds.value).strip())
        except ValueError:
            return await interaction.response.send_message("Not a number.", ephemeral=True)
        sec = max(MIN_SECONDS, min(MAX_SECONDS, sec))

        ch = self.view_ref.channel
        cog: AutoDeleteCog = self.view_ref.cog
        await cog._set_rule(ch.id, sec, True)

        # Defer to avoid “Unknown Webhook” then edit original message
        await interaction.response.defer(ephemeral=True)
        await self.view_ref.refresh(interaction)
        await interaction.followup.send(f"✅ Delay set to **{sec}s** for {ch.mention}.", ephemeral=True)


class AutoDeleteView(discord.ui.View):
    def __init__(self, cog: "AutoDeleteCog", channel: discord.abc.GuildChannel):
        super().__init__(timeout=180)
        self.cog = cog
        self.channel = channel
        self.message_id: Optional[int] = None  # original ephemeral message id

    async def refresh(self, interaction: discord.Interaction):
        rule = await self.cog._get_rule(self.channel.id)
        status = "Enabled" if (rule and rule["enabled"]) else "Disabled"
        sec = rule["seconds"] if rule else None
        desc = (
            f"**Status:** {status}\n"
            + (f"**Delay:** {sec}s\n" if sec is not None else "")
            + "Use the buttons below to configure this channel."
        )
        embed = discord.Embed(
            title=f"🧹 Auto-Delete — #{self.channel.name}",
            description=desc,
            color=discord.Color.blurple(),
        )

        # Enable/disable buttons based on rule state
        self.enable_button.disabled = bool(rule and rule["enabled"])
        self.disable_button.disabled = not bool(rule and rule["enabled"])

        # For modals (no attached message), edit via followup+stored id
        if getattr(interaction, "message", None) is None:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            if self.message_id:
                await interaction.followup.edit_message(self.message_id, embed=embed, view=self)
            return

        # Component interactions can directly edit
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success)
    async def enable_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        rule = await self.cog._get_rule(self.channel.id)
        sec = rule["seconds"] if rule else 60
        await self.cog._set_rule(self.channel.id, sec, True)
        await self.refresh(interaction)

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger)
    async def disable_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._set_rule(self.channel.id, (await self.cog._get_rule(self.channel.id) or {"seconds": 60})["seconds"], False)
        await self.refresh(interaction)

    @discord.ui.button(label="Set Seconds", style=discord.ButtonStyle.primary)
    async def set_seconds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SecondsModal(self))

    @discord.ui.button(label="Backfill Recent (100)", style=discord.ButtonStyle.secondary)
    async def backfill(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Schedule timers for the last ~100 messages younger than the rule
        rule = await self.cog._get_rule(self.channel.id)
        if not rule or not rule["enabled"]:
            return await interaction.response.send_message("Enable the rule first.", ephemeral=True)
        sec = int(rule["seconds"])
        # Fetch history without blocking the UI too long
        await interaction.response.defer(ephemeral=True)
        scheduled = await self.cog._backfill_recent(self.channel, sec, limit=100)
        await interaction.followup.send(f"⏱️ Scheduled {scheduled} messages for deletion.", ephemeral=True)

# ------------- Cog -------------

class AutoDeleteCog(commands.Cog):
    """
    Per-channel auto-delete with per-message timers (supports <60s).
    Also includes /purge.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rule_cache: Dict[int, Dict[str, int]] = {}  # channel_id -> {"seconds": int, "enabled": 0/1}
        self._tasks: Dict[int, asyncio.Task] = {}  # message_id -> task
        # Ensure table exists
        with self.bot.engine.begin() as c:
            c.execute(text(CREATE_RULES_SQL))

    # ---------- DB I/O ----------

    async def _get_rule(self, channel_id: int) -> Optional[Dict[str, int]]:
        if channel_id in self._rule_cache:
            return self._rule_cache[channel_id]
        with self.bot.engine.connect() as c:
            row = c.execute(text(SELECT_RULE_SQL), {"cid": channel_id}).fetchone()
        if not row:
            return None
        data = {"seconds": int(row[0]), "enabled": int(row[1])}
        self._rule_cache[channel_id] = data
        return data

    async def _set_rule(self, channel_id: int, seconds: int, enabled: bool):
        seconds = max(MIN_SECONDS, min(MAX_SECONDS, int(seconds)))
        with self.bot.engine.begin() as c:
            c.execute(text(UPSERT_RULE_SQL), {"cid": channel_id, "sec": seconds, "en": 1 if enabled else 0, "ts": dt.datetime.utcnow().isoformat()})
        self._rule_cache[channel_id] = {"seconds": seconds, "enabled": 1 if enabled else 0}

    # ---------- Real-time deletion ----------

    async def _schedule_delete(self, message: discord.Message, seconds: int):
        """
        Schedule a delete for a specific message with the given delay.
        Will check the rule again right before deleting in case it changed.
        """
        if message.id in self._tasks:
            # already scheduled (shouldn't really happen)
            try:
                self._tasks[message.id].cancel()
            except Exception:
                pass

        async def runner():
            try:
                now = dt.datetime.utcnow()
                age = (now - message.created_at.replace(tzinfo=None)).total_seconds()
                delay = max(0, seconds - int(age))
                if delay > 0:
                    await asyncio.sleep(delay)

                # Re-check rule & message state
                rule = await self._get_rule(message.channel.id)
                if not rule or not rule["enabled"]:
                    return
                # If the rule seconds changed, only delete if still within *current* threshold
                if rule["seconds"] != seconds:
                    # If it's still younger than current rule, proceed
                    now2 = dt.datetime.utcnow()
                    age2 = (now2 - message.created_at.replace(tzinfo=None)).total_seconds()
                    if age2 > rule["seconds"]:
                        return

                # Skip pinned or already deleted
                if getattr(message, "pinned", False):
                    return
                await message.delete(reason=f"Auto-delete after {rule['seconds']}s")
            except discord.NotFound:
                pass  # already gone
            except discord.Forbidden:
                pass  # missing perms
            except Exception:
                # don't spam logs with cancellations
                pass
            finally:
                self._tasks.pop(message.id, None)

        self._tasks[message.id] = asyncio.create_task(runner())

    async def _backfill_recent(self, channel: discord.abc.Messageable, seconds: int, limit: int = 100) -> int:
        scheduled = 0
        async for msg in channel.history(limit=limit, oldest_first=False):
            if msg.type != discord.MessageType.default:
                continue
            if msg.author.bot and msg.author.id == self.bot.user.id:
                continue
            if getattr(msg, "pinned", False):
                continue
            # remaining time
            age = (dt.datetime.utcnow() - msg.created_at.replace(tzinfo=None)).total_seconds()
            remain = int(seconds - age)
            if remain <= 0:
                # try to delete immediately if still within the policy (race window)
                try:
                    await msg.delete(reason=f"Auto-delete backfill ≤ {seconds}s")
                    scheduled += 1
                except Exception:
                    pass
                continue
            await self._schedule_delete(msg, seconds)
            scheduled += 1
        return scheduled

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore DMs / no guild / self
        if not message.guild or message.author.id == getattr(self.bot.user, "id", None):
            return
        # Only standard user messages
        if message.type != discord.MessageType.default:
            return

        rule = await self._get_rule(message.channel.id)
        if not rule or not rule["enabled"]:
            return
        seconds = int(rule["seconds"])
        # schedule per-message deletion (supports <60s)
        await self._schedule_delete(message, seconds)

    # ---------- Slash: UI ----------

    @app_commands.command(name="autodelete", description="Configure auto-delete rules for this channel.")
    @app_commands.default_permissions(manage_messages=True)
    async def autodelete(self, inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        channel = channel or inter.channel  # default to current channel
        if not isinstance(channel, discord.TextChannel):
            return await inter.response.send_message("Pick a text channel.", ephemeral=True)

        view = AutoDeleteView(self, channel)
        # initial state
        rule = await self._get_rule(channel.id)
        status = "Enabled" if (rule and rule["enabled"]) else "Disabled"
        sec = rule["seconds"] if rule else None
        desc = (
            f"**Status:** {status}\n"
            + (f"**Delay:** {sec}s\n" if sec is not None else "No rule set yet.\n")
            + "Use the buttons below to configure this channel."
        )
        embed = discord.Embed(
            title=f"🧹 Auto-Delete — #{channel.name}",
            description=desc,
            color=discord.Color.blurple(),
        )
        await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        msg = await inter.original_response()
        view.message_id = msg.id

    # ---------- Slash: Purge ----------

    @app_commands.command(name="purge", description="Bulk delete recent messages in this channel.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="How many messages to scan (max 1000).",
        contains="Only delete messages containing this text.",
        user="Only delete messages from this user.",
        bots_only="Only delete messages from bots.",
    )
    async def purge(
        self,
        inter: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
        contains: Optional[str] = None,
        user: Optional[discord.Member] = None,
        bots_only: Optional[bool] = False,
    ):
        if not isinstance(inter.channel, discord.TextChannel):
            return await inter.response.send_message("Run this in a text channel.", ephemeral=True)

        await inter.response.defer(ephemeral=True)

        def check(m: discord.Message) -> bool:
            if user and m.author.id != user.id:
                return False
            if bots_only and not m.author.bot:
                return False
            if contains and (contains.lower() not in (m.content or "").lower()):
                return False
            return True

        try:
            deleted = await inter.channel.purge(limit=amount, check=check, bulk=True, reason="Moderator purge")
            await inter.followup.send(f"🧽 Purged **{len(deleted)}** messages.", ephemeral=True)
        except discord.Forbidden:
            await inter.followup.send("I don't have permission to delete messages here.", ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"Error: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoDeleteCog(bot))