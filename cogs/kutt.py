# cogs/kutt.py
import os, aiohttp
import discord
from discord.ext import commands
from discord import app_commands

# Back-compat: accept both old and new names
KUTT_HOST   = (os.getenv("KUTT_HOST") or os.getenv("KUTT_BASE_URL") or "https://kutt.it").rstrip("/")
KUTT_API    =  os.getenv("KUTT_API") or os.getenv("KUTT_API_KEY")
KUTT_DOMAIN =  os.getenv("KUTT_DOMAIN") or os.getenv("KUTT_LINK_DOMAIN")  # domain only if forced
KUTT_FORCE_DOMAIN = (os.getenv("KUTT_FORCE_DOMAIN", "false").lower() in ("1","true","yes"))

class KuttCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="shorten", description="Shorten a URL using your Kutt instance.")
    @app_commands.describe(
        url="URL to shorten",
        slug="Optional custom alias",
        password="Optional password",
        expire_in_days="Optional expiration in days"
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
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{KUTT_HOST}/api/v2/links",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    try:
                        data = await r.json()
                    except Exception:
                        data = {"error": (await r.text()) or f"HTTP {r.status}"}
                    return r.status, data

        # Build payload: only include domain if explicitly forced
        used_domain = False
        payload = dict(base_payload)
        if KUTT_FORCE_DOMAIN and KUTT_DOMAIN:
            payload["domain"] = KUTT_DOMAIN
            used_domain = True

        status, data = await create_link(payload)

        # If domain caused trouble, retry without it
        err = (str(data.get("error", "")) if isinstance(data, dict) else "").lower()
        if status != 200 and used_domain and ("only users" in err or "domain" in err or status in (401,403)):
            status, data = await create_link(base_payload)
            used_domain = False

        if status != 200:
            hints = []
            msg = str(data)
            if status == 401 or "unauthorized" in msg.lower():
                hints.append("Check your API key.")
            if "exists" in msg.lower() and slug:
                hints.append("That slug may already be taken.")
            if used_domain and "domain" in msg.lower():
                hints.append("Your key may not be allowed to use that domain.")
            hint_text = f"\n*Hint:* {' '.join(hints)}" if hints else ""
            return await inter.followup.send(f"❌ Error from Kutt: `{data}`{hint_text}", ephemeral=True)

        short = data.get("link") or data.get("shortUrl") or "(no link)"
        target = data.get("target") or url
        await inter.followup.send(f"🔗 **{short}** → {target}", ephemeral=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(KuttCog(bot))