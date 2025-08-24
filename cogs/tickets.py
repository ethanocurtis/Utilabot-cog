# cogs/tickets.py
# A clean ticket system cog for discord.py 2.x (slash commands + buttons + persistent views)
# Features:
# - /tickets setup: configure staff role, ticket category, and spawn a "Create Ticket" panel with a button
# - Button creates a private ticket channel (ticket-###) visible to opener + staff
# - /ticket add, /ticket remove, /ticket rename, /ticket close, /ticket claim, /ticket unclaim, /ticket transcript
# - JSON persistence per-guild (data/tickets.json)
# - Works in Docker; no DB required
#
# Drop this file into your cogs/ folder and load it.
# Requires discord.py 2.3+
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "tickets.json")

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def _utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

@dataclass
class GuildConfig:
    guild_id: int
    staff_role_id: Optional[int] = None
    category_id: Optional[int] = None
    counter: int = 1
    panel_message_id: Optional[int] = None
    panel_channel_id: Optional[int] = None

class TicketStore:
    """Simple JSON persistence for guild ticket configs and per-ticket metadata."""
    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"guilds": {}, "tickets": {}}
        _ensure_data_dir()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                # keep empty on error
                self.data = {"guilds": {}, "tickets": {}}
        else:
            self._save()

    def _save(self):
        tmp = json.dumps(self.data, indent=2, ensure_ascii=False)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(tmp)

    async def with_lock(self):
        return self.lock

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        async with self.lock:
            g = self.data["guilds"].get(str(guild_id))
            if not g:
                cfg = GuildConfig(guild_id=guild_id)
                self.data["guilds"][str(guild_id)] = asdict(cfg)
                self._save()
                return cfg
            return GuildConfig(**g)

    async def set_guild_config(self, cfg: GuildConfig):
        async with self.lock:
            self.data["guilds"][str(cfg.guild_id)] = asdict(cfg)
            self._save()

    async def next_ticket_number(self, guild_id: int) -> int:
        async with self.lock:
            g = self.data["guilds"].setdefault(str(guild_id), asdict(GuildConfig(guild_id=guild_id)))
            n = g.get("counter", 1)
            g["counter"] = int(n) + 1
            self._save()
            return int(n)

    async def remember_ticket(self, channel_id: int, info: Dict[str, Any]):
        async with self.lock:
            self.data["tickets"][str(channel_id)] = info
            self._save()

    async def get_ticket(self, channel_id: int) -> Optional[Dict[str, Any]]:
        async with self.lock:
            return self.data["tickets"].get(str(channel_id))

    async def forget_ticket(self, channel_id: int):
        async with self.lock:
            if str(channel_id) in self.data["tickets"]:
                del self.data["tickets"][str(channel_id)]
                self._save()


class CreateTicketView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: TicketStore, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.store = store

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Tickets can only be created in a server.", ephemeral=True)

        guild = interaction.guild
        opener = interaction.user
        cfg = await self.store.get_guild_config(guild.id)

        if not cfg.category_id or not cfg.staff_role_id:
            return await interaction.response.send_message(
                "Ticket system isn't set up yet. An admin should run **/tickets setup**.", ephemeral=True
            )

        category = guild.get_channel(cfg.category_id)
        staff_role = guild.get_role(cfg.staff_role_id)
        if not isinstance(category, discord.CategoryChannel) or not staff_role:
            return await interaction.response.send_message(
                "Configured category or staff role no longer exists. Please re-run **/tickets setup**.", ephemeral=True
            )

        number = await self.store.next_ticket_number(guild.id)
        name = f"ticket-{number:04d}"

        # Channel permissions: staff + opener + bot can see; @everyone denied
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
        }
        channel = await guild.create_text_channel(name=name, category=category, overwrites=overwrites, reason=f"Ticket by {opener}")

        await self.store.remember_ticket(channel.id, {
            "guild_id": guild.id,
            "opener_id": opener.id,
            "created_at": _utc_now_str(),
            "number": number,
            "claimed_by": None,
        })

        embed = discord.Embed(
            title=f"Ticket #{number:04d}",
            description=(
                f"Hello {opener.mention}! A staff member will be with you shortly.\n\n"
                "Use `/ticket add` to add someone, `/ticket remove` to remove, and `/ticket close` when done."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Ticket System")
        await channel.send(content=opener.mention, embed=embed)

        try:
            await interaction.response.send_message(f"Created {channel.mention}", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"Created {channel.mention}", ephemeral=True)


class Tickets(commands.Cog):
    """Support tickets with a button panel + slash commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_data_dir()
        self.store = TicketStore(DATA_FILE)

        # Register persistent view so the button works after restarts
        self.bot.add_view(CreateTicketView(bot, self.store))

    # ---- Admin setup ----

    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.command(name="tickets_setup", description="Configure the ticket system & post a Create Ticket panel.")
    @app_commands.describe(
        staff_role="Role that should see and manage tickets",
        category="Category where ticket channels will be created",
        panel_channel="Channel to post the Create Ticket panel"
    )
    async def tickets_setup(
        self,
        interaction: discord.Interaction,
        staff_role: discord.Role,
        category: discord.CategoryChannel,
        panel_channel: discord.TextChannel
    ):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.staff_role_id = staff_role.id
        cfg.category_id = category.id
        await self.store.set_guild_config(cfg)

        view = CreateTicketView(self.bot, self.store)
        embed = discord.Embed(
            title="Need Help?",
            description="Press the button below to open a private ticket with our staff.",
            color=discord.Color.green()
        )
        msg = await panel_channel.send(embed=embed, view=view)

        cfg.panel_channel_id = panel_channel.id
        cfg.panel_message_id = msg.id
        await self.store.set_guild_config(cfg)

        await interaction.response.send_message(
            f"✅ Ticket system configured. Panel posted in {panel_channel.mention}.",
            ephemeral=True
        )

    # ---- Ticket commands ----

    ticket = app_commands.Group(name="ticket", description="Manage the current ticket")

    @ticket.command(name="add", description="Add a user to this ticket channel")
    @app_commands.describe(user="User to add")
    async def ticket_add(self, interaction: discord.Interaction, user: discord.User):
        channel = interaction.channel
        if not interaction.guild or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Run this in a ticket channel.", ephemeral=True)

        info = await self.store.get_ticket(channel.id)
        if not info:
            return await interaction.response.send_message("This is not a managed ticket channel.", ephemeral=True)

        # Only opener, staff, or admins can add
        cfg = await self.store.get_guild_config(interaction.guild_id)
        staff_role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        is_staff = False
        if isinstance(interaction.user, discord.Member):
            is_staff = (interaction.user.guild_permissions.manage_channels) or (staff_role and staff_role in interaction.user.roles)

        if interaction.user.id != info["opener_id"] and not is_staff:
            return await interaction.response.send_message("Only the ticket opener or staff can add users.", ephemeral=True)

        try:
            await channel.set_permissions(user, view_channel=True, send_messages=True)
            await interaction.response.send_message(f"✅ {user.mention} added.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I lack permission to edit channel permissions.", ephemeral=True)

    @ticket.command(name="remove", description="Remove a user from this ticket channel")
    @app_commands.describe(user="User to remove")
    async def ticket_remove(self, interaction: discord.Interaction, user: discord.User):
        channel = interaction.channel
        if not interaction.guild or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Run this in a ticket channel.", ephemeral=True)

        info = await self.store.get_ticket(channel.id)
        if not info:
            return await interaction.response.send_message("This is not a managed ticket channel.", ephemeral=True)

        cfg = await self.store.get_guild_config(interaction.guild_id)
        staff_role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        is_staff = False
        if isinstance(interaction.user, discord.Member):
            is_staff = (interaction.user.guild_permissions.manage_channels) or (staff_role and staff_role in interaction.user.roles)

        if interaction.user.id != info["opener_id"] and not is_staff:
            return await interaction.response.send_message("Only the ticket opener or staff can remove users.", ephemeral=True)

        try:
            await channel.set_permissions(user, overwrite=None)
            await interaction.response.send_message(f"✅ {user.mention} removed.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I lack permission to edit channel permissions.", ephemeral=True)

    @ticket.command(name="rename", description="Rename this ticket channel")
    @app_commands.describe(name="New channel name (letters, numbers, and dashes only)")
    async def ticket_rename(self, interaction: discord.Interaction, name: str):
        channel = interaction.channel
        if not interaction.guild or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Run this in a ticket channel.", ephemeral=True)

        info = await self.store.get_ticket(channel.id)
        if not info:
            return await interaction.response.send_message("This is not a managed ticket channel.", ephemeral=True)

        cfg = await self.store.get_guild_config(interaction.guild_id)
        staff_role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        is_staff = False
        if isinstance(interaction.user, discord.Member):
            is_staff = (interaction.user.guild_permissions.manage_channels) or (staff_role and staff_role in interaction.user.roles)

        if interaction.user.id != info["opener_id"] and not is_staff:
            return await interaction.response.send_message("Only the ticket opener or staff can rename.", ephemeral=True)

        safe = name.lower().replace(" ", "-")
        safe = "".join(ch for ch in safe if ch.isalnum() or ch == "-")
        if not safe:
            return await interaction.response.send_message("Please provide a valid name.", ephemeral=True)

        await channel.edit(name=safe, reason=f"Ticket rename by {interaction.user}")
        await interaction.response.send_message(f"✅ Renamed to **{safe}**.", ephemeral=True)

    @ticket.command(name="claim", description="Claim this ticket (marks you as the handler)")
    async def ticket_claim(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not interaction.guild or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Run this in a ticket channel.", ephemeral=True)
        info = await self.store.get_ticket(channel.id)
        if not info:
            return await interaction.response.send_message("This is not a managed ticket channel.", ephemeral=True)

        cfg = await self.store.get_guild_config(interaction.guild_id)
        staff_role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_channels or (staff_role and staff_role in interaction.user.roles)):
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)

        info["claimed_by"] = interaction.user.id
        await self.store.remember_ticket(channel.id, info)
        await channel.send(f"🧰 Ticket claimed by {interaction.user.mention}")
        await interaction.response.send_message("✅ Claimed.", ephemeral=True)

    @ticket.command(name="unclaim", description="Unclaim this ticket")
    async def ticket_unclaim(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not interaction.guild or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Run this in a ticket channel.", ephemeral=True)
        info = await self.store.get_ticket(channel.id)
        if not info:
            return await interaction.response.send_message("This is not a managed ticket channel.", ephemeral=True)

        cfg = await self.store.get_guild_config(interaction.guild_id)
        staff_role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_channels or (staff_role and staff_role in interaction.user.roles)):
            return await interaction.response.send_message("Only staff can unclaim tickets.", ephemeral=True)

        info["claimed_by"] = None
        await self.store.remember_ticket(channel.id, info)
        await channel.send("🧰 Ticket is now unclaimed.")
        await interaction.response.send_message("✅ Unclaimed.", ephemeral=True)

    @ticket.command(name="transcript", description="Save and upload a text transcript of this ticket")
    async def ticket_transcript(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not interaction.guild or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Run this in a ticket channel.", ephemeral=True)

        info = await self.store.get_ticket(channel.id)
        if not info:
            return await interaction.response.send_message("This is not a managed ticket channel.", ephemeral=True)

        # Only opener or staff
        cfg = await self.store.get_guild_config(interaction.guild_id)
        staff_role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        is_staff = False
        if isinstance(interaction.user, discord.Member):
            is_staff = (interaction.user.guild_permissions.manage_channels) or (staff_role and staff_role in interaction.user.roles)

        if interaction.user.id != info["opener_id"] and not is_staff:
            return await interaction.response.send_message("Only the ticket opener or staff can export the transcript.", ephemeral=True)

        await interaction.response.defer(thinking=True, ephemeral=True)
        lines = []
        async for msg in channel.history(limit=None, oldest_first=True):
            author = f"{msg.author} ({msg.author.id})"
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            content = msg.content.replace("\n", "\\n")
            lines.append(f"[{timestamp}] {author}: {content}")
            for a in msg.attachments:
                lines.append(f"[{timestamp}]    (attachment) {a.url}")
        blob = "\n".join(lines) if lines else "No messages."

        f = discord.File(fp=discord.utils.MISSING, filename=f"{channel.name}_transcript.txt")
        # use in-memory bytes
        import io
        f.fp = io.BytesIO(blob.encode("utf-8"))  # type: ignore

        await interaction.followup.send(content="📄 Transcript exported:", file=f, ephemeral=True)

    @ticket.command(name="close", description="Close this ticket channel")
    @app_commands.describe(reason="Optional reason for closing")
    async def ticket_close(self, interaction: discord.Interaction, reason: Optional[str] = None):
        channel = interaction.channel
        if not interaction.guild or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Run this in a ticket channel.", ephemeral=True)

        info = await self.store.get_ticket(channel.id)
        if not info:
            return await interaction.response.send_message("This is not a managed ticket channel.", ephemeral=True)

        # Only staff OR opener can close
        cfg = await self.store.get_guild_config(interaction.guild_id)
        staff_role = interaction.guild.get_role(cfg.staff_role_id) if cfg.staff_role_id else None
        is_staff = False
        if isinstance(interaction.user, discord.Member):
            is_staff = (interaction.user.guild_permissions.manage_channels) or (staff_role and staff_role in interaction.user.roles)

        if interaction.user.id != info["opener_id"] and not is_staff:
            return await interaction.response.send_message("Only the ticket opener or staff can close.", ephemeral=True)

        # Try to post a summary, then delete
        summary = discord.Embed(
            title="Ticket Closed",
            description=f"Closed by {interaction.user.mention}" + (f"\n**Reason:** {reason}" if reason else ""),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        summary.set_footer(text=f"Opened at {info.get('created_at', 'unknown')} • #{info.get('number','?'):0>4}")
        try:
            await channel.send(embed=summary)
        except Exception:
            pass

        await self.store.forget_ticket(channel.id)
        try:
            await interaction.response.send_message("🧹 Closing…", ephemeral=True)
        except discord.InteractionResponded:
            pass
        await asyncio.sleep(1.0)
        try:
            await channel.delete(reason=reason or f"Ticket closed by {interaction.user}")
        except discord.Forbidden:
            # Fallback: lock the channel instead of deleting
            await channel.set_permissions(interaction.guild.default_role, view_channel=False, send_messages=False)
            await channel.send("I couldn't delete this channel due to missing permissions, so I locked it instead.")

    # Optional admin helper to re-post a panel
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.command(name="tickets_panel", description="Post another Create Ticket panel here")
    async def tickets_panel(self, interaction: discord.Interaction):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not cfg.staff_role_id or not cfg.category_id:
            return await interaction.response.send_message("Run `/tickets_setup` first.", ephemeral=True)

        view = CreateTicketView(self.bot, self.store)
        embed = discord.Embed(
            title="Need Help?",
            description="Press the button below to open a private ticket with our staff.",
            color=discord.Color.green()
        )
        msg = await interaction.channel.send(embed=embed, view=view)
        cfg.panel_channel_id = interaction.channel.id
        cfg.panel_message_id = msg.id
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
