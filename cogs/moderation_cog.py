# cogs/moderation.py
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
from cogs.admin_gates import gated

import discord
from discord import app_commands
from discord.ext import commands, tasks


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
    Moderation utilities:
      - /purge (bulk recent)
      - /purge_all (delete entire channel history, skips pinned)
      - /autodelete set|disable|status|list
      - runtime deletion (per-message if <60s, periodic sweep for >=60s)

    Store abstraction supports:
      - Store with set_autodelete/remove_autodelete/get_autodelete()
      - WxStore with set_config/delete_config/get_config/get_config_all()
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = getattr(bot, "store", None)
        if not getattr(self, "_sweeper_started", False):
            self.cleanup_loop.start()
            self._sweeper_started = True

    # ---------- helpers ----------
    def _is_admin_or_allowlisted(self, inter: discord.Interaction) -> bool:
        if _has_guild_admin_perms(inter):
            return True
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

    # ---- persistence abstraction (supports both Store and WxStore) ----
    def _ad_key(self, channel_id: int) -> str:
        return f"autodelete:{int(channel_id)}"

    def _ad_set(self, channel_id: int, seconds: int) -> None:
        if not self.store:
            raise RuntimeError("No store attached")
        if hasattr(self.store, "set_autodelete"):
            return self.store.set_autodelete(int(channel_id), int(seconds))
        if hasattr(self.store, "set_config"):
            return self.store.set_config(self._ad_key(channel_id), int(seconds))
        raise AttributeError("Store missing set_autodelete and set_config")

    def _ad_remove(self, channel_id: int) -> None:
        if not self.store:
            raise RuntimeError("No store attached")
        if hasattr(self.store, "remove_autodelete"):
            return self.store.remove_autodelete(int(channel_id))
        if hasattr(self.store, "delete_config"):
            return self.store.delete_config(self._ad_key(channel_id))
        raise AttributeError("Store missing remove_autodelete and delete_config")

    def _ad_get_map(self) -> Dict[str, int]:
        """Returns {channel_id_str: seconds}."""
        if not self.store:
            return {}
        if hasattr(self.store, "get_autodelete"):
            try:
                return {str(k): int(v) for k, v in (self.store.get_autodelete() or {}).items()}
            except Exception:
                pass
        if hasattr(self.store, "get_config_all"):
            try:
                raw = self.store.get_config_all() or {}
                out = {}
                for k, v in raw.items():
                    if isinstance(k, str) and k.startswith("autodelete:"):
                        try:
                            cid = k.split(":", 1)[1]
                            out[str(int(cid))] = int(v)
                        except Exception:
                            continue
                return out
            except Exception:
                pass
        return {}

    def _ad_get_for_channel(self, channel_id: int) -> int:
        """Returns seconds for channel or 0 if off, even for stores without listing."""
        if not self.store:
            return 0
        m = self._ad_get_map()
        if m:
            return int(m.get(str(int(channel_id)), 0))
        if hasattr(self.store, "get_config"):
            try:
                v = self.store.get_config(self._ad_key(channel_id))
                return int(v) if v is not None else 0
            except Exception:
                return 0
        return 0

    # ---------- /purge (bulk recent) ----------
    @app_commands.command(name="purge", description="Bulk delete recent messages (max 1000, ≤14 days).")
    @app_commands.describe(limit="Number of recent messages to scan (1-1000)", user="Only delete messages by this user")
    @gated()
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

    # ---------- /purge_all (all history, any age) ----------
    @app_commands.command(
        name="purge_all",
        description="Delete ALL messages in this channel, regardless of age (skips pinned)."
    )
    @app_commands.describe(
        confirm="Must be true to proceed (safety check).",
        user="Optional: only delete messages by this user"
    )
    @gated()
    async def purge_all(
        self,
        inter: discord.Interaction,
        confirm: bool,
        user: Optional[discord.User] = None,
    ):
        if not await self._require_text_channel(inter):
            return
        if not self._is_admin_or_allowlisted(inter):
            return await inter.response.send_message(
                "You need **Administrator/Manage Server** or be on the bot's admin allowlist.", ephemeral=True
            )
        if not confirm:
            return await inter.response.send_message(
                "⚠️ Confirmation required. Re-run with `confirm: true`.", ephemeral=True
            )

        # Permission sanity
        try:
            perms = inter.channel.permissions_for(inter.guild.me) if inter.guild else None
            if not perms or not perms.manage_messages or not perms.read_message_history:
                return await inter.response.send_message(
                    "I need **Manage Messages** and **Read Message History**.", ephemeral=True
                )
        except Exception:
            pass

        await inter.response.defer(ephemeral=True, thinking=True)

        deleted = 0
        try:
            # Iterate entire history and delete one-by-one to bypass 14-day bulk limit.
            async for m in inter.channel.history(limit=None, oldest_first=False):
                if getattr(m, "pinned", False):
                    continue
                if user and m.author.id != user.id:
                    continue
                try:
                    await m.delete()
                    deleted += 1
                except Exception:
                    # soft-fail on individual messages (rate-limits/errors)
                    pass
                # Gentle pacing to play nice with rate limits
                if deleted % 25 == 0:
                    await asyncio.sleep(1)
        except Exception as e:
            return await inter.followup.send(f"Stopped early due to an error: {e}\nDeleted so far: **{deleted}**.", ephemeral=True)

        await inter.followup.send(f"🧨 Purge complete. Deleted **{deleted}** messages.", ephemeral=True)

    # ---------- /autodelete set|disable|status|list ----------
    @app_commands.command(name="autodelete", description="Manage auto-delete for this channel (set/disable/status/list).")
    @app_commands.describe(
        action="Choose what to do",
        value="For 'set': duration like '45s', '10m', '1h', '2d', or mixed '1d 2h 30m'. Or just '5' (minutes)."
    )
    @gated()
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

        if act == "status":
            seconds = self._ad_get_for_channel(inter.channel.id)
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

            ad_map = self._ad_get_map()
            if not ad_map:
                return await inter.response.send_message(
                    "No channels have auto-delete configured.", ephemeral=True
                )

            # Build rows only for this guild; include text channels and threads
            rows = []
            for cid, secs in ad_map.items():
                ch = self.bot.get_channel(int(cid))
                if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                    continue
                if not inter.guild or not ch.guild or ch.guild.id != inter.guild.id:
                    continue
                name = getattr(ch, "name", str(cid))
                rows.append((name.lower(), ch.mention, int(secs)))

            if not rows:
                return await inter.response.send_message(
                    "No channels in this guild have auto-delete configured.", ephemeral=True
                )

            rows.sort(key=lambda t: t[0])  # sort by lowercase name
            lines = [f"{mention} → {self._pretty_seconds(secs)}" for _, mention, secs in rows]
            text = "\n".join(lines)
            return await inter.response.send_message(
                f"**Auto-delete list (this guild):**\n{text}", ephemeral=True
            )

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
                self._ad_remove(inter.channel.id)
            except Exception as e:
                return await inter.response.send_message(f"Error disabling: {e}", ephemeral=True)
            return await inter.response.send_message("🛑 Auto-delete disabled for this channel.", ephemeral=True)

        if act == "set":
            if not value:
                return await inter.response.send_message(
                    "Provide a duration like **45s**, **10m**, **1h**, **2d**, or mixed **1d 2h 30m**. "
                    "A plain number (e.g. **5**) is minutes.",
                    ephemeral=True
                )
            seconds = self._parse_duration_to_seconds(value)
            if seconds is None:
                return await inter.response.send_message(
                    "Invalid format. Examples: **45s**, **10m**, **1h**, **2d**, **1d 2h 30m**, or **5** (minutes).",
                    ephemeral=True
                )
            if seconds < 5 or seconds > 2_592_000:  # 30 days max
                return await inter.response.send_message(
                    "Range must be **5 seconds** to **30 days**.", ephemeral=True
                )
            try:
                self._ad_set(inter.channel.id, int(seconds))
            except Exception as e:
                return await inter.response.send_message(f"Error saving: {e}", ephemeral=True)

            return await inter.response.send_message(
                f"🗑️ Auto-delete enabled: older than **{self._pretty_seconds(seconds)}**.", ephemeral=True
            )

    # ---------- deletion runtime ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Skip system/DMs
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        # Bot perms needed
        try:
            perms = message.channel.permissions_for(message.guild.me) if message.guild else None
            if not perms or not perms.manage_messages:
                return
        except Exception:
            return
        try:
            secs = self._ad_get_for_channel(message.channel.id)
            if secs and secs < 60:
                # schedule per-message delete
                asyncio.create_task(self._schedule_autodelete(message, secs))
        except Exception:
            pass

    async def _schedule_autodelete(self, message: discord.Message, seconds: int):
        try:
            await asyncio.sleep(max(1, int(seconds)))
            # re-fetch and respect pins
            try:
                msg = await message.channel.fetch_message(message.id)
            except Exception:
                return
            if getattr(msg, "pinned", False):
                return
            await msg.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        except Exception:
            pass

    @tasks.loop(minutes=2)
    async def cleanup_loop(self):
        try:
            conf = self._ad_get_map()
            if not conf:
                return
            now = datetime.now(timezone.utc)
            for chan_id, secs in list(conf.items()):
                if secs < 60:
                    continue  # handled per-message
                channel = self.bot.get_channel(int(chan_id))
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    continue
                try:
                    perms = channel.permissions_for(channel.guild.me) if getattr(channel, "guild", None) else None
                    if not perms or not perms.manage_messages or not perms.read_message_history:
                        continue
                except Exception:
                    continue
                cutoff = now - timedelta(seconds=int(secs))
                try:
                    # delete messages older than cutoff (skip pinned)
                    async for m in channel.history(limit=200, before=None, oldest_first=False):
                        if getattr(m, "pinned", False):
                            continue
                        if m.created_at and m.created_at.replace(tzinfo=timezone.utc) <= cutoff:
                            try:
                                await m.delete()
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

    @cleanup_loop.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # ---------- helpers ----------
    @staticmethod
    def _parse_duration_to_seconds(s: str) -> Optional[int]:
        """
        Accepts:
          - Mixed units: "1d 2h 30m 10s", "2h", "90m", "45s", etc.
          - Plain number: "5" (minutes).
        Units: d=days, h=hours, m=minutes, s=seconds. Case-insensitive.
        """
        if not isinstance(s, str):
            return None
        s = s.strip().lower()

        # Plain number means minutes
        if re.fullmatch(r"\d+", s):
            return int(s) * 60

        total = 0
        matched_any = False
        for num, unit in re.findall(r"(\d+)\s*([dhms])", s):
            matched_any = True
            val = int(num)
            if unit == "d":
                total += val * 86400
            elif unit == "h":
                total += val * 3600
            elif unit == "m":
                total += val * 60
            elif unit == "s":
                total += val
        return total if matched_any and total > 0 else None

    @staticmethod
    def _pretty_seconds(seconds: int) -> str:
        # Format up to days with components
        parts = []
        d, rem = divmod(seconds, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        if d:
            parts.append(f"{d} day{'s' if d != 1 else ''}")
        if h:
            parts.append(f"{h} hour{'s' if h != 1 else ''}")
        if m:
            parts.append(f"{m} minute{'s' if m != 1 else ''}")
        if s and seconds < 60 or (s and not (d or h or m)):
            # show seconds when <60, or when it's the only unit
            parts.append(f"{s} second{'s' if s != 1 else ''}")
        return " ".join(parts) if parts else "0 seconds"


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
