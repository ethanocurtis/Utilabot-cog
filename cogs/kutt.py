# cogs/kutt.py
import os, aiohttp
import discord
from discord.ext import commands
from discord import app_commands

KUTT_HOST   = os.getenv("KUTT_HOST", "https://kutt.it").rstrip("/")
KUTT_API    = os.getenv("KUTT_API")  # prefer a *user* API key on self-hosted
KUTT_DOMAIN = os.getenv("KUTT_DOMAIN")  # optional custom domain

class KuttCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="shorten", description="Shorten a URL using your Kutt instance.")
    @app_commands.describe(
        url="The URL to shorten",
        slug="Optional custom alias (requires user API key / permissions)",
        password="Optional password to protect the link",
        expire_in_days="Optional expiration in days (integer)"
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
            return await inter.followup.send(
                "❌ Kutt API key not configured. Set **KUTT_API** in your env.",
                ephemeral=True,
            )

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
                    f"{KUTT_HOST}/api/v2/links",
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                ) as r:
                    try:
                        data = await r.json()
                    except Exception:
                        data = {"error": (await r.text()) or f"HTTP {r.status}"}
                    return r.status, data

        # Try with domain if provided
        payload = dict(base_payload)
        used_domain = False
        if KUTT_DOMAIN:
            payload["domain"] = KUTT_DOMAIN
            used_domain = True

        status, data = await create_link(payload)

        # Auto-retry if domain is not allowed for the current key
        if (
            status != 200
            and isinstance(data, dict)
            and str(data.get("error", "")).strip().lower() == "only users can use this field."
            and used_domain
        ):
            status, data = await create_link(base_payload)  # retry without domain
            used_domain = False

        if status != 200:
            # Helpful hints
            hints = []
            msg = str(data)
            if status == 401 or "unauthorized" in msg.lower():
                hints.append("Check **KUTT_API**.")
            if "exists" in msg.lower() and slug:
                hints.append("That **slug** may already be taken.")
            if used_domain and "domain" in msg.lower():
                hints.append("Your key may not be a *user* key, or that domain isn’t allowed.")
            hint_text = f"\n*Hint:* {' '.join(hints)}" if hints else ""
            return await inter.followup.send(f"❌ Error from Kutt: `{data}`{hint_text}", ephemeral=True)

        short = data.get("link") or data.get("shortUrl") or "(no link)"
        target = data.get("target") or url
        await inter.followup.send(f"🔗 **{short}** → {target}", ephemeral=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(KuttCog(bot))