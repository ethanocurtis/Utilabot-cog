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

    # ---- helpers ----------------------------------------------------------

    def _next_note_no(self, s: Session, user_id: int) -> int:
        """
        Compute the smallest missing positive integer for this user's notes,
        based on notes.note_no (falls back to nothing if the column doesn't exist).
        """
        # Pull existing per-user numbers; ignore NULLs
        rows = s.execute(
            text("SELECT note_no FROM notes WHERE user_id=:u AND note_no IS NOT NULL ORDER BY note_no ASC"),
            {"u": user_id}
        ).fetchall()
        used = {r[0] for r in rows if isinstance(r[0], int)}
        n = 1
        while n in used:
            n += 1
        return n

    def _has_note_no_column(self, s: Session) -> bool:
        # Portable-ish check for SQLite; harmless elsewhere
        try:
            s.execute(text("SELECT note_no FROM notes LIMIT 0"))
            return True
        except Exception:
            return False

    # ---- commands ---------------------------------------------------------

    @app_commands.command(name="note_add", description="Add a personal note (per-user smallest available number).")
    async def note_add(self, inter: discord.Interaction, text_: str):
        with self.bot.SessionLocal() as s:
            has_col = self._has_note_no_column(s)
            note_no = self._next_note_no(s, inter.user.id) if has_col else None

            # Insert the row; use ORM for base fields
            n = Note(user_id=inter.user.id, text=text_)
            s.add(n)
            s.flush()  # get row id

            # If we have the note_no column, set it now
            if has_col and note_no is not None:
                s.execute(
                    text("UPDATE notes SET note_no=:nn WHERE id=:id"),
                    {"nn": note_no, "id": n.id}
                )

            s.commit()

        # Prefer showing the per-user number if we could set it
        label = f"[{note_no}]" if note_no is not None else f"[{n.id}]"
        await inter.response.send_message(f"📝 Note saved {label}.")

    @app_commands.command(name="note_list", description="List your notes (shows per-user numbers).")
    async def note_list(self, inter: discord.Interaction):
        with self.bot.SessionLocal() as s:
            # Pull latest 10; include note_no if present
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
                    {"u": inter.user.id}
                ).fetchall()
            else:
                # Fallback: no note_no column yet
                rows = s.execute(
                    text("""
                        SELECT id, text, created_at, NULL as note_no
                        FROM notes
                        WHERE user_id=:u
                        ORDER BY created_at DESC
                        LIMIT 10
                    """),
                    {"u": inter.user.id}
                ).fetchall()

        if not rows:
            return await inter.response.send_message("No notes.")

        lines = []
        for rid, rtext, _rc, rno in rows:
            tag = rno if rno is not None else rid
            lines.append(f"- [{tag}] {rtext}")

        embed = discord.Embed(title="🗒️ Notes (latest 10)", description="\n".join(lines))
        await inter.response.send_message(embed=embed)

    @app_commands.command(name="note_del", description="Delete a note by its per-user number.")
    async def note_del(self, inter: discord.Interaction, note_id: int):
        """
        We interpret note_id as the per-user number (note_no).
        If none is found (e.g., before migration), fall back to global id.
        """
        with self.bot.SessionLocal() as s:
            has_col = self._has_note_no_column(s)
            target = None

            if has_col:
                # Try per-user numeric id first
                row = s.execute(
                    text("SELECT id FROM notes WHERE user_id=:u AND note_no=:n"),
                    {"u": inter.user.id, "n": note_id}
                ).fetchone()
                if row:
                    target = s.get(Note, row[0])

            # Fallback: try global PK
            if target is None:
                target = s.get(Note, note_id)
                if target and target.user_id != inter.user.id:
                    target = None

            if not target:
                return await inter.response.send_message("Not found.", ephemeral=True)

            s.delete(target)
            s.commit()

        await inter.response.send_message("🗑️ Deleted.")

async def setup(bot: commands.Bot):
    await bot.add_cog(NotesCog(bot))