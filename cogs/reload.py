import os
import importlib
import discord
from discord.ext import commands
from discord import app_commands

from cogs.admin_gates import gated


class Reload(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="reload",
        description="Reload a cog from cogs/ and resync slash commands (global + this guild)."
    )
    @app_commands.describe(cog="Cog file name (without .py)")
    @gated()
    async def reload(self, interaction: discord.Interaction, cog: str):
        await interaction.response.defer(thinking=True, ephemeral=True)

        module = f"cogs.{cog}"
        action = None

        try:
            # Make sure Python import caches are fresh (useful during active dev)
            importlib.invalidate_caches()

            try:
                # Try a hot reload first
                await self.bot.reload_extension(module)
                action = "Reloaded"
            except commands.ExtensionNotLoaded:
                # Not loaded yet? Load it.
                await self.bot.load_extension(module)
                action = "Loaded"

            # ---- Sync logic ----
            # 1) Always sync globally (this is the source of truth)
            global_synced = await self.bot.tree.sync()

            # 2) If the command ran in a guild, force a clean guild sync:
            #    - Clear guild commands (so we don't accumulate old copies)
            #    - Copy the global tree to this guild
            #    - Sync that guild
            guild_synced = []
            scope_msg = "globally"
            if interaction.guild:
                guild_obj = interaction.guild
                self.bot.tree.clear_commands(guild=guild_obj)
                self.bot.tree.copy_global_to(guild=guild_obj)
                guild_synced = await self.bot.tree.sync(guild=guild_obj)
                scope_msg = f"globally and in guild {guild_obj.id}"

            await interaction.followup.send(
                (
                    f"✅ {action} `{cog}`.\n"
                    f"• Global sync: **{len(global_synced)}** command(s)\n"
                    f"• Guild sync: **{len(guild_synced)}** command(s){' (this was a DM, so 0 by design)' if not interaction.guild else ''}"
                ),
                ephemeral=True,
            )

        except commands.ExtensionNotFound:
            await interaction.followup.send(
                f"❌ Cog `{cog}` not found in `cogs/`.", ephemeral=True
            )
        except commands.NoEntryPointError:
            await interaction.followup.send(
                f"❌ Cog `{cog}` has no `setup(bot)`.", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"💥 Failed to reload `{cog}`: `{e}`", ephemeral=True
            )

    @reload.autocomplete("cog")
    async def cog_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            files = [
                f[:-3] for f in os.listdir("cogs")
                if f.endswith(".py") and f != "__init__.py"
            ]
        except FileNotFoundError:
            files = []
        return [
            app_commands.Choice(name=n, value=n)
            for n in files
            if current.lower() in n.lower()
        ]


async def setup(bot: commands.Bot):
    await bot.add_cog(Reload(bot))