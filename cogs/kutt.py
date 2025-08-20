import os, aiohttp
import discord
from discord.ext import commands
from discord import app_commands

class KuttCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="shorten", description="Shorten a URL with Kutt.")
    async def shorten(self, inter: discord.Interaction, url: str):
        await inter.response.defer()
        api_key = os.getenv("KUTT_API")
        if not api_key:
            return await inter.followup.send("❌ Kutt API key not configured.", ephemeral=True)
        payload = {"target": url}
        domain = os.getenv("KUTT_DOMAIN")
        if domain:
            payload["domain"] = domain
        headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://kutt.it/api/v2/links", json=payload, headers=headers) as r:
                    data = await r.json()
                    if r.status != 200:
                        return await inter.followup.send(f"❌ Error: {data}", ephemeral=True)
                    await inter.followup.send(f"🔗 {data.get('link')}")
        except Exception as e:
            await inter.followup.send(f"⚠️ Kutt error: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(KuttCog(bot))