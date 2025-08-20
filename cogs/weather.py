import re, aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy.orm import Session
from utils.db import Reminder  # not used here but placeholder
from utils.common import ensure_user
from utils.db import User

HTTP_HEADERS = {"User-Agent": "UtilaBot/1.0", "Accept": "application/json"}

class WeatherCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="weather", description="Current weather by ZIP (Open-Meteo).")
    async def weather(self, inter: discord.Interaction, zip: str):
        await inter.response.defer()
        if not re.fullmatch(r"\d{5}", zip):
            return await inter.followup.send("Please provide a 5-digit US ZIP.", ephemeral=True)

        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                async with session.get(f"https://api.zippopotam.us/us/{zip}") as r:
                    if r.status != 200:
                        return await inter.followup.send("ZIP lookup failed.", ephemeral=True)
                    data = await r.json()
                place = data["places"][0]
                lat = float(place["latitude"]); lon = float(place["longitude"])
                city = place["place name"]; state = place["state abbreviation"]
                params = {
                    "latitude": lat, "longitude": lon,
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "timezone": "auto",
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation,weather_code"
                }
                async with session.get("https://api.open-meteo.com/v1/forecast", params=params) as r2:
                    if r2.status != 200:
                        return await inter.followup.send("Weather service unavailable.", ephemeral=True)
                    wx = await r2.json()
            cur = wx.get("current", {})
            t = cur.get("temperature_2m")
            feels = cur.get("apparent_temperature", t)
            rh = cur.get("relative_humidity_2m")
            wind = cur.get("wind_speed_10m")
            pcp = cur.get("precipitation", 0.0)

            emb = discord.Embed(title=f"Weather — {city}, {state} {zip}")
            emb.add_field(name="Temp", value=f"{round(t)}°F (feels {round(feels)}°)" if t is not None else "n/a")
            if rh is not None: emb.add_field(name="Humidity", value=f"{int(rh)}%")
            if wind is not None: emb.add_field(name="Wind", value=f"{round(wind)} mph")
            emb.add_field(name="Precip (now)", value=f"{pcp:.2f} in")
            await inter.followup.send(embed=emb)
        except Exception as e:
            await inter.followup.send(f"⚠️ Weather error: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(WeatherCog(bot))
