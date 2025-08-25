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

# External deps (install: pip install mcstatus mcrcon)
from mcstatus import JavaServer, BedrockServer
from mcrcon import MCRcon

CONFIG_PATH = "data/minecraft_config.json"
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)


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
    rcon_password: Optional[str] = None
    # legacy (ignored in RCON-only mode; kept for compatibility so old JSON loads)
    properties_path: Optional[str] = None


def load_all_configs() -> Dict[str, ServerConfig]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, ServerConfig] = {}
    for gid, obj in raw.items():
        # drop unknown keys to be future-proof
        allowed = {"kind","host","port","rcon_host","rcon_port","rcon_password","properties_path"}
        slim = {k: v for k, v in obj.items() if k in allowed}
        out[gid] = ServerConfig(**slim)
    return out


def save_all_configs(cfgs: Dict[str, ServerConfig]) -> None:
    raw = {gid: asdict(cfg) for gid, cfg in cfgs.items()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


# =========================
# Helpers
# =========================
def default_port_for(kind: str) -> int:
    return 25565 if kind == "java" else 19132


def effective_port(kind: str, port: Optional[int]) -> int:
    return port if port is not None else default_port_for(kind)


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
                "latency": getattr(status, "latency", None),
                "players_online": status.players.online,
                "players_max": status.players.max,
                "sample": [p.name for p in (status.players.sample or [])],
            }
            return True, data

        elif kind == "bedrock":
            server = BedrockServer(host, p)
            status = await loop.run_in_executor(None, server.status)
            data = {
                "motd": status.motd,
                "version": status.version.brand + " " + status.version.version,
                "latency": None,
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
# RCON helpers (no thread)
# =========================
async def rcon_exec(host: str, port: int, password: str, command: str) -> Tuple[bool, str]:
    """
    Execute a command via RCON in the event loop thread
    (mcrcon's signal usage can fail in thread pools).
    """
    try:
        with MCRcon(host, password, port=port) as rcon:
            resp = rcon.command(command)
            return True, resp or "(no response)"
    except Exception as e:
        return False, str(e)


async def rcon_stop_sequence(host: str, port: int, password: str, *, warn_secs: int = 5) -> Tuple[bool, str]:
    """Graceful stop: announce → save-all flush → stop."""
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


# =========================
# Generic RCON command model
# =========================
class RconCmd:
    def __init__(self, key: str, label: str, template: str, fields: Optional[List[Dict]] = None, danger: bool = False):
        self.key = key            # unique id
        self.label = label        # dropdown label
        self.template = template  # template like "ban {name} {reason}"
        self.fields = fields or []  # [{"id","label","placeholder","min","max","required"}]
        self.danger = danger


RCON_COMMANDS: List[RconCmd] = [
    # Info / chat
    RconCmd("list", "Player list", "list"),
    RconCmd("say", "Broadcast message", "say {msg}", fields=[{"id": "msg", "label": "Message", "placeholder": "Server restarting soon!", "min":1, "max":200, "required": True}]),

    # Whitelist
    RconCmd("wl_on", "Whitelist ON", "whitelist on"),
    RconCmd("wl_off", "Whitelist OFF", "whitelist off"),
    RconCmd("wl_add", "Whitelist add <name>", "whitelist add {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),
    RconCmd("wl_remove", "Whitelist remove <name>", "whitelist remove {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),
    RconCmd("wl_list", "Whitelist list", "whitelist list"),

    # Permissions
    RconCmd("op", "OP <name>", "op {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),
    RconCmd("deop", "DEOP <name>", "deop {name}", fields=[{"id":"name","label":"Player name","placeholder":"Steve","min":2,"max":16,"required":True}]),

    # Moderation
    RconCmd("kick", "Kick <name> [reason]", "kick {name} {reason}", fields=[
        {"id":"name","label":"Player name","placeholder":"Griefer123","min":2,"max":16,"required":True},
        {"id":"reason","label":"Reason (optional)","placeholder":"Keep it short","min":0,"max":60,"required":False},
    ]),
    RconCmd("ban", "Ban <name> [reason]", "ban {name} {reason}", fields=[
        {"id":"name","label":"Player name","placeholder":"Griefer123","min":2,"max":16,"required":True},
        {"id":"reason","label":"Reason (optional)","placeholder":"Griefing","min":0,"max":60,"required":False},
    ]),
    RconCmd("pardon", "Pardon <name>", "pardon {name}", fields=[{"id":"name","label":"Player name","placeholder":"Friend","min":2,"max":16,"required":True}]),

    # Gameplay (handled by choice UIs)
    RconCmd("difficulty", "Difficulty (pick)", "difficulty {level}"),
    RconCmd("gamemode", "Default gamemode (pick)", "defaultgamemode {mode}"),
    RconCmd("weather", "Weather (pick)", "weather {kind}"),
    RconCmd("time", "Time set (pick)", "time set {value}"),
    RconCmd("gamerule_preset", "Gamerule presets (pick/toggle)", "gamerule {rule} {value}"),

    # Utility
    RconCmd("tp", "Teleport <target> <destination>", "tp {target} {dest}", fields=[
        {"id":"target","label":"Target selector or player","placeholder":"Player | @a | @p","min":1,"max":32,"required":True},
        {"id":"dest","label":"Destination","placeholder":"Player | x y z","min":1,"max":64,"required":True},
    ]),
    RconCmd("title", "Title all players", 'title @a title {"text":"{text}"}', fields=[{"id":"text","label":"Title text","placeholder":"Welcome!","min":1,"max":100,"required":True}]),
    RconCmd("save_flush", "Save-all flush", "save-all flush"),
    RconCmd("save_on", "Save-on", "save-on"),
    RconCmd("save_off", "Save-off", "save-off"),

    # Power
    RconCmd("stop", "STOP server (confirm)", "stop", danger=True),
]


# =========================
# UI: Pre-filled choice views
# =========================
class DifficultyChoiceView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        super().__init__(timeout=60); self.cog=cog; self.guild_id=guild_id

    async def _run(self, interaction: discord.Interaction, value: str):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, rp, cfg.rcon_password, f"difficulty {value}")
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
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, rp, cfg.rcon_password, f"defaultgamemode {value}")
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
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, rp, cfg.rcon_password, f"weather {kind}")
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
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)
        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, rp, cfg.rcon_password, f"time set {value}")
        await interaction.followup.send((f"✅ Time set to **{value}**.\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)

    @discord.ui.button(label="Day", style=discord.ButtonStyle.secondary, emoji="🌤️")
    async def day(self, i: discord.Interaction, _): await self._run(i, "day")
    @discord.ui.button(label="Noon", style=discord.ButtonStyle.secondary, emoji="🌞")
    async def noon(self, i: discord.Interaction, _): await self._run(i, "noon")
    @discord.ui.button(label="Night", style=discord.ButtonStyle.primary, emoji="🌙")
    async def night(self, i: discord.Interaction, _): await self._run(i, "night")
    @discord.ui.button(label="Midnight", style=discord.ButtonStyle.danger, emoji="🌌")
    async def midnight(self, i: discord.Interaction, _): await self._run(i, "midnight")


# A small list of popular gamerules to toggle quickly
GAMERULE_PRESETS = [
    ("keepInventory", ["true", "false"]),
    ("doDaylightCycle", ["true", "false"]),
    ("doWeatherCycle", ["true", "false"]),
    ("doMobGriefing", ["true", "false"]),
    ("doFireTick", ["true", "false"]),
    ("doImmediateRespawn", ["true", "false"]),
    ("showCoordinates", ["true", "false"]),  # harmless if not present
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
        self.view.selected_rule = self.values[0]
        await interaction.response.send_message(f"Rule selected: `{self.view.selected_rule}`. Now pick a value.", ephemeral=True)

class GameruleValueSelect(discord.ui.Select):
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        self.cog=cog; self.guild_id=guild_id
        options = [discord.SelectOption(label="true", value="true"), discord.SelectOption(label="false", value="false")]
        super().__init__(placeholder="Set value…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured.", ephemeral=True)

        rule = getattr(self.view, "selected_rule", None)
        if not rule:
            return await interaction.response.send_message("Pick a gamerule first.", ephemeral=True)

        rp = effective_port(cfg.kind, cfg.rcon_port)
        value = self.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, rp, cfg.rcon_password, f"gamerule {rule} {value}")
        await interaction.followup.send((f"✅ `gamerule {rule} {value}`\n```{resp}```" if ok else f"❌ Failed: `{resp}`"), ephemeral=True)


# =========================
# UI: Generic modal for commands with fields
# =========================
class CommandModal(discord.ui.Modal):
    def __init__(self, cog: "MinecraftCog", guild_id: int, cmd: RconCmd):
        super().__init__(title=f"Run: {cmd.label}")
        self.cog = cog
        self.guild_id = guild_id
        self.cmd = cmd
        self.inputs: Dict[str, discord.ui.TextInput] = {}
        for f in cmd.fields:
            ti = discord.ui.TextInput(
                label=f["label"],
                placeholder=f.get("placeholder", ""),
                required=f.get("required", True),
                min_length=f.get("min", None),
                max_length=f.get("max", None),
            )
            self.inputs[f["id"]] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)

        values = {fid: (str(inp.value).strip() if inp.value is not None else "") for fid, inp in self.inputs.items()}
        command = self.cmd.template.format(**values).strip()

        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, rp, cfg.rcon_password, command)
        msg = f"✅ Ran `{command}`.\n```{resp}```" if ok else f"❌ `{command}` failed:\n`{resp}`"
        await interaction.followup.send(msg, ephemeral=True)


# =========================
# Other views
# =========================
class ConfirmStopView(discord.ui.View):
    def __init__(self, cog: "MinecraftCog", guild_id: int, *, restart_hint: bool = False, timeout: int = 20):
        super().__init__(timeout=timeout)
        self.cog = cog; self.guild_id = guild_id; self.restart_hint = restart_hint

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="🛑")
    async def confirm(self, interaction: discord.Interaction, _):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)
        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_stop_sequence(cfg.rcon_host, rp, cfg.rcon_password)
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
    """Dropdown of RCON commands; opens a choice view or modal if needed."""
    def __init__(self, cog: "MinecraftCog", guild_id: int):
        self.cog = cog
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label=cmd.label, value=cmd.key, emoji=("❗" if cmd.danger else "🧰"))
            for cmd in RCON_COMMANDS
        ]
        super().__init__(placeholder="Run an RCON command…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cmd_key = self.values[0]
        cmd = next((c for c in RCON_COMMANDS if c.key == cmd_key), None)
        if not cmd:
            return await interaction.response.send_message("Unknown command.", ephemeral=True)

        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg or cfg.kind != "java" or not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ Java + RCON required and must be configured.", ephemeral=True)

        # Pre-filled choice UIs
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

        # Danger commands get a confirm
        if cmd.danger:
            return await interaction.response.send_message(
                "Are you sure you want to **stop** the server? (save-all + stop)",
                view=ConfirmStopView(self.cog, self.guild_id, restart_hint=True),
                ephemeral=True,
            )

        # Commands with fields → open modal
        if cmd.fields:
            return await interaction.response.send_modal(CommandModal(self.cog, self.guild_id, cmd))

        # No fields → run immediately
        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, resp = await rcon_exec(cfg.rcon_host, rp, cfg.rcon_password, cmd.template)
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
        embed = await self.cog.build_status_embed(interaction.guild_id)
        await interaction.followup.send("Updated status:", embed=embed, ephemeral=True)

    @discord.ui.button(label="Copy IP", style=discord.ButtonStyle.secondary, emoji="📋")
    async def copy_ip(self, interaction: discord.Interaction, _):
        cfg = self.cog.configs.get(str(self.guild_id))
        if not cfg:
            return await interaction.response.send_message("No config found.", ephemeral=True)
        show_port = effective_port(cfg.kind, cfg.port)
        await interaction.response.send_message(f"`{cfg.host}:{show_port}` (for **{cfg.kind.upper()}**)", ephemeral=True)

    @discord.ui.button(label="Restart (stop)", style=discord.ButtonStyle.danger, emoji="🟧")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def restart_btn(self, interaction: discord.Interaction, _):
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

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.configs: Dict[str, ServerConfig] = load_all_configs()

    # ---------- helpers ----------
    async def build_status_embed(self, guild_id: Optional[int]) -> discord.Embed:
        gid = str(guild_id) if guild_id else "global"
        cfg = self.configs.get(gid)

        embed = discord.Embed(
            title="🟩 Minecraft Server Panel" if cfg else "Minecraft Server Panel",
            description="Live status • RCON controls",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url="https://static.wikia.nocookie.net/minecraft_gamepedia/images/5/51/Grass_Block_JE2_BE2.png")

        if not cfg:
            embed.description = "Not configured yet. Use `/mc setup`."
            return embed

        ok, data = await mc_status(cfg.kind, cfg.host, cfg.port)
        show_port = effective_port(cfg.kind, cfg.port)
        if ok:
            status_line = f"**Online** `{cfg.host}:{show_port}`"
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
                value=f"**Offline / Unreachable** `{cfg.host}:{show_port}`\n```{data.get('error','unknown error')}```",
                inline=False,
            )

        hints: List[str] = []
        hints.append("RCON ready" if (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password) else "RCON not set")
        embed.set_footer(text=" • ".join(hints))
        return embed

    # ---------- slash commands ----------
    group = app_commands.Group(name="mc", description="Minecraft server controls")

    @group.command(name="setup", description="Configure server connection + RCON")
    @app_commands.describe(
        kind="Minecraft edition: java or bedrock",
        host="Server host or domain",
        port="Server port (optional; defaults 25565 Java / 19132 Bedrock)",
        rcon_host="RCON host (Java only)",
        rcon_port="RCON port (Java only)",
        rcon_password="RCON password (Java only)",
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
        rcon_password: Optional[str] = None,
    ):
        # fill default port if none provided
        if port is None:
            port = default_port_for(kind.value)

        gid = str(interaction.guild_id)
        self.configs[gid] = ServerConfig(
            kind=kind.value,
            host=host,
            port=port,
            rcon_host=rcon_host,
            rcon_port=rcon_port,
            rcon_password=rcon_password,
        )
        save_all_configs(self.configs)
        await interaction.response.send_message(
            "✅ Minecraft configuration saved for this server.\n"
            f"- Kind: **{kind.value}**\n"
            f"- Address: `{host}:{port}`\n"
            f"- RCON: {'configured' if (rcon_host and rcon_port and rcon_password) else 'not set'}",
            ephemeral=True,
        )

    @group.command(name="status", description="Show live status and open the RCON panel")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_status_embed(interaction.guild_id)
        await interaction.followup.send(embed=embed, view=PanelView(self, interaction.guild_id))

    @group.command(name="panel", description="Open the interactive RCON panel")
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_status_embed(interaction.guild_id)
        await interaction.followup.send(embed=embed, view=PanelView(self, interaction.guild_id))

    # Convenience: soft restart via RCON stop
    @group.command(name="restart", description="Soft restart via RCON (save-all + stop). Requires external auto-restart.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_restart(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        cfg = self.configs.get(gid)
        if not cfg or cfg.kind != "java":
            return await interaction.response.send_message("⚠️ Restart is available for **Java** servers with RCON configured.", ephemeral=True)
        if not (cfg.rcon_host and cfg.rcon_port and cfg.rcon_password):
            return await interaction.response.send_message("⚠️ RCON not configured. Set it in `/mc setup`.", ephemeral=True)

        rp = effective_port(cfg.kind, cfg.rcon_port)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await rcon_stop_sequence(cfg.rcon_host, rp, cfg.rcon_password)
        if ok:
            await interaction.followup.send(
                "✅ Restart sequence issued (server stopped).\n"
                "ℹ️ Make sure Docker/systemd/Pterodactyl auto-restarts the server process.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"❌ Restart failed: `{msg}`", ephemeral=True)

    # Optional: quick ping
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
