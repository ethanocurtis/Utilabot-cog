import discord
from discord.ext import commands
from discord import app_commands

class PollsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="poll", description="Create a 2-option poll.")
    async def poll(self, inter: discord.Interaction, question: str, option1: str, option2: str):
        emb = discord.Embed(title="📊 Poll", description=question)
        emb.add_field(name="Options", value=f"1️⃣ {option1}\n2️⃣ {option2}")
        msg = await inter.channel.send(embed=emb)
        await msg.add_reaction("1️⃣")
        await msg.add_reaction("2️⃣")
        await inter.response.send_message("✅ Poll created!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(PollsCog(bot))
