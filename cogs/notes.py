# cogs/notes.py
from __future__ import annotations
from typing import List, Tuple, Optional, Dict
import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy.orm import Session
from sqlalchemy import text
from utils.db import Note

# ---------- DB helpers ----------

def has_note_no_column(s: Session) -> bool:
    try:
        s.execute(text("SELECT note_no FROM notes LIMIT 0"))
        return True
    except Exception:
        return False

def next_note_no(s: Session, user_id: int) -> int:
    rows = s.execute(
        text("SELECT note_no FROM notes WHERE user_id=:u AND note_no IS NOT NULL ORDER BY note_no ASC"),
        {"u": user_id},
    ).fetchall()
    used = {r[0] for r in rows if isinstance(r[0], int)}
    n = 1
    while n in used:
        n += 1
    return n

def fetch_user_notes(s: Session, user_id: int) -> List[Tuple[int, Optional[int], str]]:
    """
    Returns list of (id, note_no, text) ordered by stable display number:
      - rows with note_no first, ascending
      - legacy rows without note_no next, ascending id
    Limited to 25 for a single Discord select.
    """
    if has_note_no_column(s):
        with_no = s.execute(
            text("""SELECT id, note_no, text
                    FROM notes
                    WHERE user_id=:u AND note_no IS NOT NULL
                    ORDER BY note_no ASC
                    LIMIT 50"""),
            {"u": user_id},
        ).fetchall()
        legacy = s.execute(
            text("""SELECT id, NULL as note_no, text
                    FROM notes
                    WHERE user_id=:u AND note_no IS NULL
                    ORDER BY id ASC
                    LIMIT 50"""),
            {"u": user_id},
        ).fetchall()
        rows = list(with_no) + list(legacy)
    else:
        rows = s.execute(
            text("""SELECT id, NULL as note_no, text
                    FROM notes
                    WHERE user_id=:u
                    ORDER BY id ASC
                    LIMIT 50"""),
            {"u": user_id},
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows][:25]

def load_note(s: Session, user_id: int, *, by_note_no: Optional[int] = None, by_id: Optional[int] = None) -> Optional[Note]:
    if by_note_no is not None and has_note_no_column(s):
        row = s.execute(
            text("SELECT id FROM notes WHERE user_id=:u AND note_no=:n"),
            {"u": user_id, "n": by_note_no},
        ).fetchone()
        if row:
            n = s.get(Note, row[0])
            return n
    if by_id is not None:
        n = s.get(Note, by_id)
        if n and n.user_id == user_id:
            return n
    return None

# ---------- UI Components ----------

class EditNoteModal(discord.ui.Modal, title="Edit Note"):
    def __init__(self, view: "NotesView", note_id: int, current_text: str):
        super().__init__(timeout=180)
        self.view_ref = view
        self.note_id = note_id
        self.new_text = discord.ui.TextInput(
            label="Note text",
            style=discord.TextStyle.paragraph,
            default=current_text[:4000],
            max_length=4000,
            required=True,
        )
        self.add_item(self.new_text)

    async def on_submit(self, interaction: discord.Interaction):
        with self.view_ref.bot.SessionLocal() as s:
            n = s.get(Note, self.note_id)
            if not n or n.user_id != interaction.user.id:
                return await interaction.response.send_message("Not found.", ephemeral=True)
            n.text = str(self.new_text.value)
            s.commit()

        # Modal interactions don't have an attached message; defer and edit via followup
        await interaction.response.defer(ephemeral=True)
        await self.view_ref.refresh_options(interaction)
        await interaction.followup.send("✅ Updated.", ephemeral=True)

class AddNoteModal(discord.ui.Modal, title="Add Note"):
    def __init__(self, view: "NotesView"):
        super().__init__(timeout=180)
        self.view_ref = view
        self.text_in = discord.ui.TextInput(
            label="Note text",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.add_item(self.text_in)

    async def on_submit(self, interaction: discord.Interaction):
        with self.view_ref.bot.SessionLocal() as s:
            nno = next_note_no(s, self.view_ref.user_id) if has_note_no_column(s) else None
            n = Note(user_id=self.view_ref.user_id, text=str(self.text_in.value))
            s.add(n)
            s.flush()
            if nno is not None:
                s.execute(text("UPDATE notes SET note_no=:nn WHERE id=:id"), {"nn": nno, "id": n.id})
            s.commit()

        # Modal path: defer then edit the stored message via followup
        await interaction.response.defer(ephemeral=True)
        await self.view_ref.refresh_options(interaction)
        shown = nno if nno is not None else n.id
        await interaction.followup.send(f"📝 Added note {shown}.", ephemeral=True)

class NotesSelect(discord.ui.Select):
    def __init__(self, view: "NotesView", options: List[discord.SelectOption], id_map: Dict[str, int]):
        super().__init__(placeholder="Select a note…", min_values=1, max_values=1, options=options)
        self.view_ref = view
        self.id_map = id_map

    async def callback(self, interaction: discord.Interaction):
        note_key = self.values[0]
        note_id = self.id_map.get(note_key)
        if note_id is None:
            return await interaction.response.send_message("Unknown note.", ephemeral=True)
        self.view_ref.selected_note_id = note_id
        await self.view_ref.show_selected(interaction)

class NotesView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        self.select: Optional[NotesSelect] = None
        self.selected_note_id: Optional[int] = None
        self.delete_confirm: bool = False
        self.message_id: Optional[int] = None  # 🟢 store original message id for modal edits

    # ----- building options -----
    def _build_options(self) -> Tuple[List[discord.SelectOption], Dict[str, int]]:
        with self.bot.SessionLocal() as s:
            rows = fetch_user_notes(s, self.user_id)  # (id, note_no, text)
        options: List[discord.SelectOption] = []
        id_map: Dict[str, int] = {}
        for nid, nno, txt in rows:
            num = nno if nno is not None else nid
            key = str(nid)
            label = f"{num}. {txt[:80] or '(empty)'}"
            desc = f"Note #{num}"
            options.append(discord.SelectOption(label=label[:100], value=key, description=desc[:100]))
            id_map[key] = nid
        if not options:
            options.append(discord.SelectOption(label="(no notes yet)", value="__none__", description="Use Add Note"))
        return options, id_map

    async def refresh_options(self, interaction: discord.Interaction):
        options, id_map = self._build_options()
        # Replace select component
        for item in list(self.children):
            if isinstance(item, NotesSelect):
                self.remove_item(item)
        self.select = NotesSelect(self, options, id_map)
        self.select.disabled = (len(options) == 1 and options[0].value == "__none__")
        self.delete_button.disabled = True
        self.edit_button.disabled = True
        self.delete_confirm = False
        self.add_item(self.select)

        # If we're in a component interaction (button/select), we can edit that message directly
        if getattr(interaction, "message", None) is not None and not interaction.response.is_done():
            await interaction.response.edit_message(view=self)
            return

        # Otherwise (modal), edit the original /notes message via followup + stored message_id
        if self.message_id is not None:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await interaction.followup.edit_message(self.message_id, view=self)

    async def show_selected(self, interaction: discord.Interaction):
        """Show the selected note in the message embed and enable buttons."""
        if self.selected_note_id is None:
            return
        with self.bot.SessionLocal() as s:
            n = s.get(Note, self.selected_note_id)
            if not n or n.user_id != self.user_id:
                return await interaction.response.send_message("Not found.", ephemeral=True)

            # Find stable number to display
            num = None
            if has_note_no_column(s):
                row = s.execute(text("SELECT note_no FROM notes WHERE id=:i"), {"i": n.id}).fetchone()
                num = row[0] if row and isinstance(row[0], int) else None
            display_num = num if num is not None else n.id

        embed = discord.Embed(
            title=f"🗒️ Note {display_num}",
            description=n.text or "(empty)",
            color=discord.Color.blurple(),
        )
        self.edit_button.disabled = False
        self.delete_button.disabled = False
        self.delete_button.label = "Delete"
        self.delete_confirm = False
        await interaction.response.edit_message(embed=embed, view=self)

    # ----- Buttons -----
    @discord.ui.button(label="Add Note", style=discord.ButtonStyle.success)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddNoteModal(self))

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, disabled=True)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_note_id:
            return await interaction.response.send_message("Select a note first.", ephemeral=True)
        with self.bot.SessionLocal() as s:
            n = s.get(Note, self.selected_note_id)
            if not n or n.user_id != self.user_id:
                return await interaction.response.send_message("Not found.", ephemeral=True)
            await interaction.response.send_modal(EditNoteModal(self, n.id, n.text or ""))

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, disabled=True)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_note_id:
            return await interaction.response.send_message("Select a note first.", ephemeral=True)

        # two-step confirm toggle
        if not self.delete_confirm:
            self.delete_confirm = True
            self.delete_button.label = "Confirm Delete"
            await interaction.response.edit_message(view=self)
            return

        with self.bot.SessionLocal() as s:
            n = s.get(Note, self.selected_note_id)
            if not n or n.user_id != self.user_id:
                return await interaction.response.send_message("Not found.", ephemeral=True)
            s.delete(n)
            s.commit()

        self.selected_note_id = None
        self.delete_confirm = False
        self.delete_button.label = "Delete"
        await self.refresh_options(interaction)
        await interaction.followup.send("🗑️ Deleted.", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.refresh_options(interaction)
        await interaction.followup.send("🔄 Refreshed.", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

# ---------- Cog ----------

class NotesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="notes", description="Manage your notes via dropdown: add, view, edit, delete.")
    async def notes(self, inter: discord.Interaction):
        view = NotesView(self.bot, inter.user.id)

        # Build first view and store the message id so modals can update it later
        options, id_map = view._build_options()
        view.select = NotesSelect(view, options, id_map)
        view.select.disabled = (len(options) == 1 and options[0].value == "__none__")
        view.edit_button.disabled = True
        view.delete_button.disabled = True
        view.delete_confirm = False
        view.add_item(view.select)

        embed = discord.Embed(
            title="🗒️ Notes",
            description="Select a note to view, or use buttons to add/edit/delete.\nUse `/notes` anytime.",
            color=discord.Color.blurple(),
        )
        await inter.response.send_message(embed=embed, view=view, ephemeral=True)

        # Grab the message id (needed to edit the ephemeral message from modals)
        msg = await inter.original_response()
        view.message_id = msg.id

async def setup(bot: commands.Bot):
    await bot.add_cog(NotesCog(bot))