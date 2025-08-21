
# cogs/moderation.py
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

def _has_guild_admin_perms(inter: discord.Interaction) -> bool:
    try:
        if isinstance(inter.channel, (discord.TextChannel, discord.Thread)):
            perms = inter.channel.permissions_for(inter.user)
            return bool(perms.administrator or perms.manage_guild)
    except Exception:
        pass
    return False

class Moderation(commands.Cog):
    """
    Moderation utilities: /purge and auto-delete controls, in cog style.

    This cog tries to use your global `store` (from your main bot) if present,
    so allowlist checks and autodelete persistence match your Utilabot behavior.
    To enable that, set `bot.store = store` in your main after initializing the Store.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # If your main sets `bot.store = store`, we'll reuse it.
        self.store = getattr(bot, "store", None)

    # ---------- helpers ----------
    def _is_admin_or_allowlisted(self, inter: discord.Interaction) -> bool:
        if _has_guild_admin_perms(inter):
            return True
        # Optional allowlist via your existing Store
        try:
            if self.store and hasattr(self.store, "is_allowlisted"):
                return bool(self.store.is_allowlisted(inter.user.id))
        except Exception:
            pass
        return False

    async def _require_text_channel(self, inter: discord.Interaction) -> bool:
        if not isinstance(inter.channel, (discord.TextChannel, discord.Thread)):
            await inter.response.send_message("Use this in a text channel.", ephemeral=True)
            return False
        return True

    # ---------- /purge ----------
    @app_commands.command(name="purge", description="Bulk delete recent messages (max 1000).")
    @app_commands.describe(limit="Number of recent messages to scan (1-1000)", user="Only delete messages by this user")
    async def purge(
        self,
        inter: discord.Interaction,
        limit: app_commands.Range[int, 1, 1000],
        user: Optional[discord.User] = None,
    ):
        if not await self._require_text_channel(inter):
            return
        if not self._is_admin_or_allowlisted(inter):
            return await inter.response.send_message(
                "You need **Administrator/Manage Server** or be on the bot's admin allowlist.", ephemeral=True
            )
        def check(m: discord.Message):
            if getattr(m, "pinned", False):
                return False
            return (user is None) or (m.author.id == user.id)

        await inter.response.defer(ephemeral=True)
        try:
            deleted = await inter.channel.purge(limit=limit, check=check, bulk=True)
            await inter.followup.send(f"🧹 Deleted **{len(deleted)}** messages.", ephemeral=True)
        except discord.Forbidden:
            await inter.followup.send(
                "I need the **Manage Messages** and **Read Message History** permissions.", ephemeral=True
            )
        except discord.HTTPException as e:
            await inter.followup.send(f"Error while deleting: {e}", ephemeral=True)

    # ---------- /autodelete set|disable|status|list ----------
    @app_commands.command(name="autodelete", description="Manage auto-delete for this channel (set/disable/status/list).")
    @app_commands.describe(
        action="Choose what to do",
        value="For 'set': duration like 10s, 2m, or just 2 (minutes). Ignored for other actions."
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="set", value="set"),
            app_commands.Choice(name="disable", value="disable"),
            app_commands.Choice(name="status", value="status"),
            app_commands.Choice(name="list", value="list"),
        ]
    )
    async def autodelete(
        self,
        inter: discord.Interaction,
        action: app_commands.Choice[str],
        value: Optional[str] = None,
    ):
        if not await self._require_text_channel(inter):
            return

        act = action.value

        # Read current map if store available
        ad_map = {}
        try:
            if self.store and hasattr(self.store, "get_autodelete"):
                ad_map = self.store.get_autodelete() or {}
        except Exception:
            ad_map = {}

        if act == "status":
            seconds = int(ad_map.get(str(inter.channel.id), 0))
            if seconds <= 0:
                return await inter.response.send_message("ℹ️ Auto-delete is **off** for this channel.", ephemeral=True)
            pretty = self._pretty_seconds(seconds)
            return await inter.response.send_message(
                f"🗑️ Auto-delete is **on** for this channel: older than **{pretty}**.", ephemeral=True
            )

        if act == "list":
            if not self._is_admin_or_allowlisted(inter):
                return await inter.response.send_message(
                    "You need **Administrator/Manage Server** or be on the bot's admin allowlist.", ephemeral=True
                )
            if not ad_map:
                return await inter.response.send_message("No channels have auto-delete configured.", ephemeral=True)
            # Try to resolve channel names in this guild only
            lines = []
            for cid, secs in ad_map.items():
                channel = inter.guild.get_channel(int(cid)) if inter.guild else None
                name = f"#{channel.name}" if channel else f"<#{cid}>"
                lines.append(f"{name} → {self._pretty_seconds(int(secs))}")
            text = "\n".join(lines)
            return await inter.response.send_message(f"**Auto-delete list:**\n{text}", ephemeral=True)

        # set/disable require admin/allowlist + store
        if not self._is_admin_or_allowlisted(inter):
            return await inter.response.send_message(
                "You need **Administrator/Manage Server** or be on the bot's admin allowlist.", ephemeral=True
            )
        if not self.store:
            return await inter.response.send_message(
                "Auto-delete persistence requires `bot.store`. Please attach your Store to the bot.", ephemeral=True
            )

        if act == "disable":
            try:
                self.store.remove_autodelete(inter.channel.id)
            except Exception as e:
                return await inter.response.send_message(f"Error disabling: {e}", ephemeral=True)
            return await inter.response.send_message("🛑 Auto-delete disabled for this channel.", ephemeral=True)

        if act == "set":
            if not value:
                return await inter.response.send_message(
                    "Provide a duration like **10s**, **2m**, or just **2** (minutes).", ephemeral=True
                )
            seconds = self._parse_duration_to_seconds(value.strip().lower())
            if seconds is None:
                return await inter.response.send_message(
                    "Invalid format. Use **10s**, **2m**, or a number for minutes.", ephemeral=True
                )
            if seconds < 5 or seconds > 86_400:
                return await inter.response.send_message(
                    "Range must be **5 seconds** to **24 hours**.", ephemeral=True
                )
            try:
                self.store.set_autodelete(inter.channel.id, int(seconds))
            except Exception as e:
                return await inter.response.send_message(f"Error saving: {e}", ephemeral=True)

            return await inter.response.send_message(
                f"🗑️ Auto-delete enabled: older than **{self._pretty_seconds(seconds)}**.", ephemeral=True
            )

    # ---------- helpers ----------
    @staticmethod
    def _parse_duration_to_seconds(s: str) -> Optional[int]:
        # Accept "10s", "2m", "2", with small whitespace tolerance
        m = re.fullmatch(r"\s*(\d+)\s*([sm]?)\s*", s)
        if not m:
            return None
        val = int(m.group(1))
        unit = m.group(2) or "m"  # default minutes
        if unit == "s":
            return val
        if unit == "m":
            return val * 60
        return None

    @staticmethod
    def _pretty_seconds(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds} seconds"
        if seconds % 3600 == 0:
            return f"{seconds // 3600} hours"
        if seconds % 60 == 0:
            return f"{seconds // 60} minutes"
        return f"{seconds // 60} minutes {seconds % 60} seconds"


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
