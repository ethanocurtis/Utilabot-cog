import os
import discord
from discord.ext import commands
from discord import app_commands

class Reload(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="reload", description="Reload a cog (auto-detects cogs/; syncs this guild or global)")
    @app_commands.describe(cog="Cog file name (without .py)")
    async def reload(self, interaction: discord.Interaction, cog: str):
        await interaction.response.defer(thinking=True, ephemeral=True)

        module = f"cogs.{cog}"
        try:
            try:
                await self.bot.reload_extension(module)
                action = "Reloaded"
            except commands.ExtensionNotLoaded:
                await self.bot.load_extension(module)
                action = "Loaded"

            # Sync where the command ran (guild) or globally in DMs
            if interaction.guild_id:
                synced = await self.bot.tree.sync(guild=discord.Object(id=interaction.guild_id))
                scope = f"guild {interaction.guild_id}"
            else:
                synced = await self.bot.tree.sync()
                scope = "globally"

            await interaction.followup.send(
                f"✅ {action} `{cog}`. Synced {len(synced)} command(s) {scope}.",
                ephemeral=True
            )
        except commands.ExtensionNotFound:
            await interaction.followup.send(f"❌ Cog `{cog}` not found in `cogs/`.", ephemeral=True)
        except commands.NoEntryPointError:
            await interaction.followup.send(f"❌ Cog `{cog}` has no `setup(bot)`.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"💥 Failed to reload `{cog}`: `{e}`", ephemeral=True)

    @reload.autocomplete("cog")
    async def cog_autocomplete(self, interaction: discord.Interaction, current: str):
        files = [f[:-3] for f in os.listdir("cogs") if f.endswith(".py") and f != "__init__.py"]
        return [app_commands.Choice(name=n, value=n) for n in files if current.lower() in n.lower()]

async def setup(bot: commands.Bot):
    await bot.add_cog(Reload(bot))