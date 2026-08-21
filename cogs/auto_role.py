# cogs/auto_role.py
# Auto-assigns roles to members when they join the server.
#
# Admins configure one set of roles for human members and (optionally) a
# separate set for bots via /autorole. An optional delay can be set to wait
# a few seconds after join before assigning (handy if another bot/gate,
# e.g. a verification flow, needs to run first). Role assignment is
# best-effort: roles that no longer exist or that the bot can't assign
# (missing Manage Roles, or above the bot's top role) are skipped rather
# than raising, and the rest still get applied.
#
# JSON persistence per-guild (data/auto_role.json) — no DB required.
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "auto_role.json")

MAX_ROLES_PER_LIST = 10
MAX_DELAY_SECONDS = 300  # 5 minutes


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


@dataclass
class GuildConfig:
    guild_id: int
    enabled: bool = True
    human_role_ids: List[int] = field(default_factory=list)
    bot_role_ids: List[int] = field(default_factory=list)
    delay_seconds: int = 0


class AutoRoleStore:
    """Simple JSON persistence for per-guild auto-role config."""

    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"guilds": {}}
        _ensure_data_dir()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {"guilds": {}}
        else:
            self._save()

    def _save(self):
        tmp = json.dumps(self.data, indent=2, ensure_ascii=False)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(tmp)

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        async with self.lock:
            g = self.data["guilds"].get(str(guild_id))
            if not g:
                cfg = GuildConfig(guild_id=guild_id)
                self.data["guilds"][str(guild_id)] = asdict(cfg)
                self._save()
                return cfg
            # Tolerate old configs missing newer fields.
            defaults = asdict(GuildConfig(guild_id=guild_id))
            defaults.update({k: v for k, v in g.items() if k in defaults})
            return GuildConfig(**defaults)

    async def set_guild_config(self, cfg: GuildConfig):
        async with self.lock:
            self.data["guilds"][str(cfg.guild_id)] = asdict(cfg)
            self._save()


class AutoRole(commands.Cog):
    """Assigns configured roles to members automatically when they join."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_data_dir()
        self.store = AutoRoleStore(DATA_FILE)

    # ---------- helpers ----------
    def _assignable_roles(self, guild: discord.Guild, role_ids: List[int]) -> tuple[List[discord.Role], List[str]]:
        """Splits configured role ids into (roles the bot can actually assign,
        human-readable reasons for the ones it can't)."""
        me = guild.me
        ok: List[discord.Role] = []
        problems: List[str] = []
        for rid in role_ids:
            role = guild.get_role(rid)
            if role is None:
                problems.append(f"<@&{rid}> no longer exists")
                continue
            if role.is_default():
                continue
            if role.managed:
                problems.append(f"{role.name} is managed by an integration")
                continue
            if me and role >= me.top_role:
                problems.append(f"{role.name} is above my highest role")
                continue
            ok.append(role)
        return ok, problems

    async def _assign_roles(self, member: discord.Member, cfg: GuildConfig):
        role_ids = cfg.bot_role_ids if member.bot else cfg.human_role_ids
        if not role_ids:
            return
        if cfg.delay_seconds > 0:
            await asyncio.sleep(cfg.delay_seconds)
            # Member may have left during the delay, or roles may have
            # already been applied by something else — re-fetch to be safe.
            guild = member.guild
            member = guild.get_member(member.id)
            if member is None:
                return

        guild = member.guild
        if not guild.me.guild_permissions.manage_roles:
            log.warning("Auto-role: missing Manage Roles permission in guild %s", guild.id)
            return

        roles, problems = self._assignable_roles(guild, role_ids)
        if problems:
            log.warning("Auto-role: skipping unassignable roles in guild %s: %s", guild.id, "; ".join(problems))
        to_add = [r for r in roles if r not in member.roles]
        if not to_add:
            return
        try:
            await member.add_roles(*to_add, reason="Auto-role on join")
        except discord.Forbidden:
            log.warning("Auto-role: forbidden adding roles to %s in guild %s", member.id, guild.id)
        except discord.HTTPException as e:
            log.warning("Auto-role: failed adding roles to %s in guild %s: %s", member.id, guild.id, e)

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild is None:
            return
        cfg = await self.store.get_guild_config(member.guild.id)
        if not cfg.enabled:
            return
        if not cfg.human_role_ids and not cfg.bot_role_ids:
            return
        asyncio.create_task(self._assign_roles(member, cfg))

    # ---------- admin commands ----------
    autorole = app_commands.Group(name="autorole", description="Configure roles auto-assigned to new members.", guild_only=True)

    @autorole.command(name="add", description="(Admin) Add a role to auto-assign when members join.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(role="Role to assign automatically", bots="Assign this to bots instead of human members")
    async def autorole_add(self, interaction: discord.Interaction, role: discord.Role, bots: bool = False):
        if role.is_default():
            return await interaction.response.send_message("⚠️ Can't auto-assign @everyone.", ephemeral=True)
        if role.managed:
            return await interaction.response.send_message("⚠️ That role is managed by an integration and can't be assigned manually.", ephemeral=True)
        if isinstance(interaction.user, discord.Member) and role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("⚠️ You can't configure a role at or above your own highest role.", ephemeral=True)

        cfg = await self.store.get_guild_config(interaction.guild_id)
        target = cfg.bot_role_ids if bots else cfg.human_role_ids
        if role.id in target:
            return await interaction.response.send_message(f"{role.mention} is already on the {'bot' if bots else 'member'} list.", ephemeral=True)
        if len(target) >= MAX_ROLES_PER_LIST:
            return await interaction.response.send_message(f"⚠️ Limit of {MAX_ROLES_PER_LIST} roles per list reached.", ephemeral=True)
        target.append(role.id)
        await self.store.set_guild_config(cfg)

        warn = ""
        if interaction.guild.me and role >= interaction.guild.me.top_role:
            warn = "\n⚠️ Heads up: that role is above my highest role, so I can't assign it until my role is moved above it."
        await interaction.response.send_message(
            f"✅ {role.mention} will now be auto-assigned to new {'bots' if bots else 'members'}.{warn}", ephemeral=True
        )

    @autorole.command(name="remove", description="(Admin) Stop auto-assigning a role.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(role="Role to stop auto-assigning")
    async def autorole_remove(self, interaction: discord.Interaction, role: discord.Role):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        found = False
        if role.id in cfg.human_role_ids:
            cfg.human_role_ids.remove(role.id)
            found = True
        if role.id in cfg.bot_role_ids:
            cfg.bot_role_ids.remove(role.id)
            found = True
        if not found:
            return await interaction.response.send_message(f"{role.mention} isn't configured for auto-assignment.", ephemeral=True)
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"✅ {role.mention} removed from auto-assignment.", ephemeral=True)

    @autorole.command(name="list", description="Show the roles auto-assigned to new members.")
    async def autorole_list(self, interaction: discord.Interaction):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not cfg.human_role_ids and not cfg.bot_role_ids:
            return await interaction.response.send_message("No auto-roles configured. Use `/autorole add`.", ephemeral=True)

        def fmt(ids: List[int]) -> str:
            if not ids:
                return "*(none)*"
            return ", ".join(f"<@&{rid}>" for rid in ids)

        embed = discord.Embed(
            title="Auto-Role Configuration",
            color=discord.Color.blurple() if cfg.enabled else discord.Color.greyple(),
        )
        embed.add_field(name="Status", value="🟢 Enabled" if cfg.enabled else "🔴 Disabled", inline=False)
        embed.add_field(name="Members", value=fmt(cfg.human_role_ids), inline=False)
        embed.add_field(name="Bots", value=fmt(cfg.bot_role_ids), inline=False)
        embed.add_field(name="Delay", value=f"{cfg.delay_seconds}s" if cfg.delay_seconds else "None", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autorole.command(name="toggle", description="(Admin) Enable or disable auto-role assignment.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(enabled="Whether auto-role should be active")
    async def autorole_toggle(self, interaction: discord.Interaction, enabled: bool):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.enabled = enabled
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"✅ Auto-role {'enabled' if enabled else 'disabled'}.", ephemeral=True)

    @autorole.command(name="delay", description="(Admin) Set a delay before roles are assigned after joining.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(seconds="Seconds to wait after join before assigning roles (0 = immediate)")
    async def autorole_delay(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, MAX_DELAY_SECONDS]):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.delay_seconds = int(seconds)
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"✅ Auto-role delay set to **{seconds}s**.", ephemeral=True)

    @autorole.command(name="clear", description="(Admin) Remove all configured auto-roles.")
    @app_commands.default_permissions(manage_roles=True)
    async def autorole_clear(self, interaction: discord.Interaction):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.human_role_ids = []
        cfg.bot_role_ids = []
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message("✅ Cleared all auto-roles.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRole(bot))
