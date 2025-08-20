import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy.orm import Session
from utils.db import Note

class NotesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="note_add", description="Add a personal note.")
    async def note_add(self, inter: discord.Interaction, text: str):
        with self.bot.SessionLocal() as s:
            s.add(Note(user_id=inter.user.id, text=text)); s.commit()
        await inter.response.send_message("📝 Note saved.")

    @app_commands.command(name="note_list", description="List your notes.")
    async def note_list(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            notes = s.query(Note).filter_by(user_id=inter.user.id).order_by(Note.created_at.desc()).limit(10).all()
        if not notes:
            return await inter.response.send_message("No notes.")
        desc = "\n".join([f"- [{n.id}] {n.text}" for n in notes])
        await inter.response.send_message(embed=discord.Embed(title="🗒️ Notes (latest 10)", description=desc))

    @app_commands.command(name="note_del", description="Delete a note by ID.")
    async def note_del(self, inter: discord.Interaction, note_id: int):
        with self.bot.SessionLocal() as s:
            n = s.get(Note, note_id)
            if not n or n.user_id != inter.user.id:
                return await inter.response.send_message("Not found.", ephemeral=True)
            s.delete(n); s.commit()
        await inter.response.send_message("🗑️ Deleted.")

async def setup(bot: commands.Bot):
    await bot.add_cog(NotesCog(bot))
