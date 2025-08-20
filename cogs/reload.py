import discord
from discord.ext import commands
from discord import app_commands
import os

GUILD_ID = 1327867158188916796

class Reload(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="reload", description="Reload a cog from the cogs folder")
    @app_commands.describe(cog="Which cog would you like to reload?")
    async def reload(self, interaction: discord.Interaction, cog: str):
        """Reloads a selected cog and resyncs commands."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            full_cog = f"cogs.{cog}"
            await self.bot.reload_extension(full_cog)

            guild = discord.Object(id=GUILD_ID)
            synced = await self.bot.tree.sync(guild=guild)

            await interaction.followup.send(
                f"✅ Reloaded `{cog}` and synced {len(synced)} command(s): {[cmd.name for cmd in synced]}",
                ephemeral=True
            )
        except commands.ExtensionNotLoaded:
            await interaction.followup.send(f"⚠️ Cog `{cog}` was not loaded.", ephemeral=True)
        except commands.ExtensionNotFound:
            await interaction.followup.send(f"❌ Cog `{cog}` does not exist.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"💥 Failed to reload `{cog}`: `{e}`", ephemeral=True)

    @reload.autocomplete("cog")
    async def cog_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        """Auto-complete cog names by scanning the cogs folder."""
        files = [
            f[:-3] for f in os.listdir("cogs")
            if f.endswith(".py") and f != "__init__.py"
        ]
        return [
            app_commands.Choice(name=f, value=f)
            for f in files if current.lower() in f.lower()
        ]


async def setup(bot: commands.Bot):
    cog = Reload(bot)
    await bot.add_cog(cog)

    guild = discord.Object(id=GUILD_ID)

    for command in cog.get_app_commands():
        bot.tree.add_command(command, guild=guild)

    print("Cog commands registered:", [cmd.name for cmd in cog.get_app_commands()])