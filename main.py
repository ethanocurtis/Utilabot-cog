import os
import asyncio
import discord
from discord.ext import commands
from utils.db import init_engine_and_session, run_migrations
from wx_store import WxStore

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

COGS = [
    "cogs.weather",
    "cogs.economy",
    "cogs.business",
    "cogs.games",
    "cogs.polls",
    "cogs.reminders",
    "cogs.notes",
    "cogs.kutt",
]

class UtilaBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        db_path = os.environ.get("DATA_PATH", "/app/data/bot.db")
        self.engine, self.SessionLocal = init_engine_and_session(db_path)

    async def setup_hook(self):
        # Ensure DB schema exists
        run_migrations(self.engine)
        # Load cogs
        for cog in COGS:
            await self.load_extension(cog)
        # Global sync
        await self.tree.sync()

bot = UtilaBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set")
    bot.run(token)
