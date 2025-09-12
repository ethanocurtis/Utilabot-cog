from __future__ import annotations

import json
import os
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Any

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_PATH = "data/sticky_config.json"
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)

# =========================
# Models & Persistence
# =========================
@dataclass
class StickyEmbedSpec:
    title: Optional[str] = None
    description: Optional[str] = None
    color: Optional[int] = None  # decimal int (0xRRGGBB)
    thumbnail_url: Optional[str] = None
    image_url: Optional[str] = None
    footer: Optional[str] = None

    def to_embed(self) -> discord.Embed:
        color = discord.Color(self.color) if self.color is not None else discord.Embed.Empty
        embed = discord.Embed(
            title=self.title or discord.Embed.Empty,
            description=self.description or discord.Embed.Empty,
            color=color,
        )
        if self.thumbnail_url:
            embed.set_thumbnail(url=self.thumbnail_url)
        if self.image_url:
            embed.set_image(url=self.image_url)
        if self.footer:
            embed.set_footer(text=self.footer)
        return embed

@dataclass
class StickyConfig:
    mode: str  # "text" | "embed"
    text: Optional[str] = None
    embed: Optional[StickyEmbedSpec] = None
    pinned: bool = False
    cooldown_messages: int = 0  # Only re-post after this many non-bot messages
    enabled: bool = True
    last_message_id: Optional[int] = None  # The current sticky message id
    messages_since: int = 0
    author_id: Optional[int] = None  # who set it

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.embed is not None:
            d["embed"] = asdict(self.embed)
        return d


class StickyStore:
    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    def _chan_key(self, channel_id: int) -> str:
        return str(channel_id)

    async def load(self) -> None:
        if not os.path.exists(self.path):
            self._data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._data = raw if isinstance(raw, dict) else {}
        except Exception:
            self._data = {}

    async def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    async def set_config(self, guild_id: int, channel_id: int, cfg: StickyConfig) -> None:
        async with self._lock:
            g = self._data.setdefault(self._guild_key(guild_id), {})
            g[self._chan_key(channel_id)] = cfg.to_dict()
            await self.save()

    async def get_config(self, guild_id: int, channel_id: int) -> Optional[StickyConfig]:
        g = self._data.get(self._guild_key(guild_id), {})
        raw = g.get(self._chan_key(channel_id))
        if not raw:
            return None
        embed = None
        if raw.get("embed"):
            e = raw["embed"]
            embed = StickyEmbedSpec(
                title=e.get("title"),
                description=e.get("description"),
                color=e.get("color"),
                thumbnail_url=e.get("thumbnail_url"),
                image_url=e.get("image_url"),
                footer=e.get("footer"),
            )
        return StickyConfig(
            mode=raw.get("mode", "text"),
            text=raw.get("text"),
            embed=embed,
            pinned=raw.get("pinned", False),
            cooldown_messages=raw.get("cooldown_messages", 0),
            enabled=raw.get("enabled", True),
            last_message_id=raw.get("last_message_id"),
            messages_since=raw.get("messages_since", 0),
            author_id=raw.get("author_id"),
        )

    async def del_config(self, guild_id: int, channel_id: int) -> None:
        async with self._lock:
            gkey = self._guild_key(guild_id)
            g = self._data.get(gkey)
            if g and self._chan_key(channel_id) in g:
                g.pop(self._chan_key(channel_id), None)
                if not g:
                    self._data.pop(gkey, None)
                await self.save()

    async def list_channels(self, guild_id: int) -> Dict[int, StickyConfig]:
        g = self._data.get(self._guild_key(guild_id), {})
        out: Dict[int, StickyConfig] = {}
        for ch_id_str, raw in g.items():
            cfg = await self.get_config(guild_id, int(ch_id_str))
            if cfg:
                out[int(ch_id_str)] = cfg
        return out

    async def update_runtime(self, guild_id: int, channel_id: int, **fields) -> None:
        async with self._lock:
            g = self._data.setdefault(self._guild_key(guild_id), {})
            ch = g.setdefault(self._chan_key(channel_id), {})
            ch.update(fields)
            await self.save()


# =========================
# Cog
# =========================
class StickyCog(commands.Cog):
    """
    Sticky Notes Cog

    Requirements:
    - Bot should have Manage Messages to delete/repost stickies.
    - If `pinned=True`, bot needs Manage Messages to pin/unpin.
    - For best results, enable Message Content intent (not strictly required).
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = StickyStore(CONFIG_PATH)
        self.allowed_role_cache: Dict[int, Optional[int]] = {}  # guild_id -> role_id or None
        self._ready = False
        self._message_lock = asyncio.Lock()

    async def cog_load(self):
        await self.store.load()

    # ------------- Permissions helpers -------------
    def _has_manage_perms(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None or not isinstance(interaction.user, discord.Member):
            return False
        m: discord.Member = interaction.user
        if m.guild_permissions.administrator or m.guild_permissions.manage_guild or m.guild_permissions.manage_messages:
            return True
        # Optional role gate
        rid = self.allowed_role_cache.get(interaction.guild_id or 0)
        if rid and m.get_role(rid):
            return True
        return False

    # ------------- Command tree -------------
    sticky = app_commands.Group(name="sticky", description="Manage sticky notes in this channel")

    @sticky.command(name="set-text", description="Set a text sticky in this channel")
    @app_commands.describe(
        message="The sticky text",
        pinned="Pin the sticky message (bot needs Manage Messages)",
        cooldown_messages="Repost only after this many non-bot messages (0 = every message)",
    )
    async def set_text(self, interaction: discord.Interaction, message: str, pinned: bool = False, cooldown_messages: app_commands.Range[int, 0, 100] = 0):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to manage stickies.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)

        cfg = StickyConfig(
            mode="text",
            text=message,
            embed=None,
            pinned=pinned,
            cooldown_messages=int(cooldown_messages or 0),
            enabled=True,
            last_message_id=None,
            messages_since=0,
            author_id=interaction.user.id,
        )
        await self.store.set_config(interaction.guild_id, channel.id, cfg)
        await interaction.response.send_message("Sticky set. I will keep it at the bottom.", ephemeral=True)
        # Immediately (re)post a fresh sticky
        await self._refresh_sticky(channel, cfg)

    @sticky.command(name="set-embed", description="Set an embed sticky in this channel")
    @app_commands.describe(
        description="Embed description (supports Markdown)",
        title="Optional title",
        color_hex="Hex color like #7db2ff or 7db2ff",
        thumbnail_url="Optional thumbnail URL",
        image_url="Optional image URL",
        footer="Optional footer text",
        pinned="Pin the sticky message",
        cooldown_messages="Repost only after this many non-bot messages (0 = every message)",
    )
    async def set_embed(self, interaction: discord.Interaction,
                        description: str,
                        title: Optional[str] = None,
                        color_hex: Optional[str] = None,
                        thumbnail_url: Optional[str] = None,
                        image_url: Optional[str] = None,
                        footer: Optional[str] = None,
                        pinned: bool = False,
                        cooldown_messages: app_commands.Range[int, 0, 100] = 0):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to manage stickies.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)

        color_int = None
        if color_hex:
            h = color_hex.strip().lstrip('#')
            try:
                color_int = int(h, 16)
            except ValueError:
                return await interaction.response.send_message("Invalid color. Use hex like #7db2ff.", ephemeral=True)

        spec = StickyEmbedSpec(
            title=title,
            description=description,
            color=color_int,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            footer=footer,
        )
        cfg = StickyConfig(
            mode="embed",
            embed=spec,
            text=None,
            pinned=pinned,
            cooldown_messages=int(cooldown_messages or 0),
            enabled=True,
            last_message_id=None,
            messages_since=0,
            author_id=interaction.user.id,
        )
        await self.store.set_config(interaction.guild_id, channel.id, cfg)
        await interaction.response.send_message("Embed sticky set. I will keep it at the bottom.", ephemeral=True)
        await self._refresh_sticky(channel, cfg)

    @sticky.command(name="edit", description="Edit the current sticky (text or embed description)")
    @app_commands.describe(new_text="New text (for text mode) or new description (for embed mode)")
    async def edit(self, interaction: discord.Interaction, new_text: str):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to manage stickies.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)

        cfg = await self.store.get_config(interaction.guild_id, channel.id)
        if not cfg:
            return await interaction.response.send_message("No sticky configured in this channel.", ephemeral=True)

        if cfg.mode == "text":
            cfg.text = new_text
        else:
            if not cfg.embed:
                cfg.embed = StickyEmbedSpec(description=new_text)
            else:
                cfg.embed.description = new_text

        # Reset runtime so we re-post immediately
        cfg.last_message_id = None
        cfg.messages_since = 0
        await self.store.set_config(interaction.guild_id, channel.id, cfg)
        await interaction.response.send_message("Sticky updated.", ephemeral=True)
        await self._refresh_sticky(channel, cfg)

    @sticky.command(name="show", description="Show the current sticky config")
    async def show(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        cfg = await self.store.get_config(interaction.guild_id, channel.id)
        if not cfg:
            return await interaction.response.send_message("No sticky configured in this channel.", ephemeral=True)

        desc = f"Mode: **{cfg.mode}**\nPinned: **{cfg.pinned}**\nCooldown messages: **{cfg.cooldown_messages}**\nEnabled: **{cfg.enabled}**"
        if cfg.mode == "text" and cfg.text:
            desc += f"\n\nPreview (text):\n{cfg.text[:4000]}"
        elif cfg.mode == "embed" and cfg.embed:
            desc += "\n\nPreview below."
        await interaction.response.send_message(desc, ephemeral=True)
        # Send preview embed (ephemeral messages can include embeds)
        if cfg.mode == "embed" and cfg.embed:
            await interaction.followup.send(embed=cfg.embed.to_embed(), ephemeral=True)

    @sticky.command(name="disable", description="Disable the sticky in this channel")
    async def disable(self, interaction: discord.Interaction):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to manage stickies.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)

        cfg = await self.store.get_config(interaction.guild_id, channel.id)
        if not cfg:
            return await interaction.response.send_message("No sticky configured in this channel.", ephemeral=True)

        await self._delete_existing_sticky_message(channel, cfg)
        cfg.enabled = False
        cfg.last_message_id = None
        await self.store.set_config(interaction.guild_id, channel.id, cfg)
        await interaction.response.send_message("Sticky disabled for this channel.", ephemeral=True)

    @sticky.command(name="enable", description="Enable (or re-enable) the sticky in this channel")
    async def enable(self, interaction: discord.Interaction):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to manage stickies.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)

        cfg = await self.store.get_config(interaction.guild_id, channel.id)
        if not cfg:
            return await interaction.response.send_message("No sticky configured in this channel.", ephemeral=True)
        cfg.enabled = True
        cfg.last_message_id = None
        cfg.messages_since = 0
        await self.store.set_config(interaction.guild_id, channel.id, cfg)
        await interaction.response.send_message("Sticky enabled.", ephemeral=True)
        await self._refresh_sticky(channel, cfg)

    @sticky.command(name="list", description="List all active stickies in this server")
    async def list_cmd(self, interaction: discord.Interaction):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to list stickies.", ephemeral=True)
        if not interaction.guild_id:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        items = await self.store.list_channels(interaction.guild_id)
        if not items:
            return await interaction.response.send_message("No stickies configured in this server.", ephemeral=True)
        lines = []
        for ch_id, cfg in items.items():
            lines.append(f"<#{ch_id}> — mode **{cfg.mode}**, pinned **{cfg.pinned}**, cooldown **{cfg.cooldown_messages}**, enabled **{cfg.enabled}**")
        await interaction.response.send_message("\n".join(lines)[:1990], ephemeral=True)

    @sticky.command(name="cooldown", description="Set the message-count cooldown for this channel's sticky")
    @app_commands.describe(count="Repost only after this many non-bot messages (0 = every message)")
    async def cooldown(self, interaction: discord.Interaction, count: app_commands.Range[int, 0, 100]):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to manage stickies.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        cfg = await self.store.get_config(interaction.guild_id, channel.id)
        if not cfg:
            return await interaction.response.send_message("No sticky configured in this channel.", ephemeral=True)
        await self.store.update_runtime(interaction.guild_id, channel.id, cooldown_messages=int(count))
        await interaction.response.send_message(f"Cooldown set to {int(count)} messages.", ephemeral=True)

    @sticky.command(name="pin", description="Enable or disable pinning of the sticky message in this channel")
    @app_commands.describe(value="Whether the sticky should be pinned")
    async def pin_toggle(self, interaction: discord.Interaction, value: bool):
        if not self._has_manage_perms(interaction):
            return await interaction.response.send_message("You lack permission to manage stickies.", ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        cfg = await self.store.get_config(interaction.guild_id, channel.id)
        if not cfg:
            return await interaction.response.send_message("No sticky configured in this channel.", ephemeral=True)
        cfg.pinned = value
        cfg.last_message_id = None  # force refresh to apply pin state
        await self.store.set_config(interaction.guild_id, channel.id, cfg)
        await interaction.response.send_message(f"Pinning is now {'enabled' if value else 'disabled'}.", ephemeral=True)
        await self._refresh_sticky(channel, cfg)

    # Optional: restrict management to a specific role (per guild)
    @sticky.command(name="perm-role", description="(Optional) Set a role allowed to manage stickies on this server")
    @app_commands.describe(role="Role that can manage stickies (in addition to admins/mods)")
    async def perm_role(self, interaction: discord.Interaction, role: Optional[discord.Role]):
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
            return await interaction.response.send_message("Only administrators or Manage Server can set this.", ephemeral=True)
        gid = interaction.guild_id or 0
        self.allowed_role_cache[gid] = role.id if role else None
        await interaction.response.send_message(
            f"Sticky manager role set to: {role.mention if role else 'None'}", ephemeral=True
        )

    # ------------- Runtime behavior -------------
    @commands.Cog.listener()
    async def on_ready(self):
        # Load once more in case cog loaded before ready
        await self.store.load()
        self._ready = True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore DM, bots, non-text channels
        if not self._ready or not message.guild or message.author.bot:
            return
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        cfg = await self.store.get_config(message.guild.id, channel.id)
        if not cfg or not cfg.enabled:
            return

        # If the message is the current sticky itself, ignore
        if cfg.last_message_id and message.id == cfg.last_message_id:
            return

        # Update message counter and conditionally refresh
        cfg.messages_since = (cfg.messages_since or 0) + 1
        await self.store.update_runtime(message.guild.id, channel.id, messages_since=cfg.messages_since)

        threshold = cfg.cooldown_messages or 0
        if threshold == 0 or cfg.messages_since >= threshold:
            await self._refresh_sticky(channel, cfg)

    # ------------- Helpers -------------
    async def _delete_existing_sticky_message(self, channel: discord.abc.MessageableChannel, cfg: StickyConfig):
        if not cfg.last_message_id:
            return
        try:
            msg = await channel.fetch_message(cfg.last_message_id)
        except Exception:
            msg = None
        if msg:
            try:
                # If pinned, unpin the old one first to keep pin list tidy
                if cfg.pinned and getattr(msg, "pinned", False):
                    try:
                        await msg.unpin(reason="Refreshing sticky")
                    except Exception:
                        pass
                await msg.delete()
            except Exception:
                pass
        # Clear it regardless (we don't want stale IDs around)
        await self.store.update_runtime(channel.guild.id, channel.id, last_message_id=None)

    async def _refresh_sticky(self, channel: discord.abc.MessageableChannel, cfg: StickyConfig):
        async with self._message_lock:
            # Delete old
            await self._delete_existing_sticky_message(channel, cfg)
            # Post new
            try:
                if cfg.mode == "embed" and cfg.embed:
                    msg = await channel.send(embed=cfg.embed.to_embed())
                else:
                    content = cfg.text or ""
                    msg = await channel.send(content)
                # Optionally pin
                if cfg.pinned and isinstance(channel, (discord.TextChannel, discord.Thread)):
                    try:
                        await msg.pin(reason="Sticky message")
                    except Exception:
                        pass
                # Save new message id & reset counter
                await self.store.update_runtime(channel.guild.id, channel.id,
                                               last_message_id=msg.id,
                                               messages_since=0)
            except discord.Forbidden:
                # Lacking perms
                if isinstance(channel, (discord.TextChannel, discord.Thread)):
                    try:
                        await channel.send(
                            "I couldn't post or pin the sticky due to missing permissions (Manage Messages / Send Messages)."
                        )
                    except Exception:
                        pass
            except Exception:
                # Swallow unexpected errors to avoid loops
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StickyCog(bot))
