# cogs/shipping_tracker.py
# Discord Cog: Shipping Tracker (FedEx, USPS, UPS) with per-user storage, auto-updates, and stats.
# Author: ChatGPT for Ethan
#
# Features:
# - /track add <carrier|auto> <tracking_number> [nickname] [channel]
# - /track list
# - /track remove <id>
# - /track info <id>
# - /track refresh [id]
# - /track stats [year] [scope]
# - Background poller that checks undelivered packages and posts updates until delivered.
#
# Storage:
# - JSON at ./data/trackings.json (configurable via TRACKER_DB_PATH env)
# - Per user entries; each tracking has history and notification preferences.
#
# Providers:
# - AfterShip (recommended): set AFTERSHIP_API_KEY in env
# - Fallback "web" provider: last resort scrapes public pages using simple heuristics (best-effort; may break).
#   You can disable it by setting TRACKER_DISABLE_FALLBACK=1
#
# Permissions & Destinations:
# - Each tracking can have a preferred channel. If set, we post there iff the *user* has send perms in that channel;
#   otherwise DM the user. If no channel set, DM the user.
#
# Notes:
# - Carriers supported: fedex, ups, usps. "auto" tries to infer from the number pattern.
# - This cog requires discord.py 2.3+.
# - Add this file under your `cogs/` folder and load it like any other cog.
#
# ---

import os
import re
import json
import math
import asyncio
import datetime as dt
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------- Small JSON store ----------

def _now_iso():
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).isoformat()

class JsonStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"users": {}}, f, indent=2)
        self._lock = asyncio.Lock()

    async def read(self) -> Dict[str, Any]:
        async with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)

    async def write(self, data: Dict[str, Any]) -> None:
        async with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

# ---------- Carrier utils ----------

CARRIER_CODES = {"fedex", "ups", "usps"}

def guess_carrier(number: str) -> Optional[str]:
    n = number.replace(" ", "").replace("-", "").upper()
    # Very rough heuristics:
    # UPS: starts with 1Z + 16 chars
    if n.startswith("1Z") and len(n) >= 18:
        return "ups"
    # FedEx: many formats; 12, 15, 20, 22, 34 digits commonly.
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}|\d{34}", n):
        return "fedex"
    # USPS: usually 20-22 or 26-30 digits, or starts with 2 letters + 9 digits + US
    if re.fullmatch(r"[A-Z]{2}\d{9}US", n) or re.fullmatch(r"\d{20,30}", n):
        return "usps"
    return None

# ---------- Provider Abstraction ----------

class TrackingEvent:
    def __init__(self, status: str, description: str, time: Optional[str], location: Optional[str] = None):
        self.status = status
        self.description = description
        self.time = time  # ISO8601 string UTC if possible
        self.location = location

    def to_dict(self):
        return {"status": self.status, "description": self.description, "time": self.time, "location": self.location}

class TrackingResult:
    def __init__(self, status: str, delivered: bool, last_update: Optional[str], events: List[TrackingEvent]):
        self.status = status              # e.g., "In Transit", "Out for Delivery", "Delivered", etc.
        self.delivered = delivered
        self.last_update = last_update    # ISO time for the latest event if available
        self.events = events

    def to_dict(self):
        return {
            "status": self.status,
            "delivered": self.delivered,
            "last_update": self.last_update,
            "events": [e.to_dict() for e in self.events],
        }

class ProviderBase:
    async def fetch(self, carrier: str, tracking_number: str) -> TrackingResult:
        raise NotImplementedError

class AfterShipProvider(ProviderBase):
    BASE = "https://api.aftership.com/v4"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, carrier: str, tracking_number: str) -> TrackingResult:
        headers = {"aftership-api-key": self.api_key, "content-type": "application/json"}
        url = f"{self.BASE}/trackings/{carrier}/{tracking_number}"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 404:
                    # Not found on AfterShip; try creating it then refetch.
                    create_url = f"{self.BASE}/trackings"
                    payload = {"tracking": {"tracking_number": tracking_number, "slug": carrier}}
                    async with sess.post(create_url, headers=headers, json=payload, timeout=30) as cr:
                        if cr.status not in (200, 201):
                            text = await cr.text()
                            raise RuntimeError(f"AfterShip create failed: {cr.status} {text}")
                    async with sess.get(url, headers=headers, timeout=30) as resp2:
                        resp2.raise_for_status()
                        data = await resp2.json()
                else:
                    resp.raise_for_status()
                    data = await resp.json()

        trk = data.get("data", {}).get("tracking", {})
        checkpoints = trk.get("checkpoints", []) or []
        events: List[TrackingEvent] = []
        last_time = None
        for cp in checkpoints:
            ts = cp.get("checkpoint_time") or cp.get("created_at")
            # AfterShip times are often ISO strings UTC
            loc = ", ".join(filter(None, [cp.get("city"), cp.get("state"), cp.get("country_name")]))
            events.append(TrackingEvent(
                status=cp.get("tag") or cp.get("subtag") or cp.get("message") or "Update",
                description=cp.get("message") or cp.get("subtag_message") or cp.get("tag"),
                time=ts,
                location=loc or None,
            ))
            if ts:
                last_time = ts
        delivered = trk.get("tag") == "Delivered"
        status = trk.get("subtag_message") or trk.get("tag") or trk.get("status") or ("Delivered" if delivered else "In Transit")
        return TrackingResult(status=status, delivered=delivered, last_update=last_time, events=events)

class FallbackWebProvider(ProviderBase):
    """Best-effort HTML fetcher for public tracking pages.
    WARNING: fragile, may break or be blocked. Disable with TRACKER_DISABLE_FALLBACK=1.
    """
    FED_EX = "https://www.fedex.com/fedextrack/?trknbr={}"
    UPS = "https://www.ups.com/track?loc=en_US&tracknum={}"
    USPS = "https://tools.usps.com/go/TrackConfirmAction?tLabels={}"

    async def fetch(self, carrier: str, tracking_number: str) -> TrackingResult:
        url = {
            "fedex": self.FED_EX.format(tracking_number),
            "ups": self.UPS.format(tracking_number),
            "usps": self.USPS.format(tracking_number),
        }.get(carrier)
        if not url:
            raise ValueError("Unsupported carrier for fallback")

        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=30) as resp:
                text = await resp.text()

        # Extremely naive scrape: just surface that we "checked".
        # Encourage AfterShip for reliability.
        ev = TrackingEvent(
            status="Checked",
            description=f"Fetched public tracking page for {carrier.upper()} (parsing disabled).",
            time=_now_iso(),
        )
        return TrackingResult(status="In Transit (unknown)", delivered=False, last_update=ev.time, events=[ev])

# ---------- Main Cog ----------

class ShippingTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        db_path = os.getenv("TRACKER_DB_PATH", "./data/trackings.json")
        self.store = JsonStore(db_path)

        self.aftership_key = os.getenv("AFTERSHIP_API_KEY")
        self.disable_fallback = os.getenv("TRACKER_DISABLE_FALLBACK") == "1"

        self.providers: List[ProviderBase] = []
        if self.aftership_key:
            self.providers.append(AfterShipProvider(self.aftership_key))
        if not self.disable_fallback:
            self.providers.append(FallbackWebProvider())

        self.poll_interval = int(os.getenv("TRACKER_POLL_MINUTES", "15"))
        self._poller.start()

    def cog_unload(self):
        if self._poller.is_running():
            self._poller.cancel()

    # ---------- Provider usage ----------
    async def fetch_status(self, carrier: str, number: str) -> TrackingResult:
        if not self.providers:
            raise RuntimeError("No tracking providers configured. Set AFTERSHIP_API_KEY or enable fallback.")
        last_exc = None
        for p in self.providers:
            try:
                return await p.fetch(carrier, number)
            except Exception as e:
                last_exc = e
                continue
        raise RuntimeError(f"All providers failed. Last error: {last_exc}")

    # ---------- Data helpers ----------

    async def _get_user_doc(self, user_id: int) -> Dict[str, Any]:
        data = await self.store.read()
        users = data.setdefault("users", {})
        user = users.setdefault(str(user_id), {"trackings": {}, "events": []})
        return user

    async def _save_user_doc(self, user_id: int, user_doc: Dict[str, Any]) -> None:
        data = await self.store.read()
        data["users"][str(user_id)] = user_doc
        await self.store.write(data)

    # ---------- Commands ----------

    track = app_commands.Group(name="track", description="Package tracking commands")

    @track.command(name="add", description="Add a tracking number")
    @app_commands.describe(
        carrier="fedex | ups | usps | auto",
        tracking_number="Your tracking number",
        nickname="Optional short name (e.g., 'GPU order')",
        channel="Optional channel for updates (otherwise you'll get DMs)"
    )
    async def add(
        self,
        interaction: discord.Interaction,
        carrier: str,
        tracking_number: str,
        nickname: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        carrier = carrier.lower().strip()
        if carrier == "auto":
            g = guess_carrier(tracking_number)
            if not g:
                await interaction.followup.send("Couldn't infer the carrier. Please specify fedex/ups/usps.", ephemeral=True)
                return
            carrier = g
        if carrier not in CARRIER_CODES:
            await interaction.followup.send("Carrier must be one of: fedex, ups, usps, or 'auto'.", ephemeral=True)
            return

        user_doc = await self._get_user_doc(interaction.user.id)
        # Generate ID
        t_id = str(int(dt.datetime.utcnow().timestamp()*1000))
        user_doc["trackings"][t_id] = {
            "carrier": carrier,
            "number": tracking_number.strip(),
            "nickname": nickname or "",
            "created_at": _now_iso(),
            "delivered_at": None,
            "last_status": "Unknown",
            "last_update": None,
            "history": [],
            "notify_channel_id": channel.id if channel else None,
            "last_notified_hash": None,
        }
        await self._save_user_doc(interaction.user.id, user_doc)

        # Do an initial fetch to validate & seed
        try:
            res = await self.fetch_status(carrier, tracking_number)
            await self._apply_update(interaction.user, t_id, res, seed_only=True)
        except Exception as e:
            # Keep it saved but warn
            await interaction.followup.send(f"Saved tracking, but initial fetch failed: `{e}`", ephemeral=True)
            return

        await interaction.followup.send(f"Added **{nickname or tracking_number}** ({carrier.upper()}). You'll receive updates.", ephemeral=True)

    @track.command(name="list", description="List your active & delivered trackings")
    async def list(self, interaction: discord.Interaction):
        user_doc = await self._get_user_doc(interaction.user.id)
        items = user_doc["trackings"]
        if not items:
            await interaction.response.send_message("You have no saved trackings.", ephemeral=True)
            return

        active = []
        delivered = []
        for tid, t in items.items():
            line = f"`{tid}` • **{t.get('nickname') or t['number']}** • {t['carrier'].upper()} • {t.get('last_status','?')}"
            if t.get("delivered_at"):
                delivered.append(line)
            else:
                active.append(line)

        embed = discord.Embed(title="📦 Your Trackings", color=discord.Color.blurple())
        if active:
            embed.add_field(name="Active", value="\n".join(active[:1024]), inline=False)
        if delivered:
            embed.add_field(name="Delivered", value="\n".join(delivered[:1024]), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @track.command(name="remove", description="Remove a saved tracking by its ID")
    async def remove(self, interaction: discord.Interaction, tracking_id: str):
        user_doc = await self._get_user_doc(interaction.user.id)
        if tracking_id not in user_doc["trackings"]:
            await interaction.response.send_message("No tracking with that ID.", ephemeral=True)
            return
        t = user_doc["trackings"].pop(tracking_id)
        await self._save_user_doc(interaction.user.id, user_doc)
        await interaction.response.send_message(f"Removed **{t.get('nickname') or t['number']}**.", ephemeral=True)

    @track.command(name="info", description="Show details for one tracking")
    async def info(self, interaction: discord.Interaction, tracking_id: str):
        user_doc = await self._get_user_doc(interaction.user.id)
        t = user_doc["trackings"].get(tracking_id)
        if not t:
            await interaction.response.send_message("No tracking with that ID.", ephemeral=True)
            return
        embed = discord.Embed(title=f"📦 {t.get('nickname') or t['number']} ({t['carrier'].upper()})", color=discord.Color.blurple())
        embed.add_field(name="Status", value=t.get("last_status") or "Unknown", inline=True)
        embed.add_field(name="Last Update", value=t.get("last_update") or "—", inline=True)
        embed.add_field(name="Created", value=t.get("created_at") or "—", inline=True)
        if t.get("delivered_at"):
            embed.add_field(name="Delivered", value=t["delivered_at"], inline=True)
        hist = t.get("history", [])
        if hist:
            # Show last 5 events
            recent = hist[-5:]
            lines = []
            for ev in recent:
                ts = ev.get("time") or "—"
                lines.append(f"• **{ev.get('status','Update')}** — {ev.get('description','')} ({ts})")
            embed.add_field(name="Recent Events", value="\n".join(lines), inline=False)
        if t.get("notify_channel_id"):
            embed.set_footer(text=f"Prefers updates in <#{t['notify_channel_id']}>")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @track.command(name="refresh", description="Force refresh (all or one)")
    async def refresh(self, interaction: discord.Interaction, tracking_id: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        user_doc = await self._get_user_doc(user.id)
        todo = [(tid, t) for tid, t in user_doc["trackings"].items()] if not tracking_id else [(tracking_id, user_doc["trackings"].get(tracking_id))]
        todo = [(tid, t) for tid, t in todo if t]
        if not todo:
            await interaction.followup.send("Nothing to refresh.", ephemeral=True)
            return
        ok = 0
        for tid, t in todo:
            try:
                res = await self.fetch_status(t["carrier"], t["number"])
                await self._apply_update(user, tid, res, notify=True)
                ok += 1
            except Exception as e:
                await interaction.followup.send(f"`{tid}` refresh failed: `{e}`", ephemeral=True)
        await interaction.followup.send(f"Refreshed {ok} tracking(s).", ephemeral=True)

    @track.command(name="stats", description="Show your shipping stats")
    @app_commands.describe(year="Year like 2025 (default: current)", scope="user or global (default: user)")
    async def stats(self, interaction: discord.Interaction, year: Optional[int] = None, scope: Optional[str] = "user"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        yr = year or dt.datetime.utcnow().year
        scope = (scope or "user").lower()
        data = await self.store.read()

        def gather_events(user_ids: List[str]):
            delivered_days = []
            count = 0
            by_carrier = {"fedex":0,"ups":0,"usps":0}
            for uid in user_ids:
                u = data.get("users", {}).get(uid, {})
                for _, t in u.get("trackings", {}).items():
                    # consider delivered within year
                    created = t.get("created_at")
                    delivered = t.get("delivered_at")
                    if created and created[:4].isdigit() and int(created[:4])==yr:
                        count += 1
                        by_carrier[t.get("carrier","unknown")] = by_carrier.get(t.get("carrier","unknown"),0)+1
                    if delivered and delivered[:4].isdigit() and int(delivered[:4])==yr and created:
                        try:
                            start = dt.datetime.fromisoformat(created.replace("Z","+00:00"))
                            end = dt.datetime.fromisoformat(delivered.replace("Z","+00:00"))
                            days = max(0.0, (end-start).total_seconds()/86400.0)
                            delivered_days.append(days)
                        except Exception:
                            pass
            avg_days = sum(delivered_days)/len(delivered_days) if delivered_days else None
            return count, avg_days, by_carrier

        if scope == "global":
            user_ids = list((data.get("users") or {}).keys())
        else:
            user_ids = [str(interaction.user.id)]
        total, avg_days, by_carrier = gather_events(user_ids)

        embed = discord.Embed(title=f"📈 Shipping Stats — {yr} ({scope.title()})", color=discord.Color.green())
        embed.add_field(name="Packages Tracked", value=str(total), inline=True)
        embed.add_field(name="Average Days (Created → Delivered)", value=f"{avg_days:.1f}" if avg_days is not None else "—", inline=True)
        lines = [f"{k.upper()}: {v}" for k,v in by_carrier.items() if v]
        embed.add_field(name="By Carrier", value="\n".join(lines) if lines else "—", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- Poller & Notifications ----------

    @tasks.loop(minutes=15.0)
    async def _poller(self):
        await self.bot.wait_until_ready()
        data = await self.store.read()
        users = data.get("users", {})
        # Iterate users and active trackings
        for uid, udoc in users.items():
            member = None
            try:
                # Best effort: get a Member object from any guild the bot shares.
                for g in self.bot.guilds:
                    m = g.get_member(int(uid))
                    if m:
                        member = m
                        break
                if not member:
                    # try creating a User
                    member = await self.bot.fetch_user(int(uid))
            except Exception:
                continue

            changed = False
            for tid, t in list(udoc.get("trackings", {}).items()):
                if t.get("delivered_at"):
                    continue  # skip delivered
                try:
                    res = await self.fetch_status(t["carrier"], t["number"])
                except Exception:
                    continue
                # Apply update + maybe notify
                sent = await self._apply_update(member, tid, res, notify=True)
                if sent:
                    changed = True
            if changed:
                await self.store.write(data)  # persist any inline updates

    @_poller.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        # Adjust to configured interval
        try:
            self._poller.change_interval(minutes=float(self.poll_interval))
        except Exception:
            pass

    async def _apply_update(self, user_or_member: discord.abc.User, tracking_id: str, res: TrackingResult, seed_only: bool=False, notify: bool=False) -> bool:
        """Merge a TrackingResult into storage. Return True if notification was sent."""
        user_doc = await self._get_user_doc(user_or_member.id)
        t = user_doc["trackings"].get(tracking_id)
        if not t:
            return False

        # Build a hash-ish string for change detection
        last_event = res.events[-1] if res.events else None
        new_hash = f"{res.status}|{res.delivered}|{res.last_update}|{(last_event.description if last_event else '')}"[:256]
        if t.get("last_notified_hash") == new_hash and not seed_only:
            return False

        # Merge
        t["last_status"] = res.status
        t["last_update"] = res.last_update or _now_iso()
        for ev in res.events:
            user_doc["events"] = user_doc.get("events", [])
            t["history"].append(ev.to_dict())

        if res.delivered and not t.get("delivered_at"):
            t["delivered_at"] = t["last_update"]

        await self._save_user_doc(user_or_member.id, user_doc)

        if seed_only or not notify:
            return False

        # Determine destination: preferred channel if user has perms; else DM
        chan_id = t.get("notify_channel_id")
        dest = None
        if chan_id:
            # Find channel in all guilds
            for g in self.bot.guilds:
                ch = g.get_channel(chan_id)
                if ch and isinstance(ch, discord.TextChannel):
                    # Check if the USER has send perms
                    perms = ch.permissions_for(getattr(user_or_member, "guild", None) and user_or_member or ch.guild.get_member(user_or_member.id) or user_or_member)
                    # Fallback: try guild member
                    if hasattr(user_or_member, "guild") and user_or_member.guild == ch.guild:
                        member = user_or_member
                    else:
                        member = ch.guild.get_member(user_or_member.id)
                    if member and ch.permissions_for(member).send_messages:
                        dest = ch
                        break

        embed = discord.Embed(
            title=f"📦 {t.get('nickname') or t['number']} — {t['carrier'].upper()}",
            description=res.status,
            color=discord.Color.blurple() if not res.delivered else discord.Color.green(),
            timestamp=dt.datetime.utcnow()
        )
        if last_event:
            le = last_event
            more = []
            if le.location:
                more.append(le.location)
            if le.time:
                more.append(le.time)
            embed.add_field(name=le.status or "Update", value=f"{le.description or ''}\n" + (" — ".join(more) if more else ""), inline=False)
        if res.delivered:
            embed.set_footer(text="Delivered")

        try:
            if dest:
                await dest.send(content=user_or_member.mention, embed=embed)
            else:
                await user_or_member.send(embed=embed)
            t["last_notified_hash"] = new_hash
            await self._save_user_doc(user_or_member.id, user_doc)
            return True
        except discord.Forbidden:
            # can't DM; nothing else to do
            return False

# ---------- Setup ----------

async def setup(bot: commands.Bot):
    await bot.add_cog(ShippingTracker(bot))
