# cogs/kutt.py
from __future__ import annotations
import os
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from urllib.parse import urlparse

# --- Env (backward compatible with your old .env) ---
KUTT_HOST = (os.getenv("KUTT_HOST") or os.getenv("KUTT_BASE_URL") or "https://kutt.it").rstrip("/")
KUTT_API = os.getenv("KUTT_API") or os.getenv("KUTT_API_KEY")
KUTT_DOMAIN = os.getenv("KUTT_DOMAIN") or os.getenv("KUTT_LINK_DOMAIN")  # domain name only, e.g. glint.zip
KUTT_FORCE_DOMAIN = os.getenv("KUTT_FORCE_DOMAIN", "false").lower() in ("1", "true", "yes")

def _scheme_from_host(host: str) -> str:
    parsed = urlparse(host if "://" in host else f"http://{host}")
    return parsed.scheme or "http"

def _netloc_from_host(host: str) -> str:
    parsed = urlparse(host if "://" in host else f"http://{host}")
    return parsed.netloc or host

def _build_short_url(data: dict) -> str:
    """
    Construct a short URL from Kutt response.
    Prefer 'link' if present; otherwise build from domain+address.
    """
    # 1) If Kutt already gave us the final link, use it.
    link = data.get("link") or data.get("shortUrl")
    if isinstance(link, str) and link.strip():
        return link

    # 2) Build it from 'address'
    address = data.get("address")
    if not address:
        # last fallback: show raw data (caller will handle as error)
        return ""

    # Prefer configured domain if provided
    if KUTT_DOMAIN:
        scheme = "https" if KUTT_HOST.startswith("https://") else "http"
        return f"{scheme}://{KUTT_DOMAIN.strip('/')}/{address}"

    # Otherwise fall back to the host we're calling
    netloc = _netloc_from_host(KUTT_HOST)
    scheme = _scheme_from_host(KUTT_HOST)
    return f"{scheme}://{netloc}/{address}"

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
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        base_payload = {"target": url}
        if slug:
            base_payload["customurl"] = slug
        if password:
            base_payload["password"] = password
        if expire_in_days is not None:
            base_payload["expireIn"] = int(expire_in_days)

        async def create_link(payload: dict) -> tuple[int, dict]:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{KUTT_HOST}/api/v2/links", json=payload, headers=headers, timeout=timeout
                ) as r:
                    try:
                        data = await r.json()
                    except Exception:
                        data = {"error": (await r.text()) or f"HTTP {r.status}"}
                    return r.status, data

        # Try with domain only if explicitly forced (mirrors your old setup)
        payload = dict(base_payload)
        used_domain = False
        if KUTT_FORCE_DOMAIN and KUTT_DOMAIN:
            payload["domain"] = KUTT_DOMAIN
            used_domain = True

        status, data = await create_link(payload)

        # If domain causes a permission error, retry without it
        err_text = (str(data.get("error", "")) if isinstance(data, dict) else "").lower()
        if status != 200 and used_domain and ("only users" in err_text or "domain" in err_text or status in (401, 403)):
            status, data = await create_link(base_payload)
            used_domain = False

        # Success path: Kutt may return the object (id/address/target) even without 'link'
        if status == 200 and isinstance(data, dict) and not data.get("error"):
            short_url = _build_short_url(data)
            if short_url:
                target = data.get("target") or url
                return await inter.followup.send(f"🔗 **{short_url}** → {target}", ephemeral=False)
            # If we couldn't build, fall through to error messaging below with raw data

        # Error path
        hints = []
        msg = str(data)
        if status == 401 or "unauthorized" in msg.lower():
            hints.append("Check your API key.")
        if "exists" in msg.lower() and slug:
            hints.append("That slug may already be taken.")
        if used_domain and "domain" in msg.lower():
            hints.append("Your key may not be allowed to use that domain.")
        hint_text = f"\n*Hint:* {' '.join(hints)}" if hints else ""
        await inter.followup.send(f"❌ Error from Kutt: `{data}`{hint_text}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(KuttCog(bot))