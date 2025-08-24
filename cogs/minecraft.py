# cogs/minecraft.py
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# ---- external deps (install: pip install mcstatus mcrcon) ----
from mcstatus import JavaServer, BedrockServer
from mcrcon import MCRcon

CONFIG_PATH = "data/minecraft_config.json"
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)


# =========================
# Data / persistence layer
# =========================
@dataclass
class ServerConfig:
    kind: str              # "java" or "bedrock"
    host: str
    port: int
    rcon_host: Optional[str] = None
    rcon_port: Optional[int] = None
    rcon_password: Optional[str] = None
    properties_path: Optional[str] = None   # path to server.properties (optional)


def load_all_configs() -> Dict[str, ServerConfig]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, ServerConfig] = {}
    for gid, obj in raw.items():
        out[gid] = ServerConfig(**obj)
    return out


def save_all_configs(cfgs: Dict[str, ServerConfig]) -> None:
    raw = {gid: asdict(cfg) for gid, cfg in cfgs.items()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


# =========================
# MC utilities
# =========================
async def mc_status(kind: str, host: str, port: int) -> Tuple[bool, Dict]:
    """
    Returns (ok, data). For Java/Bedrock, grabs status details.
    """
    loop = asyncio.get_running_loop()
    data: Dict = {}
    try:
        if kind == "java":
            server = await loop.run_in_executor(None, lambda: JavaServer(host, port))
            status = await loop.run_in_executor(None, server.status)
            data = {
                "motd": getattr(status.description, "to_plain", lambda: str(status.description))(),
                "version": getattr(status.version, "name", str(status.version)),
                "latency": getattr(status, "latency", None),
                "players_online": status.players.online,
                "players_max": status.players.max,
                "sample": [p.name for p in (status.players.sample or [])],
            }
            return True, data

        elif kind == "bedrock":
            server = BedrockServer(host, port)
            status = await loop.run_in_executor(None, server.status)
            data = {
                "motd": status.motd,
                "version": status.version.brand + " " + status.version.version,
                "latency": None,
                "players_online": status.players_online,
                "players_max": status.players_max,
                "sample": [],
            }
            return True, data

        else:
            return False, {"error": "Unknown server kind."}

    except Exception as e:
        return False, {"error": str(e)}


async def rcon_exec(host: str, port: int, password: str, command: str) -> Tuple[bool, str]:
    """
    Execute a command via RCON.
    Most commands are fine to run directly; mcrcon's signal handler breaks in threads.
    """
    try:
        # Run sync in the main async loop (fast enough for short RCON ops)
        with MCRcon(host, password, port=port) as rcon:
            resp = rcon.command(command)
            return True, resp or "(no response)"
    except Exception as e:
        return False, str(e)

    def run() -> Tuple[bool, str]:
        try:
            with MCRcon(host, password, port=port) as rcon:
                resp = rcon.command(command)
                return True, resp or "(no response)"
        except Exception as e:
            return False, str(e)

    return await loop.run_in_executor(None, run)


async def rcon_stop_sequence(host: str, port: int, password: str, *, warn_secs: int = 5) -> Tuple[bool, str]:
    """Graceful stop (announce → save-all flush → stop)."""
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


def read_properties(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def write_properties(path: str, lines: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def set_property_line(lines: List[str], key: str, value: str) -> Tuple[List[str], bool]:
    """
    Replace or append 'key=value' in a typical server.properties file.
    Preserves comments and unrelated lines. Returns (new_lines, replaced).
    """
    key_eq = f"{key}="
    replaced = False
    new_lines: List[str] = []
    for line in lines:
        if line.strip().startswith("#"):
            new_lines.append(line)
            continue
        if line.startswith(key_eq):
            new_lines.append(f"{key}={value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{key}={value}")
    return new_lines, replaced


# =========================
# Fancy UI components
# =========================
class SettingsSelect(discord.ui.Select):
    """Dropdown to choose which setting to change."""
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        self.cog = cog
        self.guild_id = guild_id
        options = [
            # Instant (RCON)
            discord.SelectOption(label="Difficulty", description="Peaceful/Easy/Normal/Hard (instant)", value="difficulty", emoji="⚔️"),
            discord.SelectOption(label="Default Gamemode", description="Survival/Creative/Adventure/Spectator (instant)", value="defaultgamemode", emoji="🎮"),
            discord.SelectOption(label="Whitelist ON/OFF", description="Toggle whitelist (instant)", value="whitelist", emoji="✅"),
            # Properties (restart)
            discord.SelectOption(label="view-distance", description="2–32 (restart required)", value="view-distance", emoji="🗺️"),
            discord.SelectOption(label="max-players", description="1–200 (restart required)", value="max-players", emoji="👥"),
            discord.SelectOption(label="allow-flight", description="true/false (restart required)", value="allow-flight", emoji="🪽"),
        ]
        super().__init__(placeholder="Choose a setting to change…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg:
            return await interaction.response.send_message("⚠️ This server isn't configured. Run `/mc setup` first.", ephemeral=True)

        if value == "difficulty":
            return await interaction.response.send_message("Pick a difficulty:", view=DifficultyView(self.cog, self.guild_id), ephemeral=True)

        if value == "defaultgamemode":
            return await interaction.response.send_message("Pick a default gamemode:", view=GamemodeView(self.cog, self.guild_id), ephemeral=True)

        if value == "whitelist":
            return await interaction.response.send_message("Toggle whitelist:", view=WhitelistToggleView(self.cog, self.guild_id), ephemeral=True)

        if not cfg.properties_path:
            return await interaction.response.send_message(
                "⚠️ No `server.properties` path set in `/mc setup`.\nRe-run setup with `properties_path` to enable property editing.",
                ephemeral=True,
            )

        if value == "view-distance":
            return await interaction.response.send_modal(PropertyNumberModal(self.cog, self.guild_id, "view-distance", 2, 32, title="Set view-distance (2–32)"))
        if value == "max-players":
            return await interaction.response.send_modal(PropertyNumberModal(self.cog, self.guild_id, "max-players", 1, 200, title="Set max-players (1–200)"))
        if value == "allow-flight":
            return await interaction.response.send_modal(PropertyBooleanModal(self.cog, self.guild_id, "allow-flight", title="Set allow-flight (true/false)"))


class DifficultyView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, timeout: Optional[float] = 60):
        super().__init__(timeout=timeout); self.cog = cog; self.guild_id = guild_id

    @discord.ui.button(label="Peaceful", style=discord.ButtonStyle.secondary, emoji="🕊️")
    async def peaceful(self, interaction: discord.Interaction, _): await self._set(interaction, "peaceful")
    @discord.ui.button(label="Easy", style=discord.ButtonStyle.secondary, emoji="🙂")
    async def easy(self, interaction: discord.Interaction, _): await self._set(interaction, "easy")
    @discord.ui.button(label="Normal", style=discord.ButtonStyle.primary, emoji="😎")
    async def normal(self, interaction: discord.Interaction, _): await self._set(interaction, "normal")
    @discord.ui.button(label="Hard", style=discord.ButtonStyle.danger, emoji="🔥")
    async def hard(self, interaction: discord.Interaction, _): await self._set(interaction, "hard")

    async def _set(self, interaction: discord.Interaction, value: str):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or not cfg.rcon_host or not cfg.rcon_port or not cfg.rcon_password:
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, f"difficulty {value}")
        await interaction.followup.send((f"✅ Difficulty set to **{value}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)


class GamemodeView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, timeout: Optional[float] = 60):
        super().__init__(timeout=timeout); self.cog = cog; self.guild_id = guild_id

    @discord.ui.button(label="Survival", style=discord.ButtonStyle.secondary, emoji="⛏️")
    async def survival(self, interaction: discord.Interaction, _): await self._set(interaction, "survival")
    @discord.ui.button(label="Creative", style=discord.ButtonStyle.primary, emoji="📦")
    async def creative(self, interaction: discord.Interaction, _): await self._set(interaction, "creative")
    @discord.ui.button(label="Adventure", style=discord.ButtonStyle.secondary, emoji="🧭")
    async def adventure(self, interaction: discord.Interaction, _): await self._set(interaction, "adventure")
    @discord.ui.button(label="Spectator", style=discord.ButtonStyle.secondary, emoji="👁️")
    async def spectator(self, interaction: discord.Interaction, _): await self._set(interaction, "spectator")

    async def _set(self, interaction: discord.Interaction, value: str):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or not cfg.rcon_host or not cfg.rcon_port or not cfg.rcon_password:
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, f"defaultgamemode {value}")
        await interaction.followup.send((f"✅ Default gamemode set to **{value}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)


class WhitelistToggleView(discord.ui.View):
    """Simple ON/OFF toggle for whitelist."""
    def __init__(self, cog: "MinecraftCog", guild_id: int, timeout: Optional[float] = 60):
        super().__init__(timeout=timeout); self.cog = cog; self.guild_id = guild_id

    @discord.ui.button(label="Whitelist ON", style=discord.ButtonStyle.success, emoji="✅")
    async def wl_on(self, interaction: discord.Interaction, _): await self._toggle(interaction, True)

    @discord.ui.button(label="Whitelist OFF", style=discord.ButtonStyle.danger, emoji="❌")
    async def wl_off(self, interaction: discord.Interaction, _): await self._toggle(interaction, False)

    async def _toggle(self, interaction: discord.Interaction, turn_on: bool):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not cfg.rcon_host or not cfg.rcon_port or not cfg.rcon_password:
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        cmd = "whitelist on" if turn_on else "whitelist off"
        ok, resp = await rcon_exec(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, cmd)
        msg = f"✅ Whitelist **{'ON' if turn_on else 'OFF'}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"
        await interaction.followup.send(msg, ephemeral=True)


# ---------- New: Whitelist Manager (Add / Remove / List) ----------
class WhitelistAddModal(discord.ui.Modal):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(title="Add player to whitelist")
        self.cog = cog; self.guild_id = guild_id
        self.name = discord.ui.TextInput(label="Java Username", placeholder="e.g., Notch", required=True, min_length=2, max_length=16)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ Java + RCON required.", ephemeral=True)
        username = str(self.name.value).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, f"whitelist add {username}")
        msg = f"✅ **{username}** added to whitelist.\n```{resp}```" if ok else f"❌ Failed to add: `{resp}`"
        await interaction.followup.send(msg, ephemeral=True)


class WhitelistRemoveModal(discord.ui.Modal):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(title="Remove player from whitelist")
        self.cog = cog; self.guild_id = guild_id
        self.name = discord.ui.TextInput(label="Java Username", placeholder="e.g., Notch", required=True, min_length=2, max_length=16)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ Java + RCON required.", ephemeral=True)
        username = str(self.name.value).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, f"whitelist remove {username}")
        msg = f"✅ **{username}** removed from whitelist.\n```{resp}```" if ok else f"❌ Failed to remove: `{resp}`"
        await interaction.followup.send(msg, ephemeral=True)


class WhitelistManagerView(discord.ui.View):
    """Manager with Add / Remove / List actions."""
    def __init__(self, cog: "MinecraftCog", guild_id: int, timeout: Optional[int] = 90):
        super().__init__(timeout=timeout)
        self.cog = cog; self.guild_id = guild_id

    @discord.ui.button(label="Add Player", style=discord.ButtonStyle.success, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(WhitelistAddModal(self.cog, self.guild_id))

    @discord.ui.button(label="Remove Player", style=discord.ButtonStyle.danger, emoji="➖")
    async def remove_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(WhitelistRemoveModal(self.cog, self.guild_id))

    @discord.ui.button(label="Show List", style=discord.ButtonStyle.secondary, emoji="📜")
    async def list_btn(self, interaction: discord.Interaction, _):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ Java + RCON required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password, "whitelist list")
        if not ok:
            return await interaction.followup.send(f"❌ Failed to list: `{resp}`", ephemeral=True)

        # Try to extract names from the server response
        text = resp.strip()
        # Common responses look like: "There are X whitelisted players: name1, name2, name3"
        names: List[str] = []
        if ":" in text:
            names_part = text.split(":", 1)[1].strip()
            if names_part:
                names = [n.strip() for n in names_part.split(",") if n.strip()]
        if not names and text:
            # Fallback, try whitespace split if server uses different format
            names = [n.strip(",") for n in text.split() if n and n.lower() not in {"there", "are", "whitelisted", "players"}]

        if not names:
            return await interaction.followup.send("ℹ️ Whitelist appears to be **empty**.", ephemeral=True)

        # Chunk into 20 per field to keep it readable
        chunks = [names[i:i+20] for i in range(0, len(names), 20)]
        embed = discord.Embed(title="Whitelisted Players", color=discord.Color.green())
        for idx, chunk in enumerate(chunks, 1):
            embed.add_field(name=f"Page {idx}", value=", ".join(chunk), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


class ConfirmStopView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, *, restart_hint: bool = False, timeout: int = 20):
        super().__init__(timeout=timeout)
        self.cog = cog; self.guild_id = guild_id; self.restart_hint = restart_hint

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="🛑")
    async def confirm(self, interaction: discord.Interaction, _):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not cfg.rcon_host or not cfg.rcon_port or not cfg.rcon_password:
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_stop_sequence(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
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


class PanelView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, *, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.cog = cog; self.guild_id = guild_id
        self.add_item(SettingsSelect(cog, guild_id))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await self.cog.build_status_embed(interaction.guild_id)
        await interaction.followup.send("Updated status:", embed=embed, ephemeral=True)

    @discord.ui.button(label="Copy IP", style=discord.ButtonStyle.secondary, emoji="📋")
    async def copy_ip(self, interaction: discord.Interaction, _):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg:
            return await interaction.response.send_message("No config found.", ephemeral=True)
        await interaction.response.send_message(f"`{cfg.host}:{cfg.port}` (for **{cfg.kind.upper()}**)", ephemeral=True)

    @discord.ui.button(label="Whitelist", style=discord.ButtonStyle.secondary, emoji="👤")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def wl_manage(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(
            "Whitelist manager:", view=WhitelistManagerView(self.cog, self.guild_id), ephemeral=True
        )

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.danger, emoji="🟧")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def restart_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(
            "Are you sure you want to **restart** the server?\nThis will run `save-all` and `stop`.",
            view=ConfirmStopView(self.cog, self.guild_id, restart_hint=True),
            ephemeral=True,
        )

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="🟥")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def stop_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(
            "Are you sure you want to **stop** the server?\nThis will run `save-all` and `stop`.",
            view=ConfirmStopView(self.cog, self.guild_id, restart_hint=False),
            ephemeral=True,
        )


# =========================
# Main Cog
# =========================
class MinecraftCog(commands.Cog):
    """Interactive Minecraft status & control panel (Java/Bedrock + RCON + properties)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.configs: Dict[str, ServerConfig] = load_all_configs()

    # ---------- helpers ----------
    async def build_status_embed(self, guild_id: Optional[int]) -> discord.Embed:
        gid = str(guild_id) if guild_id else "global"
        cfg = self.configs.get(gid)

        embed = discord.Embed(
            title="🟩 Minecraft Server Panel" if cfg else "Minecraft Server Panel",
            description="Live status • controls • quick actions",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url="https://static.wikia.nocookie.net/minecraft_gamepedia/images/5/51/Grass_Block_JE2_BE2.png")

        if not cfg:
            embed.description = "Not configured yet. Use `/mc setup`."
            return embed

        ok, data = await mc_status(cfg.kind, cfg.host, cfg.port)
        if ok:
            status_line = f"**Online** `{cfg.host}:{cfg.port}`"
            embed.add_field(name="Status", value=status_line, inline=False)

            motd = data.get("motd") or "—"
            version = data.get("version") or "—"
            latency = data.get("latency")
            ping = f"{round(latency)} ms" if latency is not None else "—"
            players = f"{data.get('players_online', 0)}/{data.get('players_max','?')}"

            embed.add_field(name="MOTD", value=f"```{motd}```", inline=False)
            embed.add_field(name="Version", value=version, inline=True)
            embed.add_field(name="Ping", value=ping, inline=True)
            embed.add_field(name="Players", value=players, inline=True)

            sample = data.get("sample") or []
            if sample:
                shown = ", ".join(sample[:20])
                more = f" … (+{len(sample)-20} more)" if len(sample) > 20 else ""
                embed.add_field(name="Who’s Online", value=shown + more, inline=False)
        else:
            embed.add_field(
                name="Status",
                value=f"**Offline / Unreachable** `{cfg.host}:{cfg.port}`\n```{data.get('error','unknown error')}```",
                inline=False,
            )

        hints: List[str] = []
        hints.append("RCON ready" if (cfg.rcon_host and cfg.rcon_port) else "RCON not set")
        hints.append("properties editing enabled" if cfg.properties_path else "properties editing not set")
        embed.set_footer(text=" • ".join(hints))
        return embed

    # ---------- slash commands ----------
    group = app_commands.Group(name="mc", description="Minecraft server controls")

    @group.command(name="setup", description="Configure server connection + optional RCON + properties path")
    @app_commands.describe(
        kind="Minecraft edition: java or bedrock",
        host="Server host or IP (no scheme)",
        port="Server port",
        rcon_host="RCON host (optional, Java only)",
        rcon_port="RCON port (optional, Java only)",
        rcon_password="RCON password (optional, Java only)",
        properties_path="Full path to server.properties (optional)",
    )
    @app_commands.choices(kind=[app_commands.Choice(name="java", value="java"), app_commands.Choice(name="bedrock", value="bedrock")])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        host: str,
        port: int,
        rcon_host: Optional[str] = None,
        rcon_port: Optional[int] = None,
        rcon_password: Optional[str] = None,
        properties_path: Optional[str] = None,
    ):
        gid = str(interaction.guild_id)
        self.configs[gid] = ServerConfig(
            kind=kind.value,
            host=host,
            port=port,
            rcon_host=rcon_host,
            rcon_port=rcon_port,
            rcon_password=rcon_password,
            properties_path=properties_path,
        )
        save_all_configs(self.configs)
        await interaction.response.send_message(
            "✅ Minecraft configuration saved for this server.\n"
            f"- Kind: **{kind.value}**\n"
            f"- Address: `{host}:{port}`\n"
            f"- RCON: {'configured' if (rcon_host and rcon_port and rcon_password) else 'not set'}\n"
            f"- Properties: `{properties_path or 'not set'}`",
            ephemeral=True,
        )

    @group.command(name="status", description="Show live status and player info")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_status_embed(interaction.guild_id)
        await interaction.followup.send(embed=embed, view=PanelView(self, interaction.guild_id))

    @group.command(name="panel", description="Open interactive controls (dropdown + buttons)")
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_status_embed(interaction.guild_id)
        await interaction.followup.send(embed=embed, view=PanelView(self, interaction.guild_id))

    # ---------- RCON-only Stop / Restart ----------
    @group.command(name="stop", description="Gracefully stop the Java server via RCON (save-all + stop)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_stop(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg or cfg.kind != "java":
            return await interaction.response.send_message("⚠️ Stop is available for **Java** servers with RCON configured.", ephemeral=True)
        if not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured. Set it in `/mc setup`.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await rcon_stop_sequence(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
        await interaction.followup.send("✅ Stop issued." if ok else f"❌ Stop failed: `{msg}`", ephemeral=True)

    @group.command(name="restart", description="Soft restart via RCON (save-all + stop). Requires external auto-restart.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_restart(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg or cfg.kind != "java":
            return await interaction.response.send_message("⚠️ Restart is available for **Java** servers with RCON configured.", ephemeral=True)
        if not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured. Set it in `/mc setup`.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await rcon_stop_sequence(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
        if ok:
            await interaction.followup.send(
                "✅ Restart sequence issued (server stopped).\n"
                "ℹ️ Make sure Docker/systemd/screen auto-restarts the server process.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"❌ Restart failed: `{msg}`", ephemeral=True)

    # Optional: simple ping test for debugging
    @group.command(name="ping", description="Quick connectivity check")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg:
            return await interaction.followup.send("Not configured. Use `/mc setup`.", ephemeral=True)
        ok, data = await mc_status(cfg.kind, cfg.host, cfg.port)
        await interaction.followup.send(("✅ Reachable." if ok else f"❌ Unreachable: `{data.get('error','unknown error')}`"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MinecraftCog(bot))
