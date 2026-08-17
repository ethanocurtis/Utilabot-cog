# cogs/minecraft.py
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

# External deps (install: pip install mcstatus mcrcon cryptography aiosqlite Pillow)
from mcstatus import JavaServer, BedrockServer
from mcrcon import MCRcon
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

CONFIG_PATH = "data/minecraft_config.json"
STATS_DB_PATH = "data/minecraft_stats.db"
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)

ASSETS_DIR = "assets"
GRASS_ICON_LOCAL = os.path.join(ASSETS_DIR, "grass_block_icon.png")
BANNER_FONT_PATH = os.path.join(ASSETS_DIR, "fonts", "PressStart2P-Regular.ttf")


# =========================
# Secret encryption (RCON passwords at rest)
# =========================
def _load_fernet() -> Optional[Fernet]:
    """Loads an encryption key from MC_SECRET_KEY if set.

    Without a key, RCON passwords are stored as-is (legacy behavior).
    Generate a key with:
      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    """
    key = os.environ.get("MC_SECRET_KEY")
    if not key:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        log.warning("MC_SECRET_KEY is set but invalid; RCON passwords will be stored unencrypted.")
        return None


_FERNET = _load_fernet()
if _FERNET is None:
    log.warning(
        "MC_SECRET_KEY not set (or invalid) — RCON passwords will be stored in plaintext in %s. "
        "Set MC_SECRET_KEY to enable encryption at rest.",
        CONFIG_PATH,
    )


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value or _FERNET is None:
        return value
    return _FERNET.encrypt(value.encode()).decode()


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """Decrypts a stored secret. Falls back to the raw value for legacy
    plaintext entries or if no key is configured, so old configs keep working."""
    if not value or _FERNET is None:
        return value
    try:
        return _FERNET.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return value


# =========================
# Data / persistence layer
# =========================
@dataclass
class ServerConfig:
    kind: str                     # "java" or "bedrock"
    host: str
    port: Optional[int] = None    # optional; defaults below
    rcon_host: Optional[str] = None
    rcon_port: Optional[int] = None
    rcon_password: Optional[str] = None  # encrypted at rest when MC_SECRET_KEY is set
    # legacy (ignored in RCON-only mode; kept for compatibility so old JSON loads)
    properties_path: Optional[str] = None

    # Sticky embed config
    sticky_channel_id: Optional[int] = None
    sticky_message_id: Optional[int] = None
    sticky_interval_min: Optional[int] = None  # minutes (>=1)

    # Join/leave notification channel
    player_log_channel_id: Optional[int] = None

    # Custom name shown in the status embed title/author (defaults to "Minecraft Server")
    display_name: Optional[str] = None


def load_all_configs() -> Dict[str, ServerConfig]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, ServerConfig] = {}
    for gid, obj in raw.items():
        allowed = {
            "kind","host","port","rcon_host","rcon_port","rcon_password","properties_path",
            "sticky_channel_id","sticky_message_id","sticky_interval_min",
            "player_log_channel_id","display_name",
        }
        slim = {k: v for k, v in obj.items() if k in allowed}
        out[gid] = ServerConfig(**slim)
    return out


def save_all_configs(cfgs: Dict[str, ServerConfig]) -> None:
    raw = {gid: asdict(cfg) for gid, cfg in cfgs.items()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


def _migrate_legacy_secrets(cfgs: Dict[str, ServerConfig]) -> bool:
    """Encrypts any plaintext RCON passwords in-place if a key is configured.
    Returns True if anything changed (caller should persist)."""
    if _FERNET is None:
        return False
    changed = False
    for cfg in cfgs.values():
        if not cfg.rcon_password:
            continue
        try:
            _FERNET.decrypt(cfg.rcon_password.encode())
            # Already encrypted with the current key; nothing to do.
        except (InvalidToken, ValueError):
            cfg.rcon_password = encrypt_secret(cfg.rcon_password)
            changed = True
    return changed


# =========================
# Helpers
# =========================
def default_port_for(kind: str) -> int:
    return 25565 if kind == "java" else 19132


def effective_port(kind: str, port: Optional[int]) -> int:
    return port if port is not None else default_port_for(kind)


def rcon_ready(cfg: Optional[ServerConfig]) -> bool:
    return bool(cfg and cfg.rcon_host and cfg.rcon_port and cfg.rcon_password)


def rcon_creds(cfg: ServerConfig) -> Tuple[str, int, str]:
    """Returns (host, port, decrypted_password) for an RCON-ready config."""
    port = effective_port(cfg.kind, cfg.rcon_port)
    return cfg.rcon_host, port, decrypt_secret(cfg.rcon_password)


def render_template(template: str, values: Dict[str, str]) -> str:
    """Substitutes {key} tokens with values via plain string replace (not
    str.format), so templates containing literal braces (e.g. JSON in the
    `title` command) aren't misparsed."""
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    # Collapse whitespace left behind by empty optional fields (e.g. a blank reason).
    return re.sub(r" {2,}", " ", out).strip()


def member_can_manage(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


async def deny_if_not_manager(interaction: discord.Interaction) -> bool:
    """Sends a denial message and returns True if the user lacks manage_guild."""
    if member_can_manage(interaction):
        return False
    await interaction.response.send_message(
        "⚠️ You need **Manage Server** permission to do that.", ephemeral=True
    )
    return True


# =========================
# MC utilities
# =========================
async def mc_status(kind: str, host: str, port: Optional[int]) -> Tuple[bool, Dict]:
    """Returns (ok, data). For Java/Bedrock, grabs status details."""
    loop = asyncio.get_running_loop()
    p = effective_port(kind, port)
    data: Dict = {}
    try:
        if kind == "java":
            server = await loop.run_in_executor(None, lambda: JavaServer(host, p))
            status = await loop.run_in_executor(None, server.status)
            data = {
                "motd": getattr(status.description, "to_plain", lambda: str(status.description))(),
                "version": getattr(status.version, "name", str(status.version)),
                # ping removed from embed; no need to record latency anymore
                "players_online": status.players.online,
                "players_max": status.players.max,
                "sample": [p.name for p in (status.players.sample or [])],
            }
            return True, data

        elif kind == "bedrock":
            def _bedrock_status():
                server = BedrockServer(host, p)
                return server.status()
            status = await loop.run_in_executor(None, _bedrock_status)
            data = {
                "motd": status.motd,
                "version": status.version.brand + " " + status.version.version,
                "players_online": status.players_online,
                "players_max": status.players_max,
                "sample": [],  # Bedrock ping doesn't provide names
            }
            return True, data

        else:
            return False, {"error": "Unknown server kind."}
    except Exception as e:
        return False, {"error": str(e)}


# =========================
# RCON helpers (offloaded to a thread; the socket I/O is blocking)
# =========================
async def rcon_exec(host: str, port: int, password: str, command: str) -> Tuple[bool, str]:
    loop = asyncio.get_running_loop()

    def _run() -> str:
        with MCRcon(host, password, port=port) as rcon:
            return rcon.command(command)

    try:
        resp = await loop.run_in_executor(None, _run)
        return True, resp or "(no response)"
    except Exception as e:
        return False, str(e)


async def rcon_stop_sequence(host: str, port: int, password: str, *, warn_secs: int = 5) -> Tuple[bool, str]:
    ok, resp = await rcon_exec(host, port, password, f"say Server stopping in {warn_secs}s… Please log out safely.")
    if not ok:
        return False, f"announce failed: {resp}"
    await asyncio.sleep(max(0, warn_secs))
    ok, resp = await rcon_exec(host, port, password, "save-all flush")
    if not ok:
        return False, f"save failed: {resp}"
    ok, resp = await rcon_exec(host, port, password, "stop")
    if not ok:
        return False, f"stop failed: {resp}"
    return True, "stop issued"


async def rcon_player_names_if_available(cfg: ServerConfig) -> List[str]:
    if cfg.kind != "java" or not rcon_ready(cfg):
        return []
    host, port, password = rcon_creds(cfg)
    ok, resp = await rcon_exec(host, port, password, "list")
    if not ok or not resp:
        return []
    part = resp.split(":", 1)
    names_blob = part[1] if len(part) > 1 else ""
    names = [n.strip() for n in names_blob.split(",") if n.strip()]
    return names


# =========================
# Stats history (uptime / peak players / join-leave log)
# =========================
class StatsStore:
    """Lightweight SQLite-backed history for server samples and player events."""

    def __init__(self, path: str):
        self.path = path
        self._init_schema()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS samples ("
                "guild_id TEXT NOT NULL, ts INTEGER NOT NULL, online INTEGER NOT NULL, "
                "players INTEGER NOT NULL DEFAULT 0, max_players INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_guild_ts ON samples(guild_id, ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS player_events ("
                "guild_id TEXT NOT NULL, ts INTEGER NOT NULL, player TEXT NOT NULL, event TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_guild_ts ON player_events(guild_id, ts)")
            conn.commit()
        finally:
            conn.close()

    async def record_sample(self, gid: str, ts: int, online: bool, players: int, max_players: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO samples (guild_id, ts, online, players, max_players) VALUES (?,?,?,?,?)",
                (gid, ts, int(online), players, max_players),
            )
            await db.commit()

    async def record_event(self, gid: str, ts: int, player: str, event: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO player_events (guild_id, ts, player, event) VALUES (?,?,?,?)",
                (gid, ts, player, event),
            )
            await db.commit()

    async def summary(self, gid: str, hours: int) -> Optional[Dict]:
        cutoff = int(time.time()) - hours * 3600
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT online, players FROM samples WHERE guild_id=? AND ts>=?", (gid, cutoff)
            )
            rows = await cur.fetchall()
        if not rows:
            return None
        total = len(rows)
        online_count = sum(1 for r in rows if r[0])
        peak = max((r[1] for r in rows), default=0)
        return {"total": total, "uptime_pct": (online_count / total) * 100, "peak": peak}

    async def hourly_peaks(self, gid: str, hours: int = 24) -> List[int]:
        now_hour = int(time.time() // 3600)
        start_hour = now_hour - hours + 1
        cutoff = start_hour * 3600
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT CAST(ts/3600 AS INTEGER) AS bucket, MAX(players) FROM samples "
                "WHERE guild_id=? AND ts>=? GROUP BY bucket",
                (gid, cutoff),
            )
            rows = await cur.fetchall()
        bucket_map = {int(b): (m or 0) for b, m in rows}
        return [bucket_map.get(start_hour + i, 0) for i in range(hours)]

    async def prune(self, sample_days: int = 30, event_days: int = 14) -> None:
        sample_cutoff = int(time.time()) - sample_days * 86400
        event_cutoff = int(time.time()) - event_days * 86400
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM samples WHERE ts < ?", (sample_cutoff,))
            await db.execute("DELETE FROM player_events WHERE ts < ?", (event_cutoff,))
            await db.commit()


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: List[int]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return (_SPARK_CHARS[0] if hi == 0 else _SPARK_CHARS[4]) * len(values)
    span = hi - lo
    return "".join(_SPARK_CHARS[int((v - lo) / span * (len(_SPARK_CHARS) - 1))] for v in values)


# =========================
# Status banner image (embed.set_image) — pixel-art styled server card,
# rendered with Pillow and attached to the message rather than hosted
# externally, so it never depends on a third-party URL staying up.
# =========================
BANNER_W, BANNER_H = 900, 170
BANNER_GROUND_H = 22
BANNER_CORNER_RADIUS = 18

_BANNER_BG_TOP = (20, 32, 22)
_BANNER_BG_BOTTOM = (11, 15, 13)
_BANNER_GREEN = (108, 210, 90)
_BANNER_RED = (224, 90, 90)
_BANNER_MUTED = (172, 180, 174)
_BANNER_WHITE = (235, 240, 236)
_BANNER_BAR_BG = (40, 48, 42)
_BANNER_BAR_BORDER = (70, 82, 72)

_banner_font_cache: Dict[int, ImageFont.FreeTypeFont] = {}


def _banner_font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _banner_font_cache:
        _banner_font_cache[size] = ImageFont.truetype(BANNER_FONT_PATH, size)
    return _banner_font_cache[size]


def _banner_vertical_gradient(w: int, h: int, top: Tuple[int, int, int], bottom: Tuple[int, int, int]) -> Image.Image:
    base = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=c)
    return base


def _banner_ground_strip(banner: Image.Image, grass_tile: Image.Image) -> None:
    x = 0
    while x < BANNER_W:
        banner.paste(grass_tile, (x, BANNER_H - BANNER_GROUND_H), grass_tile)
        x += BANNER_GROUND_H
    ImageDraw.Draw(banner).line(
        [(0, BANNER_H - BANNER_GROUND_H), (BANNER_W, BANNER_H - BANNER_GROUND_H)],
        fill=(20, 60, 24), width=2,
    )


def _banner_text_shadow(draw: ImageDraw.ImageDraw, pos, text: str, font: ImageFont.FreeTypeFont, fill, offset: int = 2) -> None:
    x, y = pos
    draw.text((x + offset, y + offset), text, font=font, fill=(0, 0, 0, 150))
    draw.text((x, y), text, font=font, fill=fill)


def _banner_capacity_bar(draw: ImageDraw.ImageDraw, x, y, w, h, frac: float, color) -> None:
    frac = max(0.0, min(1.0, frac))
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=_BANNER_BAR_BG, outline=_BANNER_BAR_BORDER, width=2)
    if frac > 0:
        fill_w = max(h, int(w * frac))
        draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=h // 2, fill=color)


def _banner_round_corners(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width, img.height], radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _render_status_banner_sync(
    *, online: bool, host: str, version: Optional[str], players: Optional[int], max_players: Optional[int],
) -> bytes:
    grass = Image.open(GRASS_ICON_LOCAL).convert("RGBA")

    banner = _banner_vertical_gradient(BANNER_W, BANNER_H, _BANNER_BG_TOP, _BANNER_BG_BOTTOM).convert("RGBA")
    _banner_ground_strip(banner, grass.resize((BANNER_GROUND_H, BANNER_GROUND_H), Image.NEAREST))
    draw = ImageDraw.Draw(banner)

    dot_color, label = (_BANNER_GREEN, "ONLINE") if online else (_BANNER_RED, "OFFLINE")

    pad = 30
    dot_r = 9
    dot_cy = pad + 18
    draw.ellipse([pad, dot_cy - dot_r, pad + dot_r * 2, dot_cy + dot_r], fill=dot_color)
    _banner_text_shadow(draw, (pad + dot_r * 2 + 14, pad), label, _banner_font(24), _BANNER_WHITE)
    _banner_text_shadow(draw, (pad, pad + 42), host[:38], _banner_font(13), _BANNER_MUTED)

    if online and players is not None and max_players:
        _banner_text_shadow(draw, (pad, pad + 72), f"{players} / {max_players} PLAYERS", _banner_font(13), _BANNER_WHITE)
        _banner_capacity_bar(draw, pad, pad + 96, BANNER_W - pad * 2, 14, players / max_players, _BANNER_GREEN)

    icon_size = 40
    icon_x = BANNER_W - pad - icon_size
    icon_img = grass.resize((icon_size, icon_size), Image.NEAREST)
    banner.paste(icon_img, (icon_x, pad - 6), icon_img)
    if version:
        vtext = version[:16]
        bbox = draw.textbbox((0, 0), vtext, font=_banner_font(11))
        vw = bbox[2] - bbox[0]
        _banner_text_shadow(draw, (icon_x + icon_size // 2 - vw // 2, pad - 6 + icon_size + 4), vtext, _banner_font(11), _BANNER_MUTED)

    banner = _banner_round_corners(banner, BANNER_CORNER_RADIUS)
    buf = io.BytesIO()
    banner.save(buf, format="PNG")
    return buf.getvalue()


async def render_status_banner(
    *, online: bool, host: str, version: Optional[str] = None,
    players: Optional[int] = None, max_players: Optional[int] = None,
) -> Optional[discord.File]:
    """Renders the status banner off the event loop. Returns None (instead of
    raising) if the asset/font is missing or rendering otherwise fails, so a
    banner problem never breaks the embed itself."""
    loop = asyncio.get_running_loop()
    try:
        png_bytes = await loop.run_in_executor(
            None,
            lambda: _render_status_banner_sync(
                online=online, host=host, version=version, players=players, max_players=max_players
            ),
        )
    except Exception:
        log.exception("Failed to render status banner")
        return None
    return discord.File(io.BytesIO(png_bytes), filename="mc_status.png")


# =========================
# Generic RCON command model
# =========================
class RconCmd:
    def __init__(self, key: str, label: str, template: str, fields: Optional[List[Dict]] = None, danger: bool = False):
        self.key = key
        self.label = label
        self.template = template
        self.fields = fields or []
        self.danger = danger


RCON_COMMANDS: List[RconCmd] = [
    RconCmd("list", "Player list", "list"),
    RconCmd("say", "Broadcast message", "say {msg}", fields=[{"id": "msg", "label": "Message", "placeholder": "Server restarting soon!", "min":1, "max":200, "required": True}]),
    RconCmd("wl_on", "Whitelist ON", "whitelist on"),
    RconCmd("wl_off", "Whitelist OFF", "whitelist off"),
    RconCmd("wl_add", "Whitelist add <name>", "whitelist add {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),
    RconCmd("wl_remove", "Whitelist remove <name>", "whitelist remove {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),
    RconCmd("wl_list", "Whitelist list", "whitelist list"),
    RconCmd("op", "OP <name>", "op {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),
    RconCmd("deop", "DEOP <name>", "deop {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),
    RconCmd("kick", "Kick <name> [reason]", "kick {name} {reason}", fields=[
        {"id":"name","label":"Player name","placeholder":"Griefer123","min":2,"max":16,"required":True},
        {"id":"reason","label":"Reason (optional)","placeholder":"Keep it short","min":0,"max":60,"required":False},
    ]),
    RconCmd("ban", "Ban <name> [reason]", "ban {name} {reason}", fields=[
        {"id":"name","label":"Player name","placeholder":"Griefer123","min":2,"max":16,"required":True},
        {"id":"reason","label":"Reason (optional)","placeholder":"Griefing","min":0,"max":60,"required":False},
    ]),
    RconCmd("pardon", "Pardon <name>", "pardon {name}", fields=[{"id":"name","label":"Player name","placeholder":"Friend","min":2,"max":16,"required":True}]),
    RconCmd("difficulty", "Difficulty (pick)", "difficulty {level}"),
    RconCmd("gamemode", "Default gamemode (pick)", "defaultgamemode {mode}"),
    RconCmd("weather", "Weather (pick)", "weather {kind}"),
    RconCmd("time", "Time set (pick)", "time set {value}"),
    RconCmd("gamerule_preset", "Gamerule presets (pick/toggle)", "gamerule {rule} {value}"),
    RconCmd("tp", "Teleport <target> <destination>", "tp {target} {dest}", fields=[
        {"id":"target","label":"Target selector or player","placeholder":"Player | @a | @p","min":1,"max":32,"required":True},
        {"id":"dest","label":"Destination","placeholder":"Player | x y z","min":1,"max":64,"required":True},
    ]),
    RconCmd("title", "Title all players", 'title @a title {"text":"{text}"}', fields=[{"id":"text","label":"Title text","placeholder":"Welcome!","min":1,"max":100,"required":True}]),
    RconCmd("save_flush", "Save-all flush", "save-all flush"),
    RconCmd("save_on", "Save-on", "save-on"),
    RconCmd("save_off", "Save-off", "save-off"),
    RconCmd("stop", "STOP server (confirm)", "stop", danger=True),
]

# Commands that target a specific player; these get a "pick from online players" UI
# instead of a free-text name field where possible.
PLAYER_TARGET_CMDS = {"wl_add", "wl_remove", "op", "deop", "kick", "ban", "pardon"}


# =========================
# UI: RCON password modal (used by /mc setup — keeps the password out of
# the visible slash-command invocation line that Discord posts to the channel)
# =========================
class RconSetupModal(discord.ui.Modal, title="RCON Password"):
    password = discord.ui.TextInput(
        label="RCON Password",
        placeholder="Leave blank to keep the existing password",
        required=False,
        max_length=200,
    )

    def __init__(self, cog: "MinecraftCog", gid: str, kind: str, host: str, port: int,
                 rcon_host: str, rcon_port: int, existing: Optional[ServerConfig],
                 display_name: Optional[str] = None):
        super().__init__()
        self.cog = cog
        self.gid = gid
        self.kind = kind
        self.host = host
        self.port = port
        self.rcon_host = rcon_host
        self.rcon_port = rcon_port
        self.existing = existing
        self.display_name = display_name

    async def on_submit(self, interaction: discord.Interaction):
        new_password = self.password.value.strip()
        if new_password:
            stored_password = encrypt_secret(new_password)
        elif self.existing and self.existing.rcon_password:
            stored_password = self.existing.rcon_password  # keep prior password
        else:
            return await interaction.response.send_message(
                "⚠️ No RCON password provided and none was previously set. Try `/mc setup` again.",
                ephemeral=True,
            )

        cfg = ServerConfig(
            kind=self.kind,
            host=self.host,
            port=self.port,
            rcon_host=self.rcon_host,
            rcon_port=self.rcon_port,
            rcon_password=stored_password,
            sticky_channel_id=self.existing.sticky_channel_id if self.existing else None,
            sticky_message_id=self.existing.sticky_message_id if self.existing else None,
            sticky_interval_min=self.existing.sticky_interval_min if self.existing else None,
            player_log_channel_id=self.existing.player_log_channel_id if self.existing else None,
            display_name=self.display_name,
        )
        self.cog.configs[self.gid] = cfg
        save_all_configs(self.cog.configs)
        await interaction.response.send_message(
            "✅ Minecraft configuration saved for this server.\n"
            f"- Kind: **{self.kind}**\n"
            f"- Address: `{self.host}:{self.port}`\n"
            "- RCON: configured 🔒",
            ephemeral=True,
        )


# =========================
# UI: Dynamic RCON command modal (also used for the reason field after
# picking a player from a select menu, via `prefill`)
# =========================
class CommandModal(discord.ui.Modal):
    def __init__(self, cog: "MinecraftCog", guild_id: int, cmd: RconCmd, prefill: Optional[Dict[str, str]] = None):
        super().__init__(title=cmd.label[:45])
        self.cog = cog
        self.guild_id = guild_id
        self.cmd = cmd
        self.prefill = prefill or {}
        self.inputs: Dict[str, discord.ui.TextInput] = {}
        for field in cmd.fields:
            if field["id"] in self.prefill:
                continue
            ti = discord.ui.TextInput(
                label=field["label"],
                placeholder=field.get("placeholder", "") or None,
                required=field.get("required", True),
                min_length=field.get("min") or 0,
                max_length=field.get("max") or None,
            )
            self.inputs[field["id"]] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        if await deny_if_not_manager(interaction):
            return
        cfg = self.cog.configs.get(str(self.guild_id))
        if not rcon_ready(cfg) or cfg.kind != "java":
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)

        values = dict(self.prefill)
        for fid, ti in self.inputs.items():
            values[fid] = ti.value.strip()
        command = render_template(self.cmd.template, values)

        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, command)
        msg = f"✅ Ran `{command}`.\n```{resp}```" if ok else f"❌ `{command}` failed:\n`{resp}`"
        await interaction.followup.send(msg, ephemeral=True)


# =========================
# UI: Pre-filled choice views
# =========================
class DifficultyChoiceView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(timeout=60); self.cog=cog; self.guild_id=guild_id

    async def _run(self, interaction: discord.Interaction, value: str):
        if await deny_if_not_manager(interaction):
            return
        cfg = self.cog.configs.get(str(self.guild_id))
        if not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, f"difficulty {value}")
        await interaction.followup.send((f"✅ Difficulty set to **{value}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)

    @discord.ui.button(label="Peaceful", style=discord.ButtonStyle.secondary, emoji="🕊️")
    async def peaceful(self, i: discord.Interaction, _): await self._run(i, "peaceful")
    @discord.ui.button(label="Easy", style=discord.ButtonStyle.secondary, emoji="🙂")
    async def easy(self, i: discord.Interaction, _): await self._run(i, "easy")
    @discord.ui.button(label="Normal", style=discord.ButtonStyle.primary, emoji="😎")
    async def normal(self, i: discord.Interaction, _): await self._run(i, "normal")
    @discord.ui.button(label="Hard", style=discord.ButtonStyle.danger, emoji="🔥")
    async def hard(self, i: discord.Interaction, _): await self._run(i, "hard")


class GamemodeChoiceView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(timeout=60); self.cog=cog; self.guild_id=guild_id

    async def _run(self, interaction: discord.Interaction, value: str):
        if await deny_if_not_manager(interaction):
            return
        cfg = self.cog.configs.get(str(self.guild_id))
        if not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, f"defaultgamemode {value}")
        await interaction.followup.send((f"✅ Default gamemode set to **{value}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)

    @discord.ui.button(label="Survival", style=discord.ButtonStyle.secondary, emoji="⛏️")
    async def survival(self, i: discord.Interaction, _): await self._run(i, "survival")
    @discord.ui.button(label="Creative", style=discord.ButtonStyle.primary, emoji="📦")
    async def creative(self, i: discord.Interaction, _): await self._run(i, "creative")
    @discord.ui.button(label="Adventure", style=discord.ButtonStyle.secondary, emoji="🧭")
    async def adventure(self, i: discord.Interaction, _): await self._run(i, "adventure")
    @discord.ui.button(label="Spectator", style=discord.ButtonStyle.secondary, emoji="👁️")
    async def spectator(self, i: discord.Interaction, _): await self._run(i, "spectator")


class WeatherChoiceView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(timeout=60); self.cog=cog; self.guild_id=guild_id

    async def _run(self, interaction: discord.Interaction, kind: str):
        if await deny_if_not_manager(interaction):
            return
        cfg = self.cog.configs.get(str(self.guild_id))
        if not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, f"weather {kind}")
        await interaction.followup.send((f"✅ Weather set to **{kind}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.success, emoji="☀️")
    async def clear(self, i: discord.Interaction, _): await self._run(i, "clear")
    @discord.ui.button(label="Rain", style=discord.ButtonStyle.secondary, emoji="🌧️")
    async def rain(self, i: discord.Interaction, _): await self._run(i, "rain")
    @discord.ui.button(label="Thunder", style=discord.ButtonStyle.danger, emoji="⛈️")
    async def thunder(self, i: discord.Interaction, _): await self._run(i, "thunder")


class TimeChoiceView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(timeout=60); self.cog=cog; self.guild_id=guild_id

    async def _run(self, interaction: discord.Interaction, value: str):
        if await deny_if_not_manager(interaction):
            return
        cfg = self.cog.configs.get(str(self.guild_id))
        if not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, f"time set {value}")
        await interaction.followup.send((f"✅ Time set to **{value}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)

    @discord.ui.button(label="Day", style=discord.ButtonStyle.secondary, emoji="🌤️")
    async def day(self, i: discord.Interaction, _): await self._run(i, "day")
    @discord.ui.button(label="Noon", style=discord.ButtonStyle.secondary, emoji="🌞")
    async def noon(self, i: discord.Interaction, _): await self._run(i, "noon")
    @discord.ui.button(label="Night", style=discord.ButtonStyle.primary, emoji="🌙")
    async def night(self, i: discord.Interaction, _): await self._run(i, "night")
    @discord.ui.button(label="Midnight", style=discord.ButtonStyle.danger, emoji="🌌")
    async def midnight(self, i: discord.Interaction, _): await self._run(i, "midnight")


GAMERULE_PRESETS = [
    ("keepInventory", ["true", "false"]),
    ("doDaylightCycle", ["true", "false"]),
    ("doWeatherCycle", ["true", "false"]),
    ("doMobGriefing", ["true", "false"]),
    ("doFireTick", ["true", "false"]),
    ("doImmediateRespawn", ["true", "false"]),
    ("showCoordinates", ["true", "false"]),
]

class GamerulePresetView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(timeout=90); self.cog=cog; self.guild_id=guild_id
        self.add_item(GameruleSelect(cog, guild_id))
        self.add_item(GameruleValueSelect(cog, guild_id))

class GameruleSelect(discord.ui.Select):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        self.cog=cog; self.guild_id=guild_id
        options = [discord.SelectOption(label=name, value=name) for name, _ in GAMERULE_PRESETS]
        super().__init__(placeholder="Choose gamerule…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if await deny_if_not_manager(interaction):
            return
        self.view.selected_rule = self.values[0]
        await interaction.response.send_message(f"Rule selected: `{self.view.selected_rule}`. Now pick a value.", ephemeral=True)

class GameruleValueSelect(discord.ui.Select):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        self.cog=cog; self.guild_id=guild_id
        options = [discord.SelectOption(label="true", value="true"), discord.SelectOption(label="false", value="false")]
        super().__init__(placeholder="Set value…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if await deny_if_not_manager(interaction):
            return
        cfg = self.cog.configs.get(str(self.guild_id))
        if not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        rule = getattr(self.view, "selected_rule", None)
        if not rule:
            return await interaction.response.send_message("Pick a gamerule first.", ephemeral=True)
        host, port, password = rcon_creds(cfg)
        value = self.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, f"gamerule {rule} {value}")
        await interaction.followup.send((f"✅ `gamerule {rule} {value}`\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)


# =========================
# UI: player-picker (used for kick/ban/op/deop/whitelist/pardon)
# =========================
class PlayerNameSelect(discord.ui.Select):
    def __init__(self, cog: "MinecraftCog", guild_id: int, cmd: RconCmd, names: List[str]):
        self.cog = cog; self.guild_id = guild_id; self.cmd = cmd
        options = [discord.SelectOption(label=n) for n in names[:25]]
        super().__init__(placeholder="Choose an online player…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if await deny_if_not_manager(interaction):
            return
        name = self.values[0]
        cmd = self.cmd
        other_fields = [f for f in cmd.fields if f["id"] != "name"]
        if other_fields:
            return await interaction.response.send_modal(
                CommandModal(self.cog, self.guild_id, cmd, prefill={"name": name})
            )

        cfg = self.cog.configs.get(str(self.guild_id))
        if not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        host, port, password = rcon_creds(cfg)
        command = render_template(cmd.template, {"name": name})
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, command)
        msg = f"✅ Ran `{command}`.\n```{resp}```" if ok else f"❌ `{command}` failed:\n`{resp}`"
        await interaction.followup.send(msg, ephemeral=True)


class ManualEntryButton(discord.ui.Button):
    def __init__(self, cog: "MinecraftCog", guild_id: int, cmd: RconCmd):
        super().__init__(label="Type name manually", style=discord.ButtonStyle.secondary, emoji="⌨️")
        self.cog = cog; self.guild_id = guild_id; self.cmd = cmd

    async def callback(self, interaction: discord.Interaction):
        if await deny_if_not_manager(interaction):
            return
        await interaction.response.send_modal(CommandModal(self.cog, self.guild_id, self.cmd))


class PlayerPickView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, cmd: RconCmd, names: List[str], timeout: int = 60):
        super().__init__(timeout=timeout)
        if names:
            self.add_item(PlayerNameSelect(cog, guild_id, cmd, names))
        self.add_item(ManualEntryButton(cog, guild_id, cmd))


# =========================
# Other views
# =========================
class ConfirmStopView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, *, restart_hint: bool = False, timeout: int = 20):
        super().__init__(timeout=timeout)
        self.cog = cog; self.guild_id = guild_id; self.restart_hint = restart_hint

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="🛑")
    async def confirm(self, interaction: discord.Interaction, _):
        if await deny_if_not_manager(interaction):
            return
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)
        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_stop_sequence(host, port, password)
        if ok:
            hint = " (your service manager should bring it back up)" if self.restart_hint else ""
            await interaction.followup.send(f"✅ Stop issued{hint}.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Stop failed: `{resp}`", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _):
        await interaction.response.send_message("Operation cancelled.", ephemeral=True)
        self.stop()


class RconSelect(discord.ui.Select):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        self.cog = cog
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label=cmd.label, value=cmd.key, emoji=("❗" if cmd.danger else "🧰"))
            for cmd in RCON_COMMANDS
        ]
        super().__init__(placeholder="Run an RCON command…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # RCON commands are administrative — gate the whole dropdown here so no
        # sub-view (modals, choice buttons, stop confirmation) is ever reachable
        # by a member without Manage Server.
        if await deny_if_not_manager(interaction):
            return

        cmd_key = self.values[0]
        cmd = next((c for c in RCON_COMMANDS if c.key == cmd_key), None)
        if not cmd:
            return await interaction.response.send_message("Unknown command.", ephemeral=True)

        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)

        if cmd.key == "difficulty":
            return await interaction.response.send_message("Pick a difficulty:", view=DifficultyChoiceView(self.cog, self.guild_id), ephemeral=True)
        if cmd.key == "gamemode":
            return await interaction.response.send_message("Pick a default gamemode:", view=GamemodeChoiceView(self.cog, self.guild_id), ephemeral=True)
        if cmd.key == "weather":
            return await interaction.response.send_message("Pick the weather:", view=WeatherChoiceView(self.cog, self.guild_id), ephemeral=True)
        if cmd.key == "time":
            return await interaction.response.send_message("Pick a time:", view=TimeChoiceView(self.cog, self.guild_id), ephemeral=True)
        if cmd.key == "gamerule_preset":
            return await interaction.response.send_message("Gamerule presets:", view=GamerulePresetView(self.cog, self.guild_id), ephemeral=True)

        if cmd.danger:
            return await interaction.response.send_message(
                "Are you sure you want to **stop** the server? (save-all + stop)",
                view=ConfirmStopView(self.cog, self.guild_id, restart_hint=True),
                ephemeral=True,
            )

        if cmd.key in PLAYER_TARGET_CMDS:
            names = await rcon_player_names_if_available(cfg)
            prompt = f"Pick a player for **{cmd.label}**:" if names else f"No players currently online — use **Type name manually** for **{cmd.label}**:"
            return await interaction.response.send_message(prompt, view=PlayerPickView(self.cog, self.guild_id, cmd, names), ephemeral=True)

        if cmd.fields:
            return await interaction.response.send_modal(CommandModal(self.cog, self.guild_id, cmd))

        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(host, port, password, cmd.template)
        msg = f"✅ Ran `{cmd.template}`.\n```{resp}```" if ok else f"❌ `{cmd.template}` failed:\n`{resp}`"
        await interaction.followup.send(msg, ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, *, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.cog = cog; self.guild_id = guild_id
        self.add_item(RconSelect(cog, guild_id))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, file = await self.cog.build_status_embed(interaction.guild_id)
        kwargs = {"file": file} if file else {}
        await interaction.followup.send("Updated status:", embed=embed, ephemeral=True, **kwargs)

    @discord.ui.button(label="Copy IP", style=discord.ButtonStyle.secondary, emoji="📋")
    async def copy_ip(self, interaction: discord.Interaction, _):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg:
            return await interaction.response.send_message("No config found.", ephemeral=True)
        show_port = effective_port(cfg.kind, cfg.port)
        await interaction.response.send_message(f"`{cfg.host}:{show_port}` (for **{cfg.kind.upper()}**)", ephemeral=True)

    @discord.ui.button(label="Restart (stop)", style=discord.ButtonStyle.danger, emoji="🟧")
    async def restart_btn(self, interaction: discord.Interaction, _):
        # NOTE: app_commands.checks decorators only apply to slash commands, not
        # component callbacks, so the permission check has to happen here directly.
        if await deny_if_not_manager(interaction):
            return
        await interaction.response.send_message(
            "Are you sure you want to **restart** the server?\nThis will run `save-all` and `stop`.",
            view=ConfirmStopView(self.cog, self.guild_id, restart_hint=True),
            ephemeral=True,
        )


# =========================
# Main Cog
# =========================
class MinecraftCog(commands.Cog):
    """Minecraft RCON control panel (Java/Bedrock status + rich RCON command runner)."""

    # Self-hosted (assets/grass_block_icon.png) — the raw vanilla texture is an
    # untinted grayscale mask, so this is a pre-tinted, upscaled copy of it.
    GRASS_ICON = "https://raw.githubusercontent.com/ethanocurtis/Utilabot-cog/main/assets/grass_block_icon.png"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.configs: Dict[str, ServerConfig] = load_all_configs()
        if _migrate_legacy_secrets(self.configs):
            save_all_configs(self.configs)
            log.info("Migrated legacy plaintext RCON passwords to encrypted storage.")
        self._sticky_next: Dict[str, float] = {}
        self._last_players: Dict[str, set] = {}
        self._stats = StatsStore(STATS_DB_PATH)
        self._last_prune = 0.0
        self._sticky_updater.start()
        self._stats_sampler.start()

    def cog_unload(self):
        for loop_ in (self._sticky_updater, self._stats_sampler):
            try:
                loop_.cancel()
            except Exception:
                log.exception("Failed to cancel background loop on unload.")

    # ---------- helpers ----------
    async def build_status_embed(
        self, guild_id: Optional[int], *, simple: bool = False
    ) -> Tuple[discord.Embed, Optional[discord.File]]:
        gid = str(guild_id) if guild_id else "global"
        cfg = self.configs.get(gid)
        display_name = (cfg.display_name if cfg and cfg.display_name else "Minecraft Server")

        embed = discord.Embed(title=f"{display_name} Status", color=discord.Color.blurple())
        embed.set_author(name=display_name, icon_url=self.GRASS_ICON)
        embed.timestamp = discord.utils.utcnow()

        if not cfg:
            embed.description = "Not configured yet. Use `/mc setup`."
            embed.set_thumbnail(url=self.GRASS_ICON)
            return embed, None

        ok, data = await mc_status(cfg.kind, cfg.host, cfg.port)
        show_port = effective_port(cfg.kind, cfg.port)

        # Hide default 25565 for Java; keep 19132 for Bedrock unless non-default
        if cfg.kind == "java" and show_port == 25565:
            display_addr = f"{cfg.host}"
        elif cfg.kind == "bedrock" and show_port == 19132:
            display_addr = f"{cfg.host}"  # also hide Bedrock default for consistency
        else:
            display_addr = f"{cfg.host}:{show_port}"

        embed.color = discord.Color.green() if ok else discord.Color.red()

        if ok:
            embed.description = f"🟢 **Online** — `{display_addr}`"

            if not simple:
                motd = data.get("motd") or "—"
                embed.add_field(name="MOTD", value=f"```{motd}```", inline=False)

            version = data.get("version") or "—"
            players = f"{data.get('players_online', 0)}/{data.get('players_max','?')}"
            embed.add_field(name="Version", value=version, inline=True)
            embed.add_field(name="Players", value=players, inline=True)

            # Names list
            sample = data.get("sample") or []
            if not sample:
                sample = await rcon_player_names_if_available(cfg)
            if sample:
                shown = "\n".join(f"• {n}" for n in sample[:20])
                more = f"\n… (+{len(sample)-20} more)" if len(sample) > 20 else ""
                embed.add_field(name="Who’s Online", value=shown + more, inline=False)

            file = await render_status_banner(
                online=True, host=display_addr, version=data.get("version"),
                players=data.get("players_online"), max_players=data.get("players_max"),
            )
        else:
            embed.description = f"🔴 **Offline / Unreachable** — `{display_addr}`"
            embed.add_field(name="Error", value=f"```{data.get('error','unknown error')}```", inline=False)
            file = await render_status_banner(online=False, host=display_addr)

        if file:
            embed.set_image(url=f"attachment://{file.filename}")
        else:
            embed.set_thumbnail(url=self.GRASS_ICON)

        hints: List[str] = []
        hints.append("🔧 RCON ready" if rcon_ready(cfg) else "🔧 RCON not set")
        if cfg.sticky_channel_id and cfg.sticky_interval_min:
            hints.append(f"🔁 every {cfg.sticky_interval_min}m")
        if cfg.player_log_channel_id:
            hints.append("📋 join/leave log on")
        embed.set_footer(text=" • ".join(hints))
        return embed, file

    async def _refresh_sticky_for_guild(self, gid: str) -> None:
        cfg = self.configs.get(gid)
        if not cfg or not cfg.sticky_channel_id or not cfg.sticky_interval_min:
            return

        guild = self.bot.get_guild(int(gid))
        if not guild:
            return

        channel = guild.get_channel(cfg.sticky_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            cfg.sticky_channel_id = None
            cfg.sticky_message_id = None
            save_all_configs(self.configs)
            return

        embed, file = await self.build_status_embed(guild.id, simple=True)

        msg = None
        if cfg.sticky_message_id:
            try:
                msg = await channel.fetch_message(cfg.sticky_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                msg = None

        if msg:
            try:
                # attachments=[] clears the old banner if this refresh has none (e.g. render failed);
                # passing a fresh File each time is required since a File's buffer is single-use.
                await msg.edit(content="‎", embed=embed, attachments=[file] if file else [])  # zero-width char
            except discord.HTTPException:
                msg = None

        if not msg:
            try:
                sent = await channel.send(embed=embed, file=file) if file else await channel.send(embed=embed)
                cfg.sticky_message_id = sent.id
                save_all_configs(self.configs)
            except discord.HTTPException:
                return

    async def _post_player_log(self, gid: str, cfg: ServerConfig, joined: List[str], left: List[str]) -> None:
        if not cfg.player_log_channel_id or not (joined or left):
            return
        guild = self.bot.get_guild(int(gid))
        if not guild:
            return
        channel = guild.get_channel(cfg.player_log_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        lines = [f"🟢 **{n}** joined" for n in joined] + [f"🔴 **{n}** left" for n in left]
        try:
            await channel.send("\n".join(lines))
        except discord.HTTPException:
            log.exception("Failed to post player join/leave log for guild %s", gid)

    # ---------- background updaters ----------
    @tasks.loop(seconds=30)
    async def _sticky_updater(self):
        now = time.time()
        for gid, cfg in list(self.configs.items()):
            if not (cfg.sticky_channel_id and cfg.sticky_interval_min and cfg.sticky_interval_min >= 1):
                continue
            due = self._sticky_next.get(gid, 0)
            if now >= due:
                self._sticky_next[gid] = now + (cfg.sticky_interval_min * 60)
                try:
                    await self._refresh_sticky_for_guild(gid)
                except Exception:
                    log.exception("Sticky refresh failed for guild %s", gid)

    @_sticky_updater.before_loop
    async def _before_sticky(self):
        await self.bot.wait_until_ready()
        for gid, cfg in self.configs.items():
            if cfg.sticky_channel_id and cfg.sticky_interval_min:
                self._sticky_next[gid] = time.time() + 10

    @tasks.loop(minutes=5)
    async def _stats_sampler(self):
        now = int(time.time())
        for gid, cfg in list(self.configs.items()):
            if not cfg.host:
                continue
            try:
                ok, data = await mc_status(cfg.kind, cfg.host, cfg.port)
                players = int(data.get("players_online") or 0) if ok else 0
                max_players = int(data.get("players_max") or 0) if ok else 0
                await self._stats.record_sample(gid, now, ok, players, max_players)

                if ok and cfg.kind == "java":
                    names = data.get("sample") or []
                    if not names and rcon_ready(cfg):
                        names = await rcon_player_names_if_available(cfg)
                    current = set(names)
                    previous = self._last_players.get(gid)
                    if previous is not None:
                        joined = sorted(current - previous)
                        left = sorted(previous - current)
                        if joined or left:
                            await self._post_player_log(gid, cfg, joined, left)
                            for p in joined:
                                await self._stats.record_event(gid, now, p, "join")
                            for p in left:
                                await self._stats.record_event(gid, now, p, "leave")
                    self._last_players[gid] = current
            except Exception:
                log.exception("Stats sampler failed for guild %s", gid)

        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                await self._stats.prune()
            except Exception:
                log.exception("Stats prune failed")

    @_stats_sampler.before_loop
    async def _before_stats(self):
        await self.bot.wait_until_ready()

    # ---------- slash commands ----------
    group = app_commands.Group(name="mc", description="Minecraft server controls", guild_only=True)

    @group.command(name="setup", description="Configure server connection (RCON password is entered via a private popup)")
    @app_commands.describe(
        kind="Minecraft edition: java or bedrock",
        host="Server host or domain",
        port="Server port (optional; defaults 25565 Java / 19132 Bedrock)",
        rcon_host="RCON host (Java only)",
        rcon_port="RCON port (Java only)",
        name="Custom name shown in the status embed (e.g. \"Ethan's SMP\"); leave blank to keep/use the default",
    )
    @app_commands.choices(kind=[
        app_commands.Choice(name="java", value="java"),
        app_commands.Choice(name="bedrock", value="bedrock")
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        host: str,
        port: Optional[int] = None,
        rcon_host: Optional[str] = None,
        rcon_port: Optional[int] = None,
        name: Optional[app_commands.Range[str, 1, 60]] = None,
    ):
        if port is None:
            port = default_port_for(kind.value)

        gid = str(interaction.guild_id)
        existing = self.configs.get(gid)
        display_name = name if name is not None else (existing.display_name if existing else None)

        if rcon_host and rcon_port:
            # Collect the password via modal so it never appears in the
            # publicly-visible slash-command invocation line.
            return await interaction.response.send_modal(
                RconSetupModal(self, gid, kind.value, host, port, rcon_host, rcon_port, existing, display_name)
            )

        cfg = ServerConfig(
            kind=kind.value,
            host=host,
            port=port,
            sticky_channel_id=existing.sticky_channel_id if existing else None,
            sticky_message_id=existing.sticky_message_id if existing else None,
            sticky_interval_min=existing.sticky_interval_min if existing else None,
            player_log_channel_id=existing.player_log_channel_id if existing else None,
            display_name=display_name,
        )
        self.configs[gid] = cfg
        save_all_configs(self.configs)
        await interaction.response.send_message(
            "✅ Minecraft configuration saved for this server.\n"
            f"- Kind: **{kind.value}**\n"
            f"- Address: `{host}:{port}`\n"
            "- RCON: not set",
            ephemeral=True,
        )

    @group.command(name="rename", description="Set (or clear) the custom name shown in the status embed")
    @app_commands.describe(name="New display name (e.g. \"Ethan's SMP\"); omit to reset to the default")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rename(self, interaction: discord.Interaction, name: Optional[app_commands.Range[str, 1, 60]] = None):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg:
            return await interaction.response.send_message("⚠️ Not configured yet. Run `/mc setup` first.", ephemeral=True)
        cfg.display_name = name
        save_all_configs(self.configs)
        if name:
            await interaction.response.send_message(f"✅ Display name set to **{name}**.", ephemeral=True)
        else:
            await interaction.response.send_message("✅ Display name reset to the default (\"Minecraft Server\").", ephemeral=True)

    @group.command(name="status", description="Show live status and open the RCON panel")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed, file = await self.build_status_embed(interaction.guild_id)
        kwargs = {"file": file} if file else {}
        await interaction.followup.send(embed=embed, view=PanelView(self, interaction.guild_id), **kwargs)

    @group.command(name="panel", description="Open the interactive RCON panel")
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed, file = await self.build_status_embed(interaction.guild_id)
        kwargs = {"file": file} if file else {}
        await interaction.followup.send(embed=embed, view=PanelView(self, interaction.guild_id), **kwargs)

    @group.command(name="config", description="Show the current Minecraft configuration for this server")
    async def show_config(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg:
            return await interaction.response.send_message("Not configured. Use `/mc setup`.", ephemeral=True)

        show_port = effective_port(cfg.kind, cfg.port)
        lines = [
            f"**Display name:** {cfg.display_name or 'Minecraft Server (default)'}",
            f"**Kind:** {cfg.kind}",
            f"**Address:** `{cfg.host}:{show_port}`",
            f"**RCON:** {'configured 🔒' if rcon_ready(cfg) else 'not set ⚠️'}",
            f"**Sticky status:** {(f'<#{cfg.sticky_channel_id}> every {cfg.sticky_interval_min}m' if cfg.sticky_channel_id and cfg.sticky_interval_min else 'disabled')}",
            f"**Join/leave log:** {(f'<#{cfg.player_log_channel_id}>' if cfg.player_log_channel_id else 'disabled')}",
        ]
        embed = discord.Embed(title="Minecraft Configuration", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="remove", description="Delete this server's Minecraft configuration")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        if gid not in self.configs:
            return await interaction.response.send_message("Nothing configured.", ephemeral=True)
        await interaction.response.send_message(
            "⚠️ This will delete the host/port/RCON/sticky/join-leave configuration for this server. Confirm?",
            view=ConfirmRemoveView(self, interaction.guild_id),
            ephemeral=True,
        )

    @group.command(name="restart", description="Soft restart via RCON (save-all + stop). Requires external auto-restart.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_restart(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg or cfg.kind != "java":
            return await interaction.response.send_message("⚠️ Restart is available for **Java** servers with RCON configured.", ephemeral=True)
        if not rcon_ready(cfg):
            return await interaction.response.send_message("⚠️ RCON not configured. Set it in `/mc setup`.", ephemeral=True)

        host, port, password = rcon_creds(cfg)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await rcon_stop_sequence(host, port, password)
        if ok:
            await interaction.followup.send(
                "✅ Restart sequence issued (server stopped).\n"
                "ℹ️ Make sure Docker/systemd/Pterodactyl auto-restarts the server process.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"❌ Restart failed: `{msg}`", ephemeral=True)

    @group.command(name="ping", description="Quick connectivity check")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg:
            return await interaction.followup.send("Not configured. Use `/mc setup`.", ephemeral=True)
        ok, data = await mc_status(cfg.kind, cfg.host, cfg.port)
        await interaction.followup.send(("✅ Reachable." if ok else f"❌ Unreachable: `{data.get('error','unknown error')}`"), ephemeral=True)

    @group.command(name="history", description="Show recent server stats: uptime, peak players, and activity")
    async def history(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg:
            return await interaction.response.send_message("Not configured. Use `/mc setup`.", ephemeral=True)

        await interaction.response.defer()
        day = await self._stats.summary(gid, 24)
        if not day:
            return await interaction.followup.send(
                "📊 No stats recorded yet — samples are taken every 5 minutes, check back shortly."
            )
        week = await self._stats.summary(gid, 24 * 7)
        buckets = await self._stats.hourly_peaks(gid, 24)

        embed = discord.Embed(title="📊 Minecraft Server Stats", color=discord.Color.blurple())
        embed.add_field(name="Last 24h", value=f"Uptime: **{day['uptime_pct']:.0f}%**\nPeak players: **{day['peak']}**", inline=True)
        if week:
            embed.add_field(name="Last 7d", value=f"Uptime: **{week['uptime_pct']:.0f}%**\nPeak players: **{week['peak']}**", inline=True)
        embed.add_field(name="Players online (last 24h, hourly peak)", value=f"`{sparkline(buckets)}`", inline=False)
        embed.set_footer(text="Sampled every 5 minutes")
        await interaction.followup.send(embed=embed)

    # ---------- Sticky controls ----------
    @group.command(name="sticky_set", description="Enable/Update the sticky status in a channel (auto-updating embed).")
    @app_commands.describe(
        channel="Channel to pin the live status message in",
        interval_minutes="Update interval in minutes (min 1)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sticky_set(self, interaction: discord.Interaction, channel: discord.TextChannel, interval_minutes: app_commands.Range[int, 1, 1440]):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg:
            return await interaction.response.send_message("⚠️ Not configured yet. Run `/mc setup` first.", ephemeral=True)

        cfg.sticky_channel_id = channel.id
        cfg.sticky_interval_min = int(interval_minutes)
        save_all_configs(self.configs)
        self._sticky_next[gid] = time.time() + 1
        await interaction.response.send_message(f"✅ Sticky status set to {channel.mention} every **{interval_minutes}m**.", ephemeral=True)

    @group.command(name="sticky_disable", description="Disable the sticky status message.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sticky_disable(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg or not cfg.sticky_channel_id:
            return await interaction.response.send_message("Sticky status is not enabled.", ephemeral=True)

        cfg.sticky_channel_id = None
        cfg.sticky_message_id = None
        cfg.sticky_interval_min = None
        save_all_configs(self.configs)
        self._sticky_next.pop(gid, None)
        await interaction.response.send_message("✅ Sticky status disabled.", ephemeral=True)

    @group.command(name="sticky_now", description="Force an immediate sticky status refresh.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sticky_now(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg or not cfg.sticky_channel_id:
            return await interaction.response.send_message("Sticky status is not enabled. Use `/mc sticky_set`.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._refresh_sticky_for_guild(gid)
            await interaction.followup.send("✅ Sticky status updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Update failed: `{e}`", ephemeral=True)

    # ---------- Join/leave log controls ----------
    @group.command(name="playerlog_set", description="Post join/leave notifications to a channel (Java only).")
    @app_commands.describe(channel="Channel to post player join/leave notifications in")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def playerlog_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg:
            return await interaction.response.send_message("⚠️ Not configured yet. Run `/mc setup` first.", ephemeral=True)

        cfg.player_log_channel_id = channel.id
        save_all_configs(self.configs)
        note = "\n⚠️ Bedrock servers don't expose player names, so only Java servers get join/leave events." if cfg.kind == "bedrock" else ""
        await interaction.response.send_message(f"✅ Join/leave notifications will post in {channel.mention}.{note}", ephemeral=True)

    @group.command(name="playerlog_disable", description="Disable join/leave notifications.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def playerlog_disable(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg or not cfg.player_log_channel_id:
            return await interaction.response.send_message("Join/leave notifications are not enabled.", ephemeral=True)

        cfg.player_log_channel_id = None
        save_all_configs(self.configs)
        await interaction.response.send_message("✅ Join/leave notifications disabled.", ephemeral=True)


class ConfirmRemoveView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, timeout: int = 20):
        super().__init__(timeout=timeout)
        self.cog = cog; self.guild_id = guild_id

    @discord.ui.button(label="Delete config", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, _):
        if await deny_if_not_manager(interaction):
            return
        gid = str(self.guild_id)
        self.cog.configs.pop(gid, None)
        save_all_configs(self.cog.configs)
        self.cog._sticky_next.pop(gid, None)
        self.cog._last_players.pop(gid, None)
        await interaction.response.send_message("✅ Configuration removed for this server.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _):
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(MinecraftCog(bot))
