# cogs/admin_gates.py
from __future__ import annotations
import asyncio
import contextlib
import dataclasses
import enum
import time
from typing import List, Optional, Tuple, Sequence

import discord
from discord import app_commands
from discord.ext import commands

# ---- Lightweight async storage (SQLite) --------------------------------------
# Requires: aiosqlite (pip install aiosqlite)
import aiosqlite


# =============== Data Model ====================================================

class Scope(str, enum.Enum):
    role = "role"
    user = "user"
    channel = "channel"
    admin_only = "admin_only"
    everyone = "everyone"


class Effect(str, enum.Enum):
    allow = "allow"
    deny = "deny"


@dataclasses.dataclass
class GateRule:
    id: int
    guild_id: int
    command: str
    scope: Scope
    target_id: Optional[int]  # null for admin_only / everyone
    effect: Effect
    priority: int
    created_by: int
    created_at: float


DEFAULT_MODE = "allow-by-default"  # or "deny-by-default"


# =============== Store =========================================================

class GateStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._owner_ids: set[int] = set()
        self._lock = asyncio.Lock()

    # ---- owners ----------------------------------------------------------------
    def set_owner_ids(self, owner_ids: Sequence[int]):
        self._owner_ids = set(owner_ids)

    def get_owner_ids(self) -> set[int]:
        return self._owner_ids

    # ---- DB init ----------------------------------------------------------------
    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS gate_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                scope TEXT NOT NULL,
                target_id INTEGER,
                effect TEXT NOT NULL,
                priority INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS gate_config (
                guild_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (guild_id, key)
            );
            """)
            await db.commit()

    # ---- Config helpers ---------------------------------------------------------
    async def config_get(self, guild_id: int, key: str, default: str) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT value FROM gate_config WHERE guild_id=? AND key=?",
                (guild_id, key),
            )
            row = await cur.fetchone()
            await cur.close()
        return row[0] if row else default

    async def config_set(self, guild_id: int, key: str, value: str) -> None:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO gate_config (guild_id, key, value) VALUES (?,?,?) "
                    "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                    (guild_id, key, value),
                )
                await db.commit()

    async def mode_get(self, guild_id: int) -> str:
        return await self.config_get(guild_id, "mode", DEFAULT_MODE)

    async def mode_set(self, guild_id: int, mode: str) -> None:
        if mode not in ("allow-by-default", "deny-by-default"):
            raise ValueError("mode must be 'allow-by-default' or 'deny-by-default'")
        await self.config_set(guild_id, "mode", mode)

    async def bypass_get(self, guild_id: int) -> bool:
        return (await self.config_get(guild_id, "manage_guild_bypass", "true")).lower() == "true"

    async def bypass_set(self, guild_id: int, enabled: bool) -> None:
        await self.config_set(guild_id, "manage_guild_bypass", "true" if enabled else "false")

    # ---- Rules ------------------------------------------------------------------
    async def add_rule(
        self,
        guild_id: int,
        command: str,
        scope: Scope,
        effect: Effect,
        priority: int,
        created_by: int,
        target_id: Optional[int] = None,
    ) -> int:
        now = time.time()
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute(
                    "INSERT INTO gate_rules (guild_id, command, scope, target_id, effect, priority, created_by, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (guild_id, command, scope.value, target_id, effect.value, priority, created_by, now),
                )
                await db.commit()
                return cur.lastrowid

    async def remove_rule(self, guild_id: int, rule_id: int) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("DELETE FROM gate_rules WHERE guild_id=? AND id=?", (guild_id, rule_id))
                await db.commit()
                return cur.rowcount > 0

    async def list_rules(self, guild_id: int, command: Optional[str] = None) -> List[GateRule]:
        async with aiosqlite.connect(self.db_path) as db:
            if command:
                cur = await db.execute(
                    "SELECT id, guild_id, command, scope, target_id, effect, priority, created_by, created_at "
                    "FROM gate_rules WHERE guild_id=? AND command=? ORDER BY priority DESC, id ASC",
                    (guild_id, command),
                )
            else:
                cur = await db.execute(
                    "SELECT id, guild_id, command, scope, target_id, effect, priority, created_by, created_at "
                    "FROM gate_rules WHERE guild_id=? ORDER BY command ASC, priority DESC, id ASC",
                    (guild_id,),
                )
            rows = await cur.fetchall()
            await cur.close()
        return [
            GateRule(
                id=row[0], guild_id=row[1], command=row[2],
                scope=Scope(row[3]), target_id=row[4],
                effect=Effect(row[5]), priority=row[6],
                created_by=row[7], created_at=row[8]
            )
            for row in rows
        ]

    async def load_rules(self, guild_id: int, command: str) -> List[GateRule]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT id, guild_id, command, scope, target_id, effect, priority, created_by, created_at "
                "FROM gate_rules WHERE guild_id=? AND command=? ORDER BY priority DESC, id ASC",
                (guild_id, command),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            GateRule(
                id=row[0], guild_id=row[1], command=row[2],
                scope=Scope(row[3]), target_id=row[4],
                effect=Effect(row[5]), priority=row[6],
                created_by=row[7], created_at=row[8]
            )
            for row in rows
        ]


# =============== Rule Evaluation ==============================================

def _matches(rule: GateRule, member: discord.Member, channel: discord.abc.GuildChannel) -> bool:
    if rule.scope == Scope.everyone or rule.scope == Scope.admin_only:
        return True
    if rule.scope == Scope.user:
        return rule.target_id == member.id
    if rule.scope == Scope.role:
        if not hasattr(member, "roles"):
            return False
        return any(r.id == rule.target_id for r in getattr(member, "roles", []))
    if rule.scope == Scope.channel:
        return rule.target_id == channel.id
    return False

def evaluate(rules: List[GateRule], member: discord.Member, channel: discord.abc.GuildChannel, default_allow: bool
             ) -> Tuple[bool, Optional[str]]:
    """
    Apply highest-priority matching rule. If none match, fall back to default_allow.
    Returns (allowed, reason_if_denied).
    """
    for r in rules:  # already sorted by priority DESC
        if _matches(r, member, channel):
            if r.scope == Scope.admin_only and not (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
                return False, "Admin-only: you need Administrator or Manage Server."
            if r.effect == Effect.deny:
                return False, f"Denied by rule #{r.id} ({r.scope.value})."
            return True, None
    if default_allow:
        return True, None
    return False, "Command is denied by default policy."


# =============== Global Decorator =============================================

# This module-level store is set by the Cog on setup().
_STORE: Optional[GateStore] = None
_BOT_REF: Optional[commands.Bot] = None

def _qualified_name_from_inter(inter: discord.Interaction) -> str:
    """
    Compute a stable 'qualified name' like 'group subcommand' or 'command'.
    """
    cmd = inter.command
    if isinstance(cmd, app_commands.Command):
        return cmd.qualified_name
    return inter.command.name if inter.command else "unknown"

async def _is_allowed(inter: discord.Interaction) -> bool:
    global _STORE, _BOT_REF
    if _STORE is None:
        return True  # if not initialized, don't block

    # DMs: allow by default (you can change this)
    if inter.guild_id is None:
        return True

    # Bypass: bot owners/team
    if inter.user.id in _STORE.get_owner_ids():
        return True

    # Optional Manage Guild bypass
    if isinstance(inter.user, discord.Member):
        if inter.user.guild_permissions.manage_guild and (await _STORE.bypass_get(inter.guild_id)):
            return True

    command_qn = _qualified_name_from_inter(inter)
    rules = await _STORE.load_rules(inter.guild_id, command_qn)
    mode = await _STORE.mode_get(inter.guild_id)
    default_allow = (mode == "allow-by-default")

    allowed, reason = evaluate(rules, inter.user, inter.channel, default_allow)
    if not allowed:
        # Try to send a friendly ephemeral message if possible
        with contextlib.suppress(discord.HTTPException, discord.InteractionResponded):
            await inter.response.send_message(reason or "You’re not allowed to use this command here.", ephemeral=True)
    return allowed

def gated():
    """
    Decorator for slash commands: @gated()
    """
    return app_commands.check(lambda inter: _is_allowed(inter))


# =============== Autocomplete Helpers =========================================

def _collect_command_names(tree: app_commands.CommandTree) -> List[str]:
    """
    Flattens all registered app commands into qualified names (e.g., "admin gate", "pin add").
    """
    names: List[str] = []

    def walk(cmds: Sequence[app_commands.AppCommand], prefix: str = ""):
        for c in cmds:
            if isinstance(c, app_commands.Command):
                names.append(c.qualified_name)
            elif isinstance(c, app_commands.Group):
                walk(c.commands, prefix=c.name)

    walk(tree.get_commands())
    # Unique + sorted for UX
    uniq = sorted(set(names), key=str.lower)
    return uniq

async def _ac_commands(inter: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    # Suggest command names that start with or contain the typed fragment
    tree = inter.client.tree
    names = _collect_command_names(tree)
    current_l = (current or "").lower()
    filtered = [n for n in names if current_l in n.lower()]
    return [app_commands.Choice(name=n[:100], value=n) for n in filtered[:25]]


# =============== The Cog =======================================================

class AdminGates(commands.Cog):
    """Admin gating: centrally control who can run which commands, where."""

    def __init__(self, bot: commands.Bot, db_path: str = "data/admin_gates.sqlite3"):
        self.bot = bot
        self.store = GateStore(db_path)

    async def cog_load(self):
        # Prepare DB
        await self.store.init()
        # Owners: application owner (+ team members if applicable)
        try:
            appinfo = await self.bot.application_info()
            owners = {appinfo.owner.id} if appinfo.owner else set()
            if appinfo.team:
                owners |= {m.id for m in appinfo.team.members}
            self.store.set_owner_ids(list(owners))
        except Exception:
            pass

        # Expose store globally for the decorator
        global _STORE, _BOT_REF
        _STORE = self.store
        _BOT_REF = self.bot

    # ---------- Command Group ----------
    admin = app_commands.Group(name="admin", description="Admin utilities")
    gate = app_commands.Group(name="gate", description="Configure command gating", parent=admin)

   # ---------- /admin gate help ----------
    @gate.command(name="help", description="Show a quick cheat sheet for Admin Gates.")
    @app_commands.default_permissions(manage_guild=True)
    async def gate_help(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        mode = await self.store.mode_get(inter.guild_id)
        bypass = await self.store.bypass_get(inter.guild_id)

        names = _collect_command_names(self.bot.tree)
        sample = ", ".join(names[:5]) if names else "pin add, market list, river stage"

        emb = discord.Embed(
            title="Admin Gate — Cheat Sheet",
            description=(
                "Centralized access control for your slash commands.\n"
                f"**Default policy:** `{mode}` • **Manage Server bypass:** `{'on' if bypass else 'off'}`"
            ),
        )
        emb.add_field(
            name="Quick Start",
            value=(
                "• Make a command admin-only:\n"
                "`/admin gate add command:\"market list\" scope:admin_only effect:allow priority:100`\n"
                "• Also allow Mods:\n"
                "`/admin gate add command:\"market list\" scope:role target_role:@Moderator effect:allow priority:90`\n"
                "• Block in #general:\n"
                "`/admin gate add command:\"market list\" scope:channel target_channel:#general effect:deny priority:95`"
            ),
            inline=False,
        )
        emb.add_field(
            name="Scopes",
            value=(
                "`role` (needs target_role) • `user` (target_user) • `channel` (target_channel)\n"
                "`admin_only` (Admin/Manage Server) • `everyone` (all members)"
            ),
            inline=False,
        )
        emb.add_field(
            name="Effects & Priority",
            value="`allow` or `deny`. Higher `priority` wins. If no rule matches, falls back to `/admin gate mode`.",
            inline=False,
        )
        emb.add_field(
            name="Defaults & Bypass",
            value="`/admin gate mode` sets default allow/deny • `/admin gate bypass` toggles Manage Server bypass",
            inline=False,
        )
        emb.add_field(
            name="Testing",
            value="Use `/admin gate test command:<cmd> user:@User channel:#room` to dry-run a check.",
            inline=False,
        )
        emb.add_field(
            name="Finding Command Names",
            value=f"Autocomplete helps. Examples: `{sample}`",
            inline=False,
        )
        emb.set_footer(text="Tip: Put @gated() on a Group to gate all its subcommands at once.")

        await inter.followup.send(embed=emb, ephemeral=True)
   
    # ---------- /admin gate add ----------
    @gate.command(name="add", description="Add a gating rule (priority: higher wins).")
    @app_commands.describe(
        command="Command qualified name (e.g., 'pin add', 'market list').",
        scope="What to gate by",
        effect="Allow or deny",
        priority="Rule priority (default 50). Higher takes precedence.",
        target_role="Role to target (when scope=role).",
        target_user="User to target (when scope=user).",
        target_channel="Channel to target (when scope=channel).",
    )
    @app_commands.autocomplete(command=_ac_commands)
    @app_commands.choices(
        scope=[app_commands.Choice(name=s.value, value=s.value) for s in Scope],
        effect=[app_commands.Choice(name="allow", value="allow"),
                app_commands.Choice(name="deny", value="deny")],
    )
    @app_commands.default_permissions(manage_guild=True)
    async def gate_add(
        self,
        inter: discord.Interaction,
        command: str,
        scope: app_commands.Choice[str],
        effect: app_commands.Choice[str],
        priority: Optional[int] = 50,
        target_role: Optional[discord.Role] = None,
        target_user: Optional[discord.Member] = None,
        target_channel: Optional[discord.abc.GuildChannel] = None,
    ):
        await inter.response.defer(ephemeral=True, thinking=True)

        # Validate target based on scope
        s = Scope(scope.value)
        target_id: Optional[int] = None
        if s == Scope.role:
            if not target_role:
                return await inter.followup.send("Scope **role** requires `target_role`.", ephemeral=True)
            target_id = target_role.id
        elif s == Scope.user:
            if not target_user:
                return await inter.followup.send("Scope **user** requires `target_user`.", ephemeral=True)
            target_id = target_user.id
        elif s == Scope.channel:
            if not target_channel:
                return await inter.followup.send("Scope **channel** requires `target_channel`.", ephemeral=True)
            target_id = target_channel.id
        elif s in (Scope.admin_only, Scope.everyone):
            target_id = None

        rid = await self.store.add_rule(
            guild_id=inter.guild_id,
            command=command,
            scope=s,
            target_id=target_id,
            effect=Effect(effect.value),
            priority=priority or 50,
            created_by=inter.user.id,
        )

        await inter.followup.send(
            f"✅ Added rule **#{rid}**: `{command}` | scope=`{s.value}` | "
            f"{'target='+str(target_id) if target_id else 'no-target'} | effect=`{effect.value}` | priority=`{priority}`",
            ephemeral=True,
        )

    # ---------- /admin gate list ----------
    @gate.command(name="list", description="List rules (optionally filter by command).")
    @app_commands.describe(command="Command qualified name to filter (optional).")
    @app_commands.autocomplete(command=_ac_commands)
    @app_commands.default_permissions(manage_guild=True)
    async def gate_list(self, inter: discord.Interaction, command: Optional[str] = None):
        await inter.response.defer(ephemeral=True, thinking=True)
        rules = await self.store.list_rules(inter.guild_id, command)
        if not rules:
            return await inter.followup.send("No rules found.", ephemeral=True)

        lines = []
        for r in rules:
            tgt = "—"
            if r.scope == Scope.role or r.scope == Scope.user or r.scope == Scope.channel:
                tgt = str(r.target_id)
            lines.append(
                f"#{r.id:>3} | {r.command:<24} | {r.scope.value:<11} | {tgt:<18} | {r.effect.value:<5} | p={r.priority}"
            )
        block = "```\n" + "\n".join(lines[:199]) + "\n```"
        await inter.followup.send(block, ephemeral=True)

    # ---------- /admin gate remove ----------
    @gate.command(name="remove", description="Remove a rule by ID.")
    @app_commands.describe(rule_id="The rule id from /admin gate list")
    @app_commands.default_permissions(manage_guild=True)
    async def gate_remove(self, inter: discord.Interaction, rule_id: int):
        ok = await self.store.remove_rule(inter.guild_id, rule_id)
        if ok:
            await inter.response.send_message(f"🗑️ Removed rule #{rule_id}.", ephemeral=True)
        else:
            await inter.response.send_message(f"Rule #{rule_id} not found.", ephemeral=True)

    # ---------- /admin gate mode ----------
    @gate.command(name="mode", description="Set default policy if no rule matches.")
    @app_commands.describe(mode="allow-by-default or deny-by-default")
    @app_commands.choices(mode=[
        app_commands.Choice(name="allow-by-default", value="allow-by-default"),
        app_commands.Choice(name="deny-by-default", value="deny-by-default"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def gate_mode(self, inter: discord.Interaction, mode: app_commands.Choice[str]):
        await self.store.mode_set(inter.guild_id, mode.value)
        await inter.response.send_message(f"🔧 Default policy set to **{mode.value}**.", ephemeral=True)

    # ---------- /admin gate bypass ----------
    @gate.command(name="bypass", description="Toggle Manage Server bypass.")
    @app_commands.describe(enabled="If on, users with Manage Server bypass gates.")
    @app_commands.choices(enabled=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def gate_bypass(self, inter: discord.Interaction, enabled: app_commands.Choice[str]):
        val = enabled.value == "on"
        await self.store.bypass_set(inter.guild_id, val)
        await inter.response.send_message(
            f"🪄 Manage Server bypass **{'enabled' if val else 'disabled'}**.",
            ephemeral=True,
        )

    # ---------- /admin gate test ----------
    @gate.command(name="test", description="Dry-run a command for a user in a channel.")
    @app_commands.describe(
        command="Command qualified name to test.",
        user="User to test as.",
        channel="Channel to test in.",
    )
    @app_commands.autocomplete(command=_ac_commands)
    @app_commands.default_permissions(manage_guild=True)
    async def gate_test(
        self,
        inter: discord.Interaction,
        command: str,
        user: discord.Member,
        channel: discord.abc.GuildChannel,
    ):
        await inter.response.defer(ephemeral=True, thinking=True)
        rules = await self.store.load_rules(inter.guild_id, command)
        mode = await self.store.mode_get(inter.guild_id)
        default_allow = (mode == "allow-by-default")
        allowed, reason = evaluate(rules, user, channel, default_allow)
        if allowed:
            await inter.followup.send(f"✅ **ALLOWED** for {user.mention} in {channel.mention}", ephemeral=True)
        else:
            await inter.followup.send(f"❌ **DENIED**: {reason}", ephemeral=True)


async def setup(bot: commands.Bot):
    """
    Standard async setup entrypoint for discord.py cogs extension.
    Adjust db path if you have a central data folder.
    """
    # Ensure a data folder present if you use one; otherwise default path is fine
    cog = AdminGates(bot, db_path="data/admin_gates.sqlite3")
    await bot.add_cog(cog)
