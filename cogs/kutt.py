# # cogs/kutt.py
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
KUTT_DOMAIN = os.getenv("KUTT_DOMAIN") or os.getenv("KUTT_LINK_DOMAIN")  # e.g. glint.zip (NO scheme)
KUTT_FORCE_DOMAIN = os.getenv("KUTT_FORCE_DOMAIN", "false").lower() in ("1", "true", "yes")

def _scheme_from_host(host: str) -> str:
    p = urlparse(host if "://" in host else f"http://{host}")
    return p.scheme or "http"

def _netloc_from_host(host: str) -> str:
    p = urlparse(host if "://" in host else f"http://{host}")
    return p.netloc or host

def _build_short_url(data: dict) -> str:
    """
    Construct a short URL from Kutt response.
    Prefer 'link' / 'shortUrl' when provided. Otherwise build from 'address'.
    """
    # 1) Direct link fields
    for k in ("link", "shortUrl"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # 2) Build from address
    address = data.get("address")
    if not address:
        return ""

    if KUTT_DOMAIN:
        scheme = "https" if KUTT_HOST.startswith("https://") else "http"
        return f"{scheme}://{KUTT_DOMAIN.strip('/')}/{address}"

    # Fall back to the host we called
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

        async def post_links(endpoint: str, payload: dict) -> tuple[int, dict]:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{KUTT_HOST}{endpoint}", json=payload, headers=headers, timeout=timeout) as r:
                    status = r.status
                    try:
                        data = await r.json()
                    except Exception:
                        data = {"error": (await r.text()) or f"HTTP {status}"}
                    return status, data

        # Build initial payload (domain only if explicitly forced)
        payload = dict(base_payload)
        used_domain = False
        if KUTT_FORCE_DOMAIN and KUTT_DOMAIN:
            payload["domain"] = KUTT_DOMAIN
            used_domain = True

        # Try v3, then v2 if v3 unsupported
        status, data = await post_links("/api/v3/links", payload)
        if status in (404, 405):  # v3 not present on some installs
            status, data = await post_links("/api/v2/links", payload)

        # If domain triggers a permission error, retry without it (same endpoint/version)
        err_text = (str(data.get("error", "")) if isinstance(data, dict) else "").lower()
        if (status < 200 or status >= 300) and used_domain and ("only users" in err_text or "domain" in err_text or status in (401, 403)):
            status, data = await post_links("/api/v3/links", base_payload)
            if status in (404, 405):
                status, data = await post_links("/api/v2/links", base_payload)
            used_domain = False

        # Treat any 2xx OR any payload with 'address'/'link' as success
        is_success_status = 200 <= status < 300
        has_link_shape = isinstance(data, dict) and (data.get("address") or data.get("link") or data.get("shortUrl"))
        if is_success_status or has_link_shape:
            short_url = _build_short_url(data if isinstance(data, dict) else {})
            if short_url:
                target = (data or {}).get("target") if isinstance(data, dict) else None
                await inter.followup.send(f"🔗 **{short_url}** → {target or url}", ephemeral=False)
                return
            # fallthrough to show data if somehow we couldn't build it

        # Error messaging
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