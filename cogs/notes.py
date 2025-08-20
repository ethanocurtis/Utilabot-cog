# cogs/notes.py
from __future__ import annotations
import datetime as dt
import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy.orm import Session
from sqlalchemy import text
from utils.db import Note

class NotesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ----------------- helpers -----------------

    def _has_note_no_column(self, s: Session) -> bool:
        try:
            s.execute(text("SELECT note_no FROM notes LIMIT 0"))
            return True
        except Exception:
            return False

    def _next_note_no(self, s: Session, user_id: int) -> int:
        rows = s.execute(
            text("SELECT note_no FROM notes WHERE user_id=:u AND note_no IS NOT NULL ORDER BY note_no ASC"),
            {"u": user_id},
        ).fetchall()
        used = {r[0] for r in rows if isinstance(r[0], int)}
        n = 1
        while n in used:
            n += 1
        return n

    # ----------------- commands -----------------

    @app_commands.command(name="note_add", description="Add a personal note (uses the lowest available number).")
    async def note_add(self, inter: discord.Interaction, text_: str):
        with self.bot.SessionLocal() as s:
            has_col = self._has_note_no_column(s)
            note_no = self._next_note_no(s, inter.user.id) if has_col else None

            n = Note(user_id=inter.user.id, text=text_)
            s.add(n)
            s.flush()  # get PK

            if has_col and note_no is not None:
                s.execute(text("UPDATE notes SET note_no=:nn WHERE id=:id"), {"nn": note_no, "id": n.id})

            s.commit()

        label = f"{note_no}." if note_no is not None else f"{n.id}."
        await inter.response.send_message(f"📝 Note saved {label} {text_}")

    @app_commands.command(name="note_list", description="List your notes (numbered 1., 2., …).")
    async def note_list(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            has_col = self._has_note_no_column(s)
            if has_col:
                rows = s.execute(
                    text("""
                        SELECT id, text, created_at, note_no
                        FROM notes
                        WHERE user_id=:u
                        ORDER BY created_at DESC
                        LIMIT 10
                    """),
                    {"u": inter.user.id},
                ).fetchall()
            else:
                rows = s.execute(
                    text("""
                        SELECT id, text, created_at, NULL as note_no
                        FROM notes
                        WHERE user_id=:u
                        ORDER BY created_at DESC
                        LIMIT 10
                    """),
                    {"u": inter.user.id},
                ).fetchall()

        if not rows:
            return await inter.response.send_message("No notes.")

        lines = []
        for rid, rtext, _rc, rno in rows:
            num = rno if rno is not None else rid  # show as "1. text"
            lines.append(f"{num}. {rtext}")

        embed = discord.Embed(title="🗒️ Your Notes (latest 10)", description="\n".join(lines))
        await inter.response.send_message(embed=embed)

    @app_commands.command(name="note_del", description="Delete a note by its number (as shown in the list).")
    async def note_del(self, inter: discord.Interaction, number: int):
        """
        Treats 'number' as per-user note_no if available; falls back to global id.
        """
        with self.bot.SessionLocal() as s:
            has_col = self._has_note_no_column(s)
            target = None

            if has_col:
                row = s.execute(
                    text("SELECT id FROM notes WHERE user_id=:u AND note_no=:n"),
                    {"u": inter.user.id, "n": number},
                ).fetchone()
                if row:
                    target = s.get(Note, row[0])

            if target is None:
                target = s.get(Note, number)
                if target and target.user_id != inter.user.id:
                    target = None

            if not target:
                return await inter.response.send_message("Not found.", ephemeral=True)

            s.delete(target)
            s.commit()

        await inter.response.send_message("🗑️ Deleted.")

async def setup(bot: commands.Bot):
    await bot.add_cog(NotesCog(bot))