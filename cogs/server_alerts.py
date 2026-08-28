# cogs/server_alerts.py
# Minecraft server issue-reporting cog.
#
# Posts a persistent "Report an Issue" panel. Anyone who clicks it picks an
# issue type from a dropdown (Server Down, Lag/TPS, Bug/Crash, Player issue,
# Other), fills in a short details modal, and the cog posts an alert embed in
# the configured alert channel, pinging a configured admin role and/or up to
# a handful of specific admin users. It can also email a set of addresses on
# every report (handy since most phones push-notify on new email — carrier
# email-to-SMS gateways are being phased out, so this is the free fallback).
# Admins can Acknowledge / Resolve each alert with buttons (or the
# /alerts ack /alerts resolve commands), which best-effort DMs the original
# reporter so they know their report was seen/fixed. Reporters are
# rate-limited per-user to avoid spam.
#
# JSON persistence per-guild (data/server_alerts.json) — no DB required.
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "server_alerts.json")

DEFAULT_COOLDOWN_SECONDS = 300  # 5 minutes per reporter

# =========================
# Email alerts (SMTP)
# =========================
# Sends a plain email to each configured address on every report. This
# replaced an earlier email-to-SMS-gateway approach — carriers (Verizon
# included) have been shutting those gateways down, so there's no reliable
# free way to send an actual text. A real email works everywhere and still
# reaches a phone instantly via push notification if the recipient has mail
# notifications on.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USERNAME
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1").strip().lower() not in ("0", "false", "no")


def email_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM)


def _send_email_sync(to_addr: str, subject: str, body: str) -> None:
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    if SMTP_PORT == 465:
        # Implicit TLS: the server expects a TLS handshake immediately, not a
        # plaintext EHLO first — speaking plain SMTP.starttls() here just
        # hangs until it times out.
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            if SMTP_USE_TLS:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_addr], msg.as_string())


async def send_email_alert(to_addr: str, subject: str, body: str) -> Tuple[bool, str]:
    """Sends an alert email. Offloaded to a thread since smtplib is blocking,
    mirroring how the Minecraft cog handles RCON I/O."""
    if not email_configured():
        return False, "SMTP not configured (set SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/SMTP_FROM)."
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: _send_email_sync(to_addr, subject, body))
        return True, "sent"
    except Exception as e:
        return False, str(e)

ISSUE_TYPES = [
    ("down", "🔴 Server Down / Unreachable"),
    ("lag", "🐢 Lag / TPS Issues"),
    ("bug", "🐛 Bug / Crash"),
    ("player", "👥 Player / Griefing Issue"),
    ("other", "❓ Other"),
]
ISSUE_LABELS = {k: v for k, v in ISSUE_TYPES}

STATUS_COLOR = {
    "open": discord.Color.red(),
    "acknowledged": discord.Color.orange(),
    "resolved": discord.Color.green(),
}
STATUS_LABEL = {
    "open": "🔴 Open",
    "acknowledged": "🟠 Acknowledged",
    "resolved": "🟢 Resolved",
}


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class GuildConfig:
    guild_id: int
    alert_channel_id: Optional[int] = None
    panel_channel_id: Optional[int] = None
    panel_message_id: Optional[int] = None
    alert_role_id: Optional[int] = None
    admin_user_ids: List[int] = field(default_factory=list)
    alert_emails: List[Dict[str, str]] = field(default_factory=list)  # [{"label": str, "address": "you@example.com"}]
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    next_report_no: int = 1


class AlertStore:
    """Simple JSON persistence for guild alert configs and report records."""

    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"guilds": {}, "reports": {}}
        _ensure_data_dir()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {"guilds": {}, "reports": {}}
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
            # Tolerate old configs missing newer fields (or carrying fields a
            # later rename removed, e.g. the old sms_numbers -> alert_emails).
            defaults = asdict(GuildConfig(guild_id=guild_id))
            defaults.update({k: v for k, v in g.items() if k in defaults})
            return GuildConfig(**defaults)

    async def set_guild_config(self, cfg: GuildConfig):
        async with self.lock:
            self.data["guilds"][str(cfg.guild_id)] = asdict(cfg)
            self._save()

    async def next_report_number(self, guild_id: int) -> int:
        async with self.lock:
            g = self.data["guilds"].setdefault(str(guild_id), asdict(GuildConfig(guild_id=guild_id)))
            n = g.get("next_report_no", 1)
            g["next_report_no"] = int(n) + 1
            self._save()
            return int(n)

    async def create_report(self, info: Dict[str, Any]) -> str:
        async with self.lock:
            report_id = str(info["number"])
            key = f'{info["guild_id"]}:{report_id}'
            self.data["reports"][key] = info
            self._save()
            return key

    async def get_report(self, key: str) -> Optional[Dict[str, Any]]:
        async with self.lock:
            return self.data["reports"].get(key)

    async def update_report(self, key: str, **changes) -> Optional[Dict[str, Any]]:
        async with self.lock:
            info = self.data["reports"].get(key)
            if not info:
                return None
            info.update(changes)
            self._save()
            return info

    async def open_reports(self, guild_id: int) -> List[Dict[str, Any]]:
        async with self.lock:
            return [
                r for r in self.data["reports"].values()
                if r.get("guild_id") == guild_id and r.get("status") != "resolved"
            ]

    async def find_report(self, guild_id: int, number: int) -> Optional[Dict[str, Any]]:
        key = f"{guild_id}:{number}"
        return await self.get_report(key)


def _report_embed(info: Dict[str, Any]) -> discord.Embed:
    status = info.get("status", "open")
    embed = discord.Embed(
        title=f"🚨 Issue Report #{info['number']:04d} — {ISSUE_LABELS.get(info['issue_type'], info['issue_type'])}",
        description=info.get("details") or "*(no details provided)*",
        color=STATUS_COLOR.get(status, discord.Color.red()),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Reported by", value=f"<@{info['reporter_id']}>", inline=True)
    embed.add_field(name="Status", value=STATUS_LABEL.get(status, status), inline=True)
    if info.get("acknowledged_by"):
        embed.add_field(name="Acknowledged by", value=f"<@{info['acknowledged_by']}>", inline=True)
    if info.get("resolved_by"):
        embed.add_field(name="Resolved by", value=f"<@{info['resolved_by']}>", inline=True)
    embed.set_footer(text=f"Reported at {info.get('created_at', 'unknown')}")
    return embed


class ServerAlerts(commands.Cog):
    """Minecraft server issue reporting: panel button -> alert with admin pings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_data_dir()
        self.store = AlertStore(DATA_FILE)
        self._last_report_at: Dict[int, float] = {}  # user_id -> unix ts, in-memory cooldown

        # Persistent panel button survives restarts (fixed custom_id).
        self.bot.add_view(ReportPanelView(self))

    async def cog_load(self):
        # Re-register per-report Acknowledge/Resolve views for anything still
        # open, bound to their specific messages, so buttons keep working
        # after a restart.
        for key, info in list(self.store.data.get("reports", {}).items()):
            if info.get("status") == "resolved":
                continue
            msg_id = info.get("alert_message_id")
            if not msg_id:
                continue
            try:
                self.bot.add_view(AlertActionView(self, key), message_id=int(msg_id))
            except Exception:
                pass

    # ---------- helpers ----------
    def is_admin(self, member: discord.Member, cfg: GuildConfig) -> bool:
        if member.guild_permissions.manage_guild:
            return True
        if cfg.alert_role_id and any(r.id == cfg.alert_role_id for r in member.roles):
            return True
        if member.id in (cfg.admin_user_ids or []):
            return True
        return False

    def cooldown_remaining(self, user_id: int, cooldown_seconds: int) -> float:
        last = self._last_report_at.get(user_id)
        if not last:
            return 0.0
        remaining = cooldown_seconds - (time.time() - last)
        return max(0.0, remaining)

    def mark_reported(self, user_id: int):
        self._last_report_at[user_id] = time.time()

    async def post_alert(self, interaction: discord.Interaction, issue_type: str, details: str) -> str:
        """Creates a report, posts the alert embed with pings, returns the report key."""
        cfg = await self.store.get_guild_config(interaction.guild_id)
        channel = None
        if cfg.alert_channel_id:
            channel = interaction.guild.get_channel(cfg.alert_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("Alert channel isn't configured or no longer exists. Ask an admin to run `/alerts setup`.")

        number = await self.store.next_report_number(interaction.guild_id)
        info = {
            "guild_id": interaction.guild_id,
            "number": number,
            "reporter_id": interaction.user.id,
            "issue_type": issue_type,
            "details": details,
            "status": "open",
            "created_at": _utc_now_str(),
            "alert_channel_id": channel.id,
            "alert_message_id": None,
            "acknowledged_by": None,
            "resolved_by": None,
            "resolved_at": None,
        }
        key = await self.store.create_report(info)

        mentions = []
        if cfg.alert_role_id:
            mentions.append(f"<@&{cfg.alert_role_id}>")
        for uid in cfg.admin_user_ids or []:
            mentions.append(f"<@{uid}>")
        content = " ".join(mentions) if mentions else None

        allowed = discord.AllowedMentions(roles=True, users=True, everyone=False)
        view = AlertActionView(self, key)
        msg = await channel.send(content=content, embed=_report_embed(info), view=view, allowed_mentions=allowed)
        await self.store.update_report(key, alert_message_id=msg.id)

        if cfg.alert_emails:
            asyncio.create_task(self._email_admins(cfg, info))

        return key

    async def _email_admins(self, cfg: GuildConfig, info: Dict[str, Any]) -> None:
        """Best-effort: emails every configured address, logging failures without
        touching the report — a bad address or SMTP outage shouldn't block reports."""
        label = ISSUE_LABELS.get(info["issue_type"], info["issue_type"])
        subject = f"MC Alert #{info['number']:04d} — {label}"
        body = f"{label}\n\n{info.get('details') or ''}\n\nReported at {info.get('created_at', 'unknown')}"
        for entry in cfg.alert_emails:
            ok, err = await send_email_alert(entry["address"], subject, body)
            if not ok:
                log.warning("Alert email to %s (%s) failed: %s", entry.get("label"), entry.get("address"), err)

    # ---------- admin setup ----------
    alerts = app_commands.Group(name="alerts", description="Configure and manage server issue reports.", guild_only=True)

    @alerts.command(name="setup", description="(Admin) Configure the issue-report panel and alert destination.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        panel_channel="Channel to post the 'Report an Issue' button in",
        alert_channel="Channel where issue alerts (with pings) are posted",
        ping_role="Optional role to ping on every report",
        admin_1="Optional specific admin to ping on every report",
        admin_2="Optional second specific admin to ping on every report",
    )
    async def alerts_setup(
        self,
        interaction: discord.Interaction,
        panel_channel: discord.TextChannel,
        alert_channel: discord.TextChannel,
        ping_role: Optional[discord.Role] = None,
        admin_1: Optional[discord.Member] = None,
        admin_2: Optional[discord.Member] = None,
    ):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.panel_channel_id = panel_channel.id
        cfg.alert_channel_id = alert_channel.id
        cfg.alert_role_id = ping_role.id if ping_role else None
        admins = [m.id for m in (admin_1, admin_2) if m is not None]
        cfg.admin_user_ids = admins

        view = ReportPanelView(self)
        embed = discord.Embed(
            title="🛠️ Server Issues",
            description=(
                "Something wrong with the Minecraft server? Click below to report it.\n"
                "Pick the type of issue and add a few details — admins will be pinged right away."
            ),
            color=discord.Color.blurple(),
        )
        msg = await panel_channel.send(embed=embed, view=view)
        cfg.panel_message_id = msg.id
        await self.store.set_guild_config(cfg)

        pings = []
        if ping_role:
            pings.append(ping_role.mention)
        pings.extend(m.mention for m in (admin_1, admin_2) if m is not None)
        ping_summary = ", ".join(pings) if pings else "*(none configured)*"

        await interaction.response.send_message(
            f"✅ Panel posted in {panel_channel.mention}.\n"
            f"Alerts will be posted in {alert_channel.mention}.\n"
            f"Pinged on report: {ping_summary}\n"
            f"-# Want email alerts too? Use `/alerts email add` to email specific addresses on every report.",
            ephemeral=True,
        )

    @alerts.command(name="panel", description="(Admin) Post another Report an Issue panel here.")
    @app_commands.default_permissions(manage_guild=True)
    async def alerts_panel(self, interaction: discord.Interaction):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not cfg.alert_channel_id:
            return await interaction.response.send_message("Run `/alerts setup` first.", ephemeral=True)

        view = ReportPanelView(self)
        embed = discord.Embed(
            title="🛠️ Server Issues",
            description=(
                "Something wrong with the Minecraft server? Click below to report it.\n"
                "Pick the type of issue and add a few details — admins will be pinged right away."
            ),
            color=discord.Color.blurple(),
        )
        msg = await interaction.channel.send(embed=embed, view=view)
        cfg.panel_channel_id = interaction.channel.id
        cfg.panel_message_id = msg.id
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

    @alerts.command(name="addadmin", description="(Admin) Add a user to the ping list for issue reports.")
    @app_commands.default_permissions(manage_guild=True)
    async def alerts_addadmin(self, interaction: discord.Interaction, user: discord.Member):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if user.id in cfg.admin_user_ids:
            return await interaction.response.send_message(f"{user.mention} is already on the list.", ephemeral=True)
        cfg.admin_user_ids.append(user.id)
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"✅ {user.mention} will now be pinged on reports.", ephemeral=True)

    @alerts.command(name="removeadmin", description="(Admin) Remove a user from the ping list for issue reports.")
    @app_commands.default_permissions(manage_guild=True)
    async def alerts_removeadmin(self, interaction: discord.Interaction, user: discord.Member):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if user.id not in cfg.admin_user_ids:
            return await interaction.response.send_message(f"{user.mention} isn't on the list.", ephemeral=True)
        cfg.admin_user_ids.remove(user.id)
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"✅ {user.mention} removed from the ping list.", ephemeral=True)

    @alerts.command(name="role", description="(Admin) Set or clear the role pinged on issue reports.")
    @app_commands.default_permissions(manage_guild=True)
    async def alerts_role(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.alert_role_id = role.id if role else None
        await self.store.set_guild_config(cfg)
        msg = f"✅ Ping role set to {role.mention}." if role else "✅ Ping role cleared."
        await interaction.response.send_message(msg, ephemeral=True)

    @alerts.command(name="cooldown", description="(Admin) Set the per-user cooldown between reports (seconds).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(seconds="Minimum seconds between reports from the same user (0 to disable)")
    async def alerts_cooldown(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 86400]):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.cooldown_seconds = seconds
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"✅ Report cooldown set to **{seconds}s**.", ephemeral=True)

    # ---------- Email alerts ----------
    email = app_commands.Group(name="email", description="Email specific addresses on every issue report.", parent=alerts)

    @email.command(name="add", description="(Admin) Email an address on every issue report.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        label="A short name for this address (e.g. 'Ethan') — reusing a label replaces it",
        address="Email address to notify (any provider — use one with mobile push notifications for phone alerts)",
    )
    async def email_add(self, interaction: discord.Interaction, label: str, address: str):
        address = address.strip()
        if not EMAIL_RE.match(address):
            return await interaction.response.send_message("⚠️ That doesn't look like a valid email address.", ephemeral=True)
        cfg = await self.store.get_guild_config(interaction.guild_id)
        cfg.alert_emails = [n for n in cfg.alert_emails if n["label"].lower() != label.lower()]
        cfg.alert_emails.append({"label": label, "address": address})
        await self.store.set_guild_config(cfg)

        msg = f"✅ **{label}** (`{address}`) will be emailed on every report."
        if not email_configured():
            msg += (
                "\n⚠️ Heads up: SMTP isn't configured on the bot yet "
                "(`SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM`), so emails won't send until it is."
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @email.command(name="remove", description="(Admin) Stop emailing an address on issue reports.")
    @app_commands.default_permissions(manage_guild=True)
    async def email_remove(self, interaction: discord.Interaction, label: str):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        before = len(cfg.alert_emails)
        cfg.alert_emails = [n for n in cfg.alert_emails if n["label"].lower() != label.lower()]
        if len(cfg.alert_emails) == before:
            return await interaction.response.send_message(f"No address labeled **{label}** found.", ephemeral=True)
        await self.store.set_guild_config(cfg)
        await interaction.response.send_message(f"✅ Removed **{label}**.", ephemeral=True)

    @email.command(name="list", description="(Admin) List email addresses notified on issue reports.")
    @app_commands.default_permissions(manage_guild=True)
    async def email_list(self, interaction: discord.Interaction):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not cfg.alert_emails:
            return await interaction.response.send_message("No alert emails configured.", ephemeral=True)
        lines = [f"• **{n['label']}** — `{n['address']}`" for n in cfg.alert_emails]
        status = "🟢 SMTP configured" if email_configured() else "🔴 SMTP not configured — emails won't send"
        await interaction.response.send_message(f"{status}\n" + "\n".join(lines), ephemeral=True)

    @email.command(name="test", description="(Admin) Send a test email to all configured addresses.")
    @app_commands.default_permissions(manage_guild=True)
    async def email_test(self, interaction: discord.Interaction):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not cfg.alert_emails:
            return await interaction.response.send_message("No alert emails configured.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        results = []
        for entry in cfg.alert_emails:
            ok, err = await send_email_alert(entry["address"], "MC Alert test", "This is only a test.")
            results.append(f"{'✅' if ok else '❌'} **{entry['label']}**" + ("" if ok else f" — `{err}`"))
        await interaction.followup.send("\n".join(results), ephemeral=True)

    # ---------- viewing / managing reports ----------
    @alerts.command(name="list", description="List open (and acknowledged) issue reports.")
    async def alerts_list(self, interaction: discord.Interaction):
        reports = await self.store.open_reports(interaction.guild_id)
        if not reports:
            return await interaction.response.send_message("✅ No open reports.", ephemeral=True)
        reports.sort(key=lambda r: r["number"])
        lines = []
        for r in reports[:20]:
            lines.append(
                f"**#{r['number']:04d}** {STATUS_LABEL.get(r['status'], r['status'])} — "
                f"{ISSUE_LABELS.get(r['issue_type'], r['issue_type'])} — <@{r['reporter_id']}>"
            )
        embed = discord.Embed(title="Open Issue Reports", description="\n".join(lines), color=discord.Color.orange())
        if len(reports) > 20:
            embed.set_footer(text=f"+{len(reports) - 20} more not shown")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alerts.command(name="resolve", description="Mark a report resolved by its number (admins / ping role / ping list).")
    async def alerts_resolve(self, interaction: discord.Interaction, number: int):
        cfg = await self.store.get_guild_config(interaction.guild_id)
        if not isinstance(interaction.user, discord.Member) or not self.is_admin(interaction.user, cfg):
            return await interaction.response.send_message(
                "⚠️ Only admins (or the configured ping role/users) can do that.", ephemeral=True
            )
        info = await self.store.find_report(interaction.guild_id, number)
        if not info:
            return await interaction.response.send_message("No report with that number.", ephemeral=True)
        key = f"{interaction.guild_id}:{number}"
        await self._resolve_report(interaction, key)

    async def _resolve_report(self, interaction: discord.Interaction, key: str):
        info = await self.store.update_report(
            key, status="resolved", resolved_by=interaction.user.id, resolved_at=_utc_now_str()
        )
        if not info:
            return await interaction.response.send_message("Report not found.", ephemeral=True)
        await self._sync_alert_message(info)
        if info["reporter_id"] != interaction.user.id:
            await self._notify_reporter(
                info, f"✅ Your issue report **#{info['number']:04d}** was marked **resolved** by {interaction.user.mention}."
            )
        if not interaction.response.is_done():
            await interaction.response.send_message(f"✅ Report #{info['number']:04d} marked resolved.", ephemeral=True)

    async def _notify_reporter(self, info: Dict[str, Any], message: str) -> None:
        """Best-effort DM to the reporter — silently skipped if their DMs are
        closed, they've left the server, or anything else goes wrong."""
        try:
            user = self.bot.get_user(info["reporter_id"]) or await self.bot.fetch_user(info["reporter_id"])
            await user.send(message)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

    async def _sync_alert_message(self, info: Dict[str, Any]):
        guild = self.bot.get_guild(info["guild_id"])
        if not guild:
            return
        channel = guild.get_channel(info.get("alert_channel_id"))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)) or not info.get("alert_message_id"):
            return
        try:
            msg = await channel.fetch_message(info["alert_message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        key = f'{info["guild_id"]}:{info["number"]}'
        view = AlertActionView(self, key, resolved=(info.get("status") == "resolved"))
        try:
            await msg.edit(embed=_report_embed(info), view=view)
        except discord.HTTPException:
            pass


# =========================
# UI
# =========================
class IssueTypeSelect(discord.ui.Select):
    def __init__(self, cog: ServerAlerts):
        self.cog = cog
        options = [discord.SelectOption(label=label, value=key) for key, label in ISSUE_TYPES]
        super().__init__(placeholder="What's the issue?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReportDetailsModal(self.cog, self.values[0]))


class IssueTypeSelectView(discord.ui.View):
    def __init__(self, cog: ServerAlerts, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.add_item(IssueTypeSelect(cog))


class ReportDetailsModal(discord.ui.Modal, title="Report an Issue"):
    details = discord.ui.TextInput(
        label="What's happening?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Server hasn't responded to /status in 10 minutes, players can't join.",
        required=True,
        min_length=5,
        max_length=500,
    )

    def __init__(self, cog: ServerAlerts, issue_type: str):
        super().__init__()
        self.cog = cog
        self.issue_type = issue_type

    async def on_submit(self, interaction: discord.Interaction):
        cfg = await self.cog.store.get_guild_config(interaction.guild_id)
        remaining = self.cog.cooldown_remaining(interaction.user.id, cfg.cooldown_seconds)
        if remaining > 0:
            return await interaction.response.send_message(
                f"⏳ Please wait **{int(remaining)}s** before reporting again.", ephemeral=True
            )

        try:
            await self.cog.post_alert(interaction, self.issue_type, self.details.value.strip())
        except RuntimeError as e:
            return await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)

        self.cog.mark_reported(interaction.user.id)
        await interaction.response.send_message("✅ Thanks — admins have been notified.", ephemeral=True)


class ReportPanelView(discord.ui.View):
    def __init__(self, cog: ServerAlerts):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Report an Issue", style=discord.ButtonStyle.danger, emoji="🚨", custom_id="alerts:report")
    async def report(self, interaction: discord.Interaction, _: discord.ui.Button):
        cfg = await self.cog.store.get_guild_config(interaction.guild_id)
        remaining = self.cog.cooldown_remaining(interaction.user.id, cfg.cooldown_seconds)
        if remaining > 0:
            return await interaction.response.send_message(
                f"⏳ Please wait **{int(remaining)}s** before reporting again.", ephemeral=True
            )
        await interaction.response.send_message(
            "What kind of issue is it?", view=IssueTypeSelectView(self.cog), ephemeral=True
        )


class AlertActionView(discord.ui.View):
    """Acknowledge / Resolve buttons attached to a specific report's alert message."""

    def __init__(self, cog: ServerAlerts, report_key: str, *, resolved: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.report_key = report_key

        ack_btn = discord.ui.Button(
            label="Acknowledge", style=discord.ButtonStyle.primary, emoji="👀",
            custom_id=f"alerts:ack:{report_key}", disabled=resolved,
        )
        ack_btn.callback = self._acknowledge
        self.add_item(ack_btn)

        resolve_btn = discord.ui.Button(
            label="Resolve", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"alerts:resolve:{report_key}", disabled=resolved,
        )
        resolve_btn.callback = self._resolve
        self.add_item(resolve_btn)

    async def _guard(self, interaction: discord.Interaction) -> Optional[GuildConfig]:
        cfg = await self.cog.store.get_guild_config(interaction.guild_id)
        if not isinstance(interaction.user, discord.Member) or not self.cog.is_admin(interaction.user, cfg):
            await interaction.response.send_message(
                "⚠️ Only admins (or the configured ping role/users) can do that.", ephemeral=True
            )
            return None
        return cfg

    async def _acknowledge(self, interaction: discord.Interaction):
        if await self._guard(interaction) is None:
            return
        info = await self.cog.store.update_report(self.report_key, acknowledged_by=interaction.user.id)
        if not info:
            return await interaction.response.send_message("Report not found.", ephemeral=True)
        if info.get("status") == "open":
            info = await self.cog.store.update_report(self.report_key, status="acknowledged")
        await self.cog._sync_alert_message(info)
        if info["reporter_id"] != interaction.user.id:
            await self.cog._notify_reporter(
                info, f"👀 Your issue report **#{info['number']:04d}** was acknowledged by {interaction.user.mention} — an admin is looking into it."
            )
        await interaction.response.send_message(f"👀 Marked #{info['number']:04d} acknowledged.", ephemeral=True)

    async def _resolve(self, interaction: discord.Interaction):
        if await self._guard(interaction) is None:
            return
        await self.cog._resolve_report(interaction, self.report_key)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerAlerts(bot))
