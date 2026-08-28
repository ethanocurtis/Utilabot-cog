# cogs/pterodactyl.py
# Admin control panel for servers hosted on a Pterodactyl panel, via its
# Client API (a per-account API key, not the Application/admin API).
#
# - /ptero setup / addserver / removeserver / servers  — configure the panel
#   URL, API key, and short nicknames for the servers you want to manage.
# - /ptero panel   — posts a persistent embed with Start/Restart/Stop/Refresh
#   buttons and live CPU/memory/disk/uptime, so admins don't need to remember
#   slash commands day-to-day.
# - /ptero status / backups — one-off ephemeral lookups.
# - /ptero alertchannel / monitor — where and how often a background loop
#   polls each server, alerting on:
#     * a start/restart/stop that didn't reach the expected state in time
#     * an unexpected offline (server dropped from "running" with no
#       power command pending — i.e. it crashed)
#     * rapid running/offline flapping (crash-looping)
#     * a backup that completed with is_successful=false
#     * a backup stuck "in progress" far longer than normal
#
# Pterodactyl has no first-class "reboot failed" event, so all of this is
# inferred by polling GET .../resources and GET .../backups — the same
# approach the Minecraft cog uses for its own status polling.
#
# Every command here is admin-gated (Administrator permission), and the
# panel's buttons re-check that at click time since default_permissions is
# only a Discord-side hint, not something discord.py enforces on components.
#
# JSON persistence per-guild (data/pterodactyl.json) — no DB required.
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "pterodactyl.json")

DEFAULT_POLL_SECONDS = 60
DEFAULT_RESTART_TIMEOUT = 180       # seconds a start/restart/stop may take before we complain
DEFAULT_BACKUP_STUCK_MINUTES = 120  # a backup still "in progress" past this looks stuck
CRASH_LOOP_THRESHOLD = 3            # this many state flips...
CRASH_LOOP_WINDOW_SECONDS = 600     # ...within this window counts as crash-looping
MAX_KNOWN_BACKUPS = 50              # cap per-server dedupe lists so the JSON file doesn't grow forever


# =========================
# Secret encryption (API keys at rest) — same approach as cogs/minecraft.py
# =========================
def _load_fernet():
    key = os.environ.get("PTERO_SECRET_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        log.warning("PTERO_SECRET_KEY is set but invalid; Pterodactyl API keys will be stored unencrypted.")
        return None


_FERNET = _load_fernet()
if _FERNET is None:
    log.warning(
        "PTERO_SECRET_KEY not set (or invalid) — Pterodactyl API keys will be stored in plaintext in %s. "
        "Set PTERO_SECRET_KEY (python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\") to enable encryption at rest.",
        DATA_FILE,
    )


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value or _FERNET is None:
        return value
    return _FERNET.encrypt(value.encode()).decode()


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value or _FERNET is None:
        return value
    try:
        return _FERNET.decrypt(value.encode()).decode()
    except Exception:
        return value  # legacy plaintext, or no key configured


# =========================
# Pterodactyl Client API wrapper
# =========================
class PteroAPIError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _extract_error(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return first.get("detail") or first.get("code")
    return None


class PteroClient:
    """Thin async wrapper around the Pterodactyl Client API.

    Uses a fresh aiohttp session per call — call volume here is low (a
    handful of servers polled once a minute plus the occasional command), so
    the simplicity outweighs the cost of a persistent connection.
    """

    def __init__(self, panel_url: str, api_key: str):
        self.base = panel_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/vnd.pterodactyl.v1+json",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=self._headers(), **kwargs) as resp:
                    text = await resp.text()
                    data: Any = {}
                    if text:
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            data = {}
                    if resp.status >= 400:
                        raise PteroAPIError(_extract_error(data) or f"HTTP {resp.status}", status=resp.status)
                    return data if isinstance(data, dict) else {}
        except asyncio.TimeoutError:
            raise PteroAPIError("Timed out reaching the panel.")
        except aiohttp.ClientError as e:
            raise PteroAPIError(f"Connection error: {e}")

    async def list_servers(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/api/client")
        return [item.get("attributes", {}) for item in data.get("data", [])]

    async def get_server(self, identifier: str) -> Dict[str, Any]:
        data = await self._request("GET", f"/api/client/servers/{identifier}")
        return data.get("attributes", {})

    async def get_resources(self, identifier: str) -> Dict[str, Any]:
        data = await self._request("GET", f"/api/client/servers/{identifier}/resources")
        return data.get("attributes", {})

    async def send_power(self, identifier: str, signal: str) -> None:
        await self._request("POST", f"/api/client/servers/{identifier}/power", json={"signal": signal})

    async def list_backups(self, identifier: str) -> List[Dict[str, Any]]:
        data = await self._request("GET", f"/api/client/servers/{identifier}/backups?per_page=10&sort=-created_at")
        return [item.get("attributes", {}) for item in data.get("data", [])]


# =========================
# Formatting helpers
# =========================
def _format_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


STATE_COLOR = {
    "running": discord.Color.green(),
    "starting": discord.Color.gold(),
    "stopping": discord.Color.orange(),
    "offline": discord.Color.red(),
}
STATE_EMOJI = {
    "running": "🟢",
    "starting": "🟡",
    "stopping": "🟠",
    "offline": "🔴",
}


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# =========================
# Data model
# =========================
@dataclass
class ServerConfig:
    identifier: str
    name: str
    panel_channel_id: Optional[int] = None
    panel_message_id: Optional[int] = None
    basic_panel_channel_id: Optional[int] = None
    basic_panel_message_id: Optional[int] = None
    last_state: Optional[str] = None
    state_since: float = 0.0
    pending_signal: Optional[str] = None
    pending_since: float = 0.0
    flip_count: int = 0
    flip_window_start: float = 0.0
    known_backup_ids: List[str] = field(default_factory=list)
    stuck_backup_ids: List[str] = field(default_factory=list)


@dataclass
class GuildConfig:
    guild_id: int
    panel_url: Optional[str] = None
    api_key: Optional[str] = None  # encrypted at rest when PTERO_SECRET_KEY is set
    alert_channel_id: Optional[int] = None
    basic_role_id: Optional[int] = None  # role allowed to use the Start-only basic panel
    monitor_enabled: bool = True
    poll_interval_seconds: int = DEFAULT_POLL_SECONDS
    restart_timeout_seconds: int = DEFAULT_RESTART_TIMEOUT
    backup_stuck_minutes: int = DEFAULT_BACKUP_STUCK_MINUTES
    servers: Dict[str, ServerConfig] = field(default_factory=dict)


class PteroStore:
    """Simple JSON persistence for per-guild Pterodactyl configs."""

    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"guilds": {}}
        os.makedirs(DATA_DIR, exist_ok=True)
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
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _to_guild_config(guild_id: int, raw: Dict[str, Any]) -> GuildConfig:
        defaults = asdict(GuildConfig(guild_id=guild_id))
        defaults.update({k: v for k, v in raw.items() if k in defaults})
        servers_raw = defaults.pop("servers", {}) or {}
        cfg = GuildConfig(**{**defaults, "servers": {}})
        for key, sraw in servers_raw.items():
            sdefaults = asdict(ServerConfig(identifier="", name=""))
            sdefaults.update({k: v for k, v in sraw.items() if k in sdefaults})
            cfg.servers[key] = ServerConfig(**sdefaults)
        return cfg

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        async with self.lock:
            raw = self.data["guilds"].get(str(guild_id))
            if not raw:
                cfg = GuildConfig(guild_id=guild_id)
                self.data["guilds"][str(guild_id)] = asdict(cfg)
                self._save()
                return cfg
            return self._to_guild_config(guild_id, raw)

    async def set_guild_config(self, cfg: GuildConfig):
        async with self.lock:
            self.data["guilds"][str(cfg.guild_id)] = asdict(cfg)
            self._save()

    async def all_guild_configs(self) -> List[GuildConfig]:
        async with self.lock:
            return [self._to_guild_config(int(gid), raw) for gid, raw in self.data["guilds"].items()]


# =========================
# Embeds
# =========================
def _panel_embed(
    sc: ServerConfig,
    resources: Optional[Dict[str, Any]],
    error: Optional[str] = None,
    commands_line: str = "▶️ Start · 🔁 Restart · ⏹️ Stop · 🔄 Refresh — admins only",
) -> discord.Embed:
    state = (resources or {}).get("current_state") if resources else None
    color = STATE_COLOR.get(state, discord.Color.greyple())
    emoji = STATE_EMOJI.get(state, "⚪")

    embed = discord.Embed(
        title=f"🖥️ {sc.name}",
        color=color,
        description=f"{emoji} **{(state or 'unknown').title()}**",
        timestamp=discord.utils.utcnow(),
    )
    if error:
        embed.add_field(name="⚠️ Error", value=error, inline=False)
    elif resources:
        res = resources.get("resources", {}) or {}
        embed.add_field(name="CPU", value=f"{res.get('cpu_absolute', 0.0):.1f}%", inline=True)
        embed.add_field(name="Memory", value=_format_bytes(res.get("memory_bytes", 0)), inline=True)
        embed.add_field(name="Disk", value=_format_bytes(res.get("disk_bytes", 0)), inline=True)
        uptime_ms = res.get("uptime", 0) or 0
        if state == "running" and uptime_ms:
            embed.add_field(name="Uptime", value=_format_duration(uptime_ms / 1000), inline=True)
    if sc.pending_signal:
        embed.add_field(name="Pending", value=f"`{sc.pending_signal}` sent, waiting for confirmation…", inline=False)
    embed.add_field(name="Commands", value=commands_line, inline=False)
    embed.set_footer(text=f"Identifier: {sc.identifier}")
    return embed


def _basic_panel_embed(sc: ServerConfig, resources: Optional[Dict[str, Any]], error: Optional[str] = None) -> discord.Embed:
    """Stripped-down panel for non-admins: status + a single Start button, no
    CPU/memory/disk clutter — just enough to see it's down and bring it back."""
    embed = _panel_embed(
        sc,
        resources,
        error=error,
        commands_line="▶️ Start Server — press if it didn't come back up on its own",
    )
    # Drop the resource-usage fields (CPU/Memory/Disk/Uptime); keep status, any
    # error, "Pending", and the Commands line.
    for name in ("CPU", "Memory", "Disk", "Uptime"):
        for i, f in enumerate(embed.fields):
            if f.name == name:
                embed.remove_field(i)
                break
    return embed


def _status_embed(sc: ServerConfig, resources: Dict[str, Any], backups: List[Dict[str, Any]]) -> discord.Embed:
    embed = _panel_embed(sc, resources)
    embed.remove_field(len(embed.fields) - 1)  # drop the "Commands" hint, not relevant for a one-off lookup
    if backups:
        latest = backups[0]
        if latest.get("completed_at") is None:
            status = "⏳ in progress"
        elif latest.get("is_successful"):
            status = "✅ succeeded"
        else:
            status = "❌ failed"
        embed.add_field(
            name="Latest backup",
            value=f"{latest.get('name') or latest.get('uuid', '')[:8]} — {status}",
            inline=False,
        )
    return embed


def _backups_embed(sc: ServerConfig, backups: List[Dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(title=f"🗄️ Backups — {sc.name}", color=discord.Color.blurple())
    if not backups:
        embed.description = "No backups found."
        return embed
    lines = []
    for b in backups[:10]:
        name = b.get("name") or (b.get("uuid") or "")[:8]
        if b.get("completed_at") is None:
            status = "⏳ in progress"
        elif b.get("is_successful"):
            status = "✅ succeeded"
        else:
            status = "❌ failed"
        size = _format_bytes(b.get("bytes", 0)) if b.get("completed_at") else "—"
        created = b.get("created_at", "")[:19].replace("T", " ")
        lines.append(f"**{name}** — {status} — {size}\n`{created} UTC`")
    embed.description = "\n\n".join(lines)
    return embed


# =========================
# Persistent control-panel view
# =========================
class PteroControlView(discord.ui.View):
    def __init__(self, cog: "PterodactylCog", guild_id: int, server_key: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.server_key = server_key
        self.start_btn.custom_id = f"ptero:start:{guild_id}:{server_key}"
        self.restart_btn.custom_id = f"ptero:restart:{guild_id}:{server_key}"
        self.stop_btn.custom_id = f"ptero:stop:{guild_id}:{server_key}"
        self.refresh_btn.custom_id = f"ptero:refresh:{guild_id}:{server_key}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await interaction.response.send_message("🔒 This panel is admin-only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_power(interaction, self.guild_id, self.server_key, "start")

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔁")
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_power(interaction, self.guild_id, self.server_key, "restart")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_power(interaction, self.guild_id, self.server_key, "stop")

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_refresh(interaction, self.guild_id, self.server_key)


# =========================
# Persistent basic panel view (Start only — for the "not available to fix it
# myself" case; anyone holding the configured basic_role_id can use it)
# =========================
class PteroBasicPanelView(discord.ui.View):
    def __init__(self, cog: "PterodactylCog", guild_id: int, server_key: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.server_key = server_key
        self.start_btn.custom_id = f"ptero:basicstart:{guild_id}:{server_key}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.administrator:
            return True
        cfg = await self.cog.store.get_guild_config(self.guild_id)
        if cfg.basic_role_id and any(r.id == cfg.basic_role_id for r in member.roles):
            return True
        await interaction.response.send_message(
            "🔒 You don't have the role for this. Ask an admin to grant it.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Start Server", style=discord.ButtonStyle.success, emoji="▶️")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_power(interaction, self.guild_id, self.server_key, "start")


# =========================
# The Cog
# =========================
class PterodactylCog(commands.Cog):
    """Admin control panel + failure/backup monitoring for Pterodactyl-hosted servers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = PteroStore(DATA_FILE)

    async def cog_load(self):
        # Re-register persistent panel views bound to their specific messages
        # so the buttons keep working after a restart.
        for cfg in await self.store.all_guild_configs():
            for key, sc in cfg.servers.items():
                if sc.panel_message_id:
                    try:
                        self.bot.add_view(PteroControlView(self, cfg.guild_id, key), message_id=int(sc.panel_message_id))
                    except Exception:
                        log.exception("Failed to re-register panel view for %s/%s", cfg.guild_id, key)
                if sc.basic_panel_message_id:
                    try:
                        self.bot.add_view(
                            PteroBasicPanelView(self, cfg.guild_id, key), message_id=int(sc.basic_panel_message_id)
                        )
                    except Exception:
                        log.exception("Failed to re-register basic panel view for %s/%s", cfg.guild_id, key)
        self._monitor.start()

    def cog_unload(self):
        try:
            self._monitor.cancel()
        except Exception:
            log.exception("Failed to cancel Pterodactyl monitor loop on unload.")

    # ---------- helpers ----------
    def _client_for(self, cfg: GuildConfig) -> Optional[PteroClient]:
        if not cfg.panel_url or not cfg.api_key:
            return None
        return PteroClient(cfg.panel_url, decrypt_secret(cfg.api_key))

    def _resolve_server(self, cfg: GuildConfig, name: Optional[str]) -> Optional[ServerConfig]:
        if not cfg.servers:
            return None
        if name:
            return cfg.servers.get(name.strip().lower())
        if len(cfg.servers) == 1:
            return next(iter(cfg.servers.values()))
        return None

    async def _ac_server(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        cfg = await self.store.get_guild_config(interaction.guild_id)
        current_l = (current or "").lower()
        choices = [
            app_commands.Choice(name=f"{sc.name} ({key})", value=key)
            for key, sc in cfg.servers.items()
            if current_l in key or current_l in sc.name.lower()
        ]
        return choices[:25]

    async def _alert(self, cfg: GuildConfig, text: str):
        if not cfg.alert_channel_id:
            return
        channel = self.bot.get_channel(cfg.alert_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            await channel.send(text)
        except discord.HTTPException:
            log.exception("Failed to send Pterodactyl alert to channel %s", cfg.alert_channel_id)

    async def _edit_message(self, channel_id: Optional[int], message_id: Optional[int], embed: discord.Embed):
        if not channel_id or not message_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    async def _refresh_panel_message(
        self,
        cfg: GuildConfig,
        sc: ServerConfig,
        client: Optional[PteroClient] = None,
        resources: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
        """Redraws a server's posted panel embed(s) — both the admin control
        panel and the basic Start-only panel, when either is posted. Pass
        `resources` (already fetched by a caller, e.g. the monitor loop) to
        skip a duplicate API call."""
        has_admin_panel = sc.panel_channel_id and sc.panel_message_id
        has_basic_panel = sc.basic_panel_channel_id and sc.basic_panel_message_id
        if not has_admin_panel and not has_basic_panel:
            return
        if resources is None and error is None:
            client = client or self._client_for(cfg)
            if not client:
                return
            try:
                resources = await client.get_resources(sc.identifier)
            except PteroAPIError as e:
                error = str(e)
        if has_admin_panel:
            await self._edit_message(sc.panel_channel_id, sc.panel_message_id, _panel_embed(sc, resources, error=error))
        if has_basic_panel:
            await self._edit_message(
                sc.basic_panel_channel_id, sc.basic_panel_message_id, _basic_panel_embed(sc, resources, error=error)
            )

    # ---------- button handlers ----------
    async def handle_power(self, interaction: discord.Interaction, guild_id: int, server_key: str, signal: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await self.store.get_guild_config(guild_id)
        sc = cfg.servers.get(server_key)
        client = self._client_for(cfg)
        if not sc or not client:
            return await interaction.followup.send("This server isn't configured anymore.", ephemeral=True)
        try:
            await client.send_power(sc.identifier, signal)
        except PteroAPIError as e:
            return await interaction.followup.send(f"❌ Failed to send `{signal}`: {e}", ephemeral=True)
        sc.pending_signal = signal
        sc.pending_since = time.time()
        await self.store.set_guild_config(cfg)
        await interaction.followup.send(f"✅ Sent **{signal}** to `{sc.name}`.", ephemeral=True)
        await asyncio.sleep(2)  # give the daemon a moment before refreshing the panel
        await self._refresh_panel_message(cfg, sc, client)

    async def handle_refresh(self, interaction: discord.Interaction, guild_id: int, server_key: str):
        await interaction.response.defer(ephemeral=True)
        cfg = await self.store.get_guild_config(guild_id)
        sc = cfg.servers.get(server_key)
        client = self._client_for(cfg)
        if not sc or not client:
            return await interaction.followup.send("This server isn't configured anymore.", ephemeral=True)
        await self._refresh_panel_message(cfg, sc, client)
        await interaction.followup.send("🔄 Refreshed.", ephemeral=True)

    # ---------- command group ----------
    ptero = app_commands.Group(name="ptero", description="Control & monitor Pterodactyl-hosted servers.")

    @ptero.command(name="setup", description="Connect this server to your Pterodactyl panel.")
    @app_commands.describe(panel_url="e.g. https://panel.example.com", api_key="Client API key (Account → API Credentials)")
    @app_commands.default_permissions(administrator=True)
    async def ptero_setup(self, interaction: discord.Interaction, panel_url: str, api_key: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        panel_url = panel_url.strip().rstrip("/")
        client = PteroClient(panel_url, api_key.strip())
        try:
            servers = await client.list_servers()
        except PteroAPIError as e:
            return await interaction.followup.send(f"❌ Couldn't reach the panel with that key: {e}", ephemeral=True)
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.panel_url = panel_url
        cfg.api_key = encrypt_secret(api_key.strip())
        await self.store.set_guild_config(cfg)
        await interaction.followup.send(
            f"✅ Connected to `{panel_url}`. That key can see **{len(servers)}** server(s). "
            "Now register one with `/ptero addserver`.",
            ephemeral=True,
        )

    @ptero.command(name="addserver", description="Register a server from your panel under a short nickname.")
    @app_commands.describe(
        name="Nickname to use in commands (e.g. survival)",
        identifier="Server identifier from the panel's URL (e.g. the 1a2b3c4d in /server/1a2b3c4d)",
    )
    @app_commands.default_permissions(administrator=True)
    async def ptero_addserver(self, interaction: discord.Interaction, name: str, identifier: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await self.store.get_guild_config(interaction.guild_id)
        client = self._client_for(cfg)
        if not client:
            return await interaction.followup.send("Run `/ptero setup` first.", ephemeral=True)
        identifier = identifier.strip()
        try:
            details = await client.get_server(identifier)
        except PteroAPIError as e:
            return await interaction.followup.send(f"❌ Couldn't find that server: {e}", ephemeral=True)
        key = name.strip().lower()
        cfg.servers[key] = ServerConfig(identifier=identifier, name=name.strip())
        await self.store.set_guild_config(cfg)
        panel_name = details.get("name")
        suffix = f" (panel calls it \"{panel_name}\")" if panel_name else ""
        await interaction.followup.send(f"✅ Registered `{key}` → `{identifier}`{suffix}.", ephemeral=True)

    @ptero.command(name="removeserver", description="Un-register a server nickname.")
    @app_commands.describe(server="Nickname to remove")
    @app_commands.default_permissions(administrator=True)
    async def ptero_removeserver(self, interaction: discord.Interaction, server: str):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        key = server.strip().lower()
        if key not in cfg.servers:
            return await interaction.response.send_message(f"No server registered as `{key}`.", ephemeral=True)
        del cfg.servers[key]
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"🗑️ Removed `{key}`.", ephemeral=True)

    @ptero_removeserver.autocomplete("server")
    async def _ac_removeserver(self, interaction: discord.Interaction, current: str):
        return await self._ac_server(interaction, current)

    @ptero.command(name="servers", description="List registered servers.")
    @app_commands.default_permissions(administrator=True)
    async def ptero_servers(self, interaction: discord.Interaction):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not cfg.servers:
            return await interaction.response.send_message("No servers registered. Use `/ptero addserver`.", ephemeral=True)
        lines = [f"`{key}` → `{sc.identifier}` ({sc.name})" for key, sc in cfg.servers.items()]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @ptero.command(name="panel", description="Post a live control panel (status + Start/Restart/Stop buttons) for a server.")
    @app_commands.describe(server="Server nickname (optional if you only have one registered)")
    @app_commands.default_permissions(administrator=True)
    async def ptero_panel(self, interaction: discord.Interaction, server: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await self.store.get_guild_config(interaction.guild_id)
        sc = self._resolve_server(cfg, server)
        client = self._client_for(cfg)
        if not client:
            return await interaction.followup.send("Run `/ptero setup` first.", ephemeral=True)
        if not sc:
            return await interaction.followup.send("Specify `server:` — you have more than one registered.", ephemeral=True)
        key = next(k for k, v in cfg.servers.items() if v is sc)
        try:
            resources = await client.get_resources(sc.identifier)
            embed = _panel_embed(sc, resources)
        except PteroAPIError as e:
            embed = _panel_embed(sc, None, error=str(e))
        view = PteroControlView(self, interaction.guild_id, key)
        message = await interaction.channel.send(embed=embed, view=view)
        sc.panel_channel_id = message.channel.id
        sc.panel_message_id = message.id
        await self.store.set_guild_config(cfg)
        await interaction.followup.send("✅ Panel posted.", ephemeral=True)

    @ptero_panel.autocomplete("server")
    async def _ac_panel(self, interaction: discord.Interaction, current: str):
        return await self._ac_server(interaction, current)

    @ptero.command(
        name="basicpanel",
        description="Post a simple Start-only panel non-admins (with the basic role) can use.",
    )
    @app_commands.describe(server="Server nickname (optional if you only have one registered)")
    @app_commands.default_permissions(administrator=True)
    async def ptero_basicpanel(self, interaction: discord.Interaction, server: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not cfg.basic_role_id:
            return await interaction.followup.send(
                "Set who's allowed first with `/ptero basicrole`, then post the panel.", ephemeral=True
            )
        sc = self._resolve_server(cfg, server)
        client = self._client_for(cfg)
        if not client:
            return await interaction.followup.send("Run `/ptero setup` first.", ephemeral=True)
        if not sc:
            return await interaction.followup.send("Specify `server:` — you have more than one registered.", ephemeral=True)
        key = next(k for k, v in cfg.servers.items() if v is sc)
        try:
            resources = await client.get_resources(sc.identifier)
            embed = _basic_panel_embed(sc, resources)
        except PteroAPIError as e:
            embed = _basic_panel_embed(sc, None, error=str(e))
        view = PteroBasicPanelView(self, interaction.guild_id, key)
        message = await interaction.channel.send(embed=embed, view=view)
        sc.basic_panel_channel_id = message.channel.id
        sc.basic_panel_message_id = message.id
        await self.store.set_guild_config(cfg)
        await interaction.followup.send("✅ Basic panel posted.", ephemeral=True)

    @ptero_basicpanel.autocomplete("server")
    async def _ac_basicpanel(self, interaction: discord.Interaction, current: str):
        return await self._ac_server(interaction, current)

    @ptero.command(name="status", description="Show current status for a server.")
    @app_commands.describe(server="Server nickname (optional if you only have one registered)")
    @app_commands.default_permissions(administrator=True)
    async def ptero_status(self, interaction: discord.Interaction, server: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await self.store.get_guild_config(interaction.guild_id)
        sc = self._resolve_server(cfg, server)
        client = self._client_for(cfg)
        if not client:
            return await interaction.followup.send("Run `/ptero setup` first.", ephemeral=True)
        if not sc:
            return await interaction.followup.send("Specify `server:` — you have more than one registered.", ephemeral=True)
        try:
            resources = await client.get_resources(sc.identifier)
            backups = await client.list_backups(sc.identifier)
        except PteroAPIError as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send(embed=_status_embed(sc, resources, backups), ephemeral=True)

    @ptero_status.autocomplete("server")
    async def _ac_status(self, interaction: discord.Interaction, current: str):
        return await self._ac_server(interaction, current)

    @ptero.command(name="backups", description="Show recent backups for a server.")
    @app_commands.describe(server="Server nickname (optional if you only have one registered)")
    @app_commands.default_permissions(administrator=True)
    async def ptero_backups(self, interaction: discord.Interaction, server: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await self.store.get_guild_config(interaction.guild_id)
        sc = self._resolve_server(cfg, server)
        client = self._client_for(cfg)
        if not client:
            return await interaction.followup.send("Run `/ptero setup` first.", ephemeral=True)
        if not sc:
            return await interaction.followup.send("Specify `server:` — you have more than one registered.", ephemeral=True)
        try:
            backups = await client.list_backups(sc.identifier)
        except PteroAPIError as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send(embed=_backups_embed(sc, backups), ephemeral=True)

    @ptero_backups.autocomplete("server")
    async def _ac_backups(self, interaction: discord.Interaction, current: str):
        return await self._ac_server(interaction, current)

    @ptero.command(name="alertchannel", description="Set the channel for automatic failure/backup alerts.")
    @app_commands.describe(channel="Channel to post alerts in")
    @app_commands.default_permissions(administrator=True)
    async def ptero_alertchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.alert_channel_id = channel.id
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"🔔 Alerts will post in {channel.mention}.", ephemeral=True)

    @ptero.command(name="basicrole", description="Set which role can use the basic Start-only panel.")
    @app_commands.describe(role="Role allowed to press Start on the basic panel (Administrators can always use it)")
    @app_commands.default_permissions(administrator=True)
    async def ptero_basicrole(self, interaction: discord.Interaction, role: discord.Role):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.basic_role_id = role.id
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(
            f"✅ {role.mention} can now press Start on the basic panel. Post it with `/ptero basicpanel`.",
            ephemeral=True,
        )

    @ptero.command(name="monitor", description="Enable/disable automatic monitoring, or tune its interval.")
    @app_commands.describe(enabled="Turn monitoring on/off", interval_seconds="How often to poll (default 60, min 30)")
    @app_commands.default_permissions(administrator=True)
    async def ptero_monitor(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        interval_seconds: Optional[int] = None,
    ):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if enabled is not None:
            cfg.monitor_enabled = enabled
        if interval_seconds is not None:
            cfg.poll_interval_seconds = max(30, interval_seconds)
        await self.store.set_guild_config(cfg)
        state = "enabled" if cfg.monitor_enabled else "disabled"
        await interaction.response.send_message(
            f"🩺 Monitoring **{state}**, polling every **{cfg.poll_interval_seconds}s**"
            f"{' (set an alert channel with /ptero alertchannel)' if not cfg.alert_channel_id else ''}.",
            ephemeral=True,
        )

    # ---------- background monitor ----------
    @tasks.loop(seconds=30)
    async def _monitor(self):
        try:
            configs = await self.store.all_guild_configs()
        except Exception:
            log.exception("Failed to load Pterodactyl configs for monitor tick.")
            return
        now = time.time()
        for cfg in configs:
            if not cfg.monitor_enabled or not cfg.servers:
                continue
            client = self._client_for(cfg)
            if not client:
                continue
            last_poll_key = f"_last_poll_{cfg.guild_id}"
            last_poll = getattr(self, last_poll_key, 0.0)
            if now - last_poll < cfg.poll_interval_seconds:
                continue
            setattr(self, last_poll_key, now)
            changed = False
            for key, sc in list(cfg.servers.items()):
                try:
                    server_changed, resources, error = await self._poll_server(cfg, key, sc, client, now)
                except Exception:
                    log.exception("Error polling Pterodactyl server %s/%s", cfg.guild_id, key)
                    continue
                changed = changed or server_changed
                await self._refresh_panel_message(cfg, sc, client, resources=resources, error=error)
            if changed:
                await self.store.set_guild_config(cfg)

    @_monitor.before_loop
    async def _before_monitor(self):
        await self.bot.wait_until_ready()

    async def _poll_server(
        self, cfg: GuildConfig, key: str, sc: ServerConfig, client: PteroClient, now: float
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        changed = False
        try:
            resources = await client.get_resources(sc.identifier)
        except PteroAPIError as e:
            return changed, None, str(e)  # transient panel/network hiccup — don't alert on a single miss
        state = resources.get("current_state")

        if sc.last_state is None:
            sc.last_state = state
            sc.state_since = now
            changed = True
        elif state != sc.last_state:
            # Only treat this as a suspicious flip if it wasn't caused by a
            # power command we ourselves issued — a normal admin-initiated
            # restart cycles running -> stopping -> offline -> starting ->
            # running and shouldn't count toward crash-loop detection.
            if sc.pending_signal is None:
                if now - sc.flip_window_start > CRASH_LOOP_WINDOW_SECONDS:
                    sc.flip_window_start = now
                    sc.flip_count = 0
                sc.flip_count += 1
                if sc.flip_count >= CRASH_LOOP_THRESHOLD:
                    await self._alert(
                        cfg,
                        f"⚠️ **{sc.name}** looks like it's crash-looping — state flipped "
                        f"{sc.flip_count}x in the last {CRASH_LOOP_WINDOW_SECONDS // 60}m. Currently `{state}`.",
                    )
                    sc.flip_count = 0
                elif state == "offline" and sc.last_state == "running":
                    await self._alert(cfg, f"🔴 **{sc.name}** went offline unexpectedly (was `running`).")
            sc.last_state = state
            sc.state_since = now
            changed = True

        if sc.pending_signal:
            if (state == "running" and sc.pending_signal in ("start", "restart")) or (
                state == "offline" and sc.pending_signal in ("stop", "kill")
            ):
                sc.pending_signal = None
                changed = True
            elif now - sc.pending_since > cfg.restart_timeout_seconds:
                await self._alert(
                    cfg,
                    f"⏱️ **{sc.name}**: `{sc.pending_signal}` was sent {cfg.restart_timeout_seconds}s ago "
                    f"but the server is still `{state}` — it may have failed to {sc.pending_signal}.",
                )
                sc.pending_signal = None
                changed = True

        try:
            backups = await client.list_backups(sc.identifier)
        except PteroAPIError:
            backups = []
        for b in backups:
            buuid = b.get("uuid")
            if not buuid:
                continue
            if b.get("completed_at") is None:
                created = _parse_iso(b.get("created_at"))
                if created and buuid not in sc.stuck_backup_ids:
                    age_minutes = (now - created.timestamp()) / 60
                    if age_minutes > cfg.backup_stuck_minutes:
                        await self._alert(
                            cfg,
                            f"🗄️ **{sc.name}**: backup `{b.get('name') or buuid[:8]}` has been running for "
                            f"{int(age_minutes)}m — it may be stuck.",
                        )
                        sc.stuck_backup_ids.append(buuid)
                        sc.stuck_backup_ids = sc.stuck_backup_ids[-MAX_KNOWN_BACKUPS:]
                        changed = True
                continue
            if buuid in sc.known_backup_ids:
                continue
            sc.known_backup_ids.append(buuid)
            sc.known_backup_ids = sc.known_backup_ids[-MAX_KNOWN_BACKUPS:]
            changed = True
            if not b.get("is_successful"):
                await self._alert(cfg, f"❌ **{sc.name}**: backup `{b.get('name') or buuid[:8]}` failed.")

        return changed, resources, None


async def setup(bot: commands.Bot):
    await bot.add_cog(PterodactylCog(bot))
