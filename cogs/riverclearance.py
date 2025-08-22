from __future__ import annotations
import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy import text

# USGS Instantaneous Values (IV) service
USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
USGS_PARAM = "00065"  # Gage height, feet
# Bounding box for upper Mississippi corridor (lonLeft, latLower, lonRight, latUpper)
UPPER_MS_BBOX = (-94.5, 37.5, -89.0, 46.5)

# Default ArcGIS Bridges layer (USACE Navigation Charts style). Override with /rc set_source
DEFAULT_BRIDGES_LAYER = (
    "https://navigation.usace.army.mil/arcgis/rest/services/NTNP/NavigationCharts/MapServer/21"
)

def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8  # miles
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

@dataclass
class Gauge:
    site_id: str
    name: str
    lat: float
    lon: float
    stage_ft: Optional[float]

@dataclass
class Bridge:
    id: str
    name: str
    river_mile: Optional[float]
    lat: float
    lon: float
    clearance_ft: Optional[float]  # nominal clearance at ref datum/pool
    ref_stage_ft: Optional[float]  # if known; else None
    source: str

class RiverClearance(commands.Cog):
    """Upper Mississippi River: live stages, bridge clearances, and vessel air-draft margins."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_gauge_pull = 0.0
        self.last_bridge_pull = 0.0

        # Create tables
        with self.bot.engine.begin() as conn:
            conn.execute(text(
                """CREATE TABLE IF NOT EXISTS rc_vessels (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT,
                    air_draft_ft REAL NOT NULL,
                    updated_ts TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            ))
            conn.execute(text(
                """CREATE TABLE IF NOT EXISTS rc_settings (
                    k TEXT PRIMARY KEY,
                    v TEXT
                )"""
            ))
            conn.execute(text(
                """CREATE TABLE IF NOT EXISTS rc_gauges (
                    site_id TEXT PRIMARY KEY,
                    name TEXT,
                    lat REAL, lon REAL,
                    last_stage_ft REAL,
                    last_fetched_ts TEXT
                )"""
            ))
            conn.execute(text(
                """CREATE TABLE IF NOT EXISTS rc_bridges (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    river_mile REAL,
                    lat REAL, lon REAL,
                    clearance_ft REAL,
                    ref_stage_ft REAL,
                    source TEXT,
                    last_fetched_ts TEXT
                )"""
            ))

        # Default settings if not present
        with self.bot.engine.begin() as conn:
            row = conn.execute(text("SELECT v FROM rc_settings WHERE k='bridges_layer_url'")).fetchone()
            if not row:
                conn.execute(text(
                    "INSERT INTO rc_settings(k,v) VALUES('bridges_layer_url', :u)"
                ), {"u": DEFAULT_BRIDGES_LAYER})

        self.refresh_gauges.start()

    async def cog_unload(self):
        self.refresh_gauges.cancel()
        if self.session:
            await self.session.close()

    # Background gauge refresh
    @tasks.loop(minutes=15.0)
    async def refresh_gauges(self):
        try:
            await self._ensure_session()
            await self._pull_usgs_gauges()
        except Exception as e:
            print("[river_clearance] refresh_gauges error:", e)

    # ---- Slash command group
    group = app_commands.Group(name="rc", description="River Clearance tools")

    @group.command(name="set_airdraft", description="Save your vessel air draft (feet above waterline).")
    async def set_airdraft(self, interaction: discord.Interaction, feet: app_commands.Range[float, 1.0, 200.0], name: Optional[str] = None):
        with self.bot.engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO rc_vessels(user_id, name, air_draft_ft)
                        VALUES(:u,:n,:a)
                        ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, air_draft_ft=excluded.air_draft_ft, updated_ts=CURRENT_TIMESTAMP"""),
                {"u": interaction.user.id, "n": name or "Vessel", "a": float(feet)}
            )
        await interaction.response.send_message(f"Saved air draft **{feet:.1f} ft** for you.", ephemeral=True)

    @group.command(name="my_vessel", description="Show your saved air draft.")
    async def my_vessel(self, interaction: discord.Interaction):
        with self.bot.engine.connect() as c:
            row = c.execute(text("SELECT name, air_draft_ft, updated_ts FROM rc_vessels WHERE user_id=:u"), {"u": interaction.user.id}).fetchone()
        if not row:
            return await interaction.response.send_message("No vessel saved. Use `/rc set_airdraft` first.", ephemeral=True)
        name, airdraft, ts = row
        e = discord.Embed(title="🛥️ Your Vessel", color=discord.Color.blurple())
        e.add_field(name="Name", value=name or "Vessel", inline=True)
        e.add_field(name="Air Draft", value=f"{airdraft:.1f} ft", inline=True)
        e.set_footer(text=f"Updated {ts}")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @group.command(name="sync_bridges", description="Admin: sync bridges from the configured ArcGIS layer (Upper Mississippi corridor).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sync_bridges(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._ensure_session()
        n = await self._pull_bridges()
        await interaction.followup.send(f"Bridges sync complete. Stored: **{n}**", ephemeral=True)

    @group.command(name="set_source", description="Admin: set source URLs (bridges layer).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_source(self, interaction: discord.Interaction, bridges_layer_url: str):
        with self.bot.engine.begin() as conn:
            conn.execute(text("INSERT INTO rc_settings(k,v) VALUES('bridges_layer_url', :v) ON CONFLICT(k) DO UPDATE SET v=excluded.v"),
                         {"v": bridges_layer_url})
        await interaction.response.send_message("Updated bridges layer URL.", ephemeral=True)

    @group.command(name="stages", description="Show current river stages (key gauges) in the corridor.")
    async def stages(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gauges = self._list_gauges(limit=15)
        if not gauges:
            return await interaction.followup.send("No gauges cached yet. Try again in a minute.")
        e = discord.Embed(title="📈 Upper Mississippi — River Stages", color=discord.Color.green())
        for g in gauges[:15]:
            stage = f"{g.stage_ft:.1f} ft" if g.stage_ft is not None else "—"
            e.add_field(name=g.name[:256], value=stage, inline=True)
        await interaction.followup.send(embed=e)

    @group.command(name="clearance", description="Compute live bridge clearance vs your air draft near a location (approximate).")
    async def clearance(self, interaction: discord.Interaction, near_lat: float, near_lon: float, radius_mi: app_commands.Range[float, 1.0, 100.0] = 25.0):
        await interaction.response.defer()
        with self.bot.engine.connect() as c:
            v = c.execute(text("SELECT air_draft_ft FROM rc_vessels WHERE user_id=:u"), {"u": interaction.user.id}).fetchone()
        if not v:
            return await interaction.followup.send("No vessel saved. Use `/rc set_airdraft` first.", ephemeral=True)
        air = float(v[0])

        bridges = self._list_bridges()
        nearby = [b for b in bridges if haversine_miles(near_lat, near_lon, b.lat, b.lon) <= radius_mi]
        if not nearby:
            return await interaction.followup.send("No bridges found near that location (try a bigger radius).")

        gauges = self._list_gauges()
        if not gauges:
            return await interaction.followup.send("No gauges cached yet. Try again shortly.")

        items = []
        for b in nearby:
            g = min(gauges, key=lambda gg: haversine_miles(b.lat, b.lon, gg.lat, gg.lon))
            stage = g.stage_ft
            if b.clearance_ft is None or stage is None:
                continue
            ref = b.ref_stage_ft if b.ref_stage_ft is not None else 0.0
            clearance_now = b.clearance_ft - (stage - ref)
            margin = clearance_now - air
            items.append((b, g, clearance_now, margin))

        if not items:
            return await interaction.followup.send("Missing clearance metrics for nearby bridges. Try syncing bridges or setting ref stages.")

        items.sort(key=lambda x: x[3])
        e = discord.Embed(
            title="🧮 Live Bridge Clearance (approximate)",
            description=f"Air draft: **{air:.1f} ft** — showing bridges within **{radius_mi:.0f} mi**",
            color=discord.Color.orange(),
        )
        for b, g, clr, m in items[:10]:
            status = "✅ OK" if m >= 0 else "⚠️ LOW"
            e.add_field(
                name=f"{b.name}",
                value=f"Clearance now: **{clr:.1f} ft** | Margin vs you: **{m:+.1f} ft**\n"
                      f"Nearest gauge: {g.name} ({g.stage_ft:.1f} ft)",
                inline=False
            )
        e.set_footer(text="Approximation: clearance_now ≈ nominal - (stage - ref). Ref defaults to 0 if unknown.")
        await interaction.followup.send(embed=e)

    # ---- Internals

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def _pull_usgs_gauges(self) -> int:
        lonL, latL, lonR, latU = UPPER_MS_BBOX
        params = {
            "format": "json",
            "parameterCd": USGS_PARAM,
            "bBox": f"{lonL},{latL},{lonR},{latU}",
        }
        url = USGS_IV_URL
        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        timeSeries = data.get("value", {}).get("timeSeries", [])
        count = 0
        with self.bot.engine.begin() as conn:
            for ts in timeSeries:
                source_info = ts.get("sourceInfo", {})
                site = source_info.get("siteCode", [{}])[0].get("value")
                name = source_info.get("siteName")
                geo = source_info.get("geoLocation", {}).get("geogLocation", {})
                lat = float(geo.get("latitude")) if geo.get("latitude") is not None else None
                lon = float(geo.get("longitude")) if geo.get("longitude") is not None else None
                val_series = ts.get("values", [{}])[0].get("value", [])
                stage_ft = None
                if val_series:
                    try:
                        stage_ft = float(val_series[-1].get("value"))
                    except Exception:
                        stage_ft = None
                if not site or lat is None or lon is None:
                    continue
                conn.execute(text(
                    """INSERT INTO rc_gauges(site_id,name,lat,lon,last_stage_ft,last_fetched_ts)
                       VALUES(:s,:n,:lat,:lon,:st,CURRENT_TIMESTAMP)
                       ON CONFLICT(site_id) DO UPDATE SET name=excluded.name, lat=excluded.lat, lon=excluded.lon,
                                                          last_stage_ft=excluded.last_stage_ft, last_fetched_ts=CURRENT_TIMESTAMP"""
                ), {"s": site, "n": name, "lat": lat, "lon": lon, "st": stage_ft})
                count += 1
        self.last_gauge_pull = time.time()
        return count

    async def _pull_bridges(self) -> int:
        with self.bot.engine.connect() as c:
            row = c.execute(text("SELECT v FROM rc_settings WHERE k='bridges_layer_url'")).fetchone()
        layer_url = row[0] if row else DEFAULT_BRIDGES_LAYER

        lonL, latL, lonR, latU = UPPER_MS_BBOX
        params = {
            "where": "1=1",
            "geometry": f"{lonL},{latL},{lonR},{latU}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "f": "json",
        }

        feats: List[Dict[str, Any]] = []
        async with self.session.get(f"{layer_url}/query", params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Bridges layer fetch failed: HTTP {resp.status}")
            data = await resp.json()
            feats.extend(data.get("features", []))
            # Note: pagination may be needed for some layers; omitted for now.

        if not feats:
            return self._count_bridges()

        count = 0
        with self.bot.engine.begin() as conn:
            for f in feats:
                attrs = f.get("attributes", {})
                geom = f.get("geometry", {})
                bid = str(attrs.get("OBJECTID") or attrs.get("ID") or attrs.get("GlobalID") or f"b_{count}")
                name = attrs.get("NAME") or attrs.get("STRUCTURE_N") or attrs.get("StructureName") or "Bridge"
                river_mile = attrs.get("RIVERMILE") or attrs.get("RM")
                try:
                    river_mile = float(river_mile) if river_mile is not None else None
                except Exception:
                    river_mile = None
                clearance = attrs.get("VERT_CLR") or attrs.get("VERTCLR_FT") or attrs.get("NAV_VCL") or attrs.get("VERTICAL_CL")
                try:
                    clearance = float(clearance) if clearance is not None else None
                except Exception:
                    clearance = None
                ref_stage = attrs.get("REF_STAGE")
                try:
                    ref_stage = float(ref_stage) if ref_stage is not None else None
                except Exception:
                    ref_stage = None
                x = geom.get("x"); y = geom.get("y")
                if x is None or y is None:
                    continue
                conn.execute(text(
                    """INSERT INTO rc_bridges(id,name,river_mile,lat,lon,clearance_ft,ref_stage_ft,source,last_fetched_ts)
                       VALUES(:i,:n,:rm,:lat,:lon,:c,:r,:src,CURRENT_TIMESTAMP)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name, river_mile=excluded.river_mile,
                           lat=excluded.lat, lon=excluded.lon, clearance_ft=excluded.clearance_ft,
                           ref_stage_ft=excluded.ref_stage_ft, source=excluded.source, last_fetched_ts=CURRENT_TIMESTAMP"""
                ), {"i": bid, "n": name, "rm": river_mile, "lat": float(y), "lon": float(x),
                    "c": clearance, "r": ref_stage, "src": layer_url})
                count += 1
        self.last_bridge_pull = time.time()
        return count

    def _list_gauges(self, limit: Optional[int] = None) -> List[Gauge]:
        with self.bot.engine.connect() as c:
            rows = c.execute(text("SELECT site_id,name,lat,lon,last_stage_ft FROM rc_gauges ORDER BY name ASC")).fetchall()
        gauges = [Gauge(r[0], r[1], float(r[2]), float(r[3]), (float(r[4]) if r[4] is not None else None)) for r in rows]
        if limit:
            return gauges[:limit]
        return gauges

    def _list_bridges(self) -> List[Bridge]:
        with self.bot.engine.connect() as c:
            rows = c.execute(text("SELECT id,name,river_mile,lat,lon,clearance_ft,ref_stage_ft,source FROM rc_bridges")).fetchall()
        return [
            Bridge(str(r[0]), r[1], (float(r[2]) if r[2] is not None else None), float(r[3]), float(r[4]),
                   (float(r[5]) if r[5] is not None else None), (float(r[6]) if r[6] is not None else None), str(r[7]))
            for r in rows
        ]

    def _count_bridges(self) -> int:
        with self.bot.engine.connect() as c:
            row = c.execute(text("SELECT COUNT(*) FROM rc_bridges")).fetchone()
        return int(row[0]) if row else 0

async def setup(bot: commands.Bot):
    cog = RiverClearance(bot)
    await bot.add_cog(cog)
    # Ensure the /rc group is registered even when defined inside the cog
    try:
        bot.tree.add_command(cog.group)
    except Exception:
        # If it was already added, ignore
        pass