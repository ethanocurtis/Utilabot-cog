# cogs/kutt.py
from __future__ import annotations
import os
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from urllib.parse import urlparse

# ---- ENV (compatible with your old bot) ----
KUTT_HOST = (os.getenv("KUTT_HOST") or os.getenv("KUTT_BASE_URL") or "https://kutt.it").rstrip("/")
KUTT_API  = os.getenv("KUTT_API") or os.getenv("KUTT_API_KEY")
# Domain you want to DISPLAY (no scheme). If set, we show this even if Kutt returns a LAN link.
KUTT_DOMAIN = os.getenv("KUTT_DOMAIN") or os.getenv("KUTT_LINK_DOMAIN")
# Only send the 'domain' field to the API if explicitly forced.
KUTT_FORCE_DOMAIN = os.getenv("KUTT_FORCE_DOMAIN", "false").lower() in ("1", "true", "yes")

def _scheme(host: str) -> str:
    p = urlparse(host if "://" in host else f"http://{host}")
    return p.scheme or "http"

def _netloc(host: str) -> str:
    p = urlparse(host if "://" in host else f"http://{host}")
    return p.netloc or host

def _short_url_from_payload(d: dict) -> str:
    """
    Build the short URL to display.
    Preference:
      1) If KUTT_DOMAIN is set and we have 'address', use it (avoids showing LAN IPs).
      2) Otherwise use 'link'/'shortUrl' from Kutt if present.
      3) Fallback: host + address.
    """
    addr = d.get("address")
    dom = (KUTT_DOMAIN or "").replace("http://", "").replace("https://", "").strip().strip("/")
    if dom and addr:
        scheme = "https" if KUTT_HOST.startswith("https://") else "http"
        return f"{scheme}://{dom}/{addr}"

    for k in ("link", "shortUrl"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    if addr:
        return f"{_scheme(KUTT_HOST)}://{_netloc(KUTT_HOST)}/{addr}"

    return ""

class KuttCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="shorten", description="Shorten a URL using your Kutt instance.")
    @app_commands.describe(
        url="The URL to shorten",
        slug="Optional custom alias",
        password="Optional password",
        expire_in_days="Optional expiration in days (1..3650)",
    )
    async def shorten(
        self,
        inter: discord.Interaction,
        url: str,
        slug: str | None = None,
        password: str | None = None,
        expire_in_days: app_commands.Range[int, 1, 3650] | None = None,
    ):
        await inter.response.defer(ephemeral=True)

        if not KUTT_API:
            return await inter.followup.send("❌ Kutt API key not configured.", ephemeral=True)

        headers = {
            "X-API-KEY": KUTT_API,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        base_payload = {"target": url}
        if slug:
            base_payload["customurl"] = slug
        if password:
            base_payload["password"] = password
        if expire_in_days is not None:
            base_payload["expireIn"] = int(expire_in_days)

        async def _post(endpoint: str, payload: dict) -> tuple[int, dict]:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{KUTT_HOST}{endpoint}", json=payload, headers=headers, timeout=timeout) as r:
                    status = r.status
                    try:
                        data = await r.json()
                    except Exception:
                        data = {"error": (await r.text()) or f"HTTP {status}"}
                    return status, data

        payload = dict(base_payload)
        used_domain = False
        if KUTT_FORCE_DOMAIN and KUTT_DOMAIN:
            payload["domain"] = KUTT_DOMAIN
            used_domain = True

        # Try v3 first, then fallback to v2
        status, data = await _post("/api/v3/links", payload)
        if status in (404, 405):
            status, data = await _post("/api/v2/links", payload)

        # If domain causes permission issues, retry without it (same version)
        errmsg = (str(data.get("error", "")) if isinstance(data, dict) else "").lower()
        if (status < 200 or status >= 300) and used_domain and (
            "domain" in errmsg or "only users" in errmsg or status in (401, 403)
        ):
            status, data = await _post("/api/v3/links", base_payload)
            if status in (404, 405):
                status, data = await _post("/api/v2/links", base_payload)
            used_domain = False

        # Success if 2xx OR the payload looks like a link object
        looks_like_link = isinstance(data, dict) and any(k in data for k in ("address", "id", "link", "shortUrl", "target"))
        if (200 <= status < 300) or looks_like_link:
            short = _short_url_from_payload(data if isinstance(data, dict) else {})
            if short:
                return await inter.followup.send(f"Shortened: {short}", ephemeral=False)

        # Otherwise show a concise error
        hints = []
        msg = str(data)
        if status == 401 or "unauthorized" in msg.lower():
            hints.append("Check your API key.")
        if "exists" in msg.lower() and slug:
            hints.append("That slug may already be taken.")
        if used_domain and "domain" in msg.lower():
            hints.append("Your key may not be allowed to use that domain.")
        hint_txt = f"  ({' '.join(hints)})" if hints else ""
        await inter.followup.send(f"❌ Error from Kutt: {data}{hint_txt}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(KuttCog(bot))