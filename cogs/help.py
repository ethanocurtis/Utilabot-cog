"""
Central /help command.

Discovers every loaded cog's slash commands (including subcommands on
app_commands.Group) at call time, so it never goes stale as cogs are added,
removed, or edited. Commands are organized by cog ("category") with a short
per-category blurb, and browsable either via a dropdown or a direct
`/help category:` argument.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Friendly display metadata for each cog. Keyed by the cog's qualified name
# (its class name, since none of these cogs override it). A cog that isn't
# listed here still shows up -- it just falls back to its raw class name and
# docstring (or a generic blurb) instead of a curated one.
# ---------------------------------------------------------------------------
CATEGORY_META: Dict[str, Dict[str, str]] = {
    "EconomyCog":    {"emoji": "💰", "label": "Economy",        "blurb": "Balance, daily credits, and the item shop."},
    "BusinessCog":   {"emoji": "🏢", "label": "Business",       "blurb": "Buy, upgrade, and collect income from businesses."},
    "StockMarket":   {"emoji": "📈", "label": "Stock Market",   "blurb": "Trade a fictional market and track your portfolio."},
    "GamesCog":      {"emoji": "🎮", "label": "Games",          "blurb": "Coinflip, blackjack, high-low, and trivia."},
    "Slots":         {"emoji": "🎰", "label": "Slots",          "blurb": "Slot machine with leaderboards and personal stats."},
    "Roulette":      {"emoji": "🎡", "label": "Roulette",       "blurb": "Place bets and spin the wheel."},
    "HorseRace":     {"emoji": "🐎", "label": "Horse Race",     "blurb": "Betting horse races with AI-filled lanes."},
    "TextAdventure": {"emoji": "🗺️", "label": "Text Adventure", "blurb": "Play choice-driven text adventures."},
    "Weather":       {"emoji": "🌦️", "label": "Weather",        "blurb": "Current conditions, DM subscriptions, and severe alerts."},
    "Polls":         {"emoji": "🗳️", "label": "Polls",          "blurb": "Create quick 2-option polls."},
    "PollsCog":      {"emoji": "🗳️", "label": "Polls",          "blurb": "Create quick 2-option polls."},
    "Reminders":     {"emoji": "⏰", "label": "Reminders",      "blurb": "Set one-shot or recurring reminders."},
    "NotesCog":      {"emoji": "📝", "label": "Notes",          "blurb": "Personal notes: add, view, edit, delete."},
    "Pins":          {"emoji": "📌", "label": "Pins",           "blurb": "Save and organize message links per channel."},
    "StickyCog":     {"emoji": "📍", "label": "Sticky Notes",   "blurb": "Auto-reposting sticky messages in a channel."},
    "KuttCog":       {"emoji": "🔗", "label": "Links",          "blurb": "Shorten URLs with your Kutt instance."},
    "AudioSlash":    {"emoji": "🎵", "label": "Audio",          "blurb": "Play music from YouTube in a voice channel."},
    "Tickets":       {"emoji": "🎫", "label": "Tickets",        "blurb": "Support ticket panel and per-ticket management."},
    "Moderation":    {"emoji": "🛡️", "label": "Moderation",     "blurb": "Purge messages and manage auto-delete rules."},
    "AutoRole":      {"emoji": "🚪", "label": "Auto Role",      "blurb": "Automatically assign roles to new members."},
    "AdminGates":    {"emoji": "🔐", "label": "Admin Gates",    "blurb": "Control who can run which commands, and where."},
    "MinecraftCog":  {"emoji": "⛏️", "label": "Minecraft",      "blurb": "RCON control panel for a Minecraft server."},
    "MCTodo":        {"emoji": "✅", "label": "Minecraft To-Do", "blurb": "Shared to-do list for a Minecraft server."},
    "ServerAlerts":  {"emoji": "🚨", "label": "Server Alerts",  "blurb": "Report and route Minecraft server issues."},
    "PterodactylCog": {"emoji": "🦖", "label": "Pterodactyl",  "blurb": "Control panel + backup/crash monitoring for Pterodactyl servers."},
}

DEFAULT_EMOJI = "📦"


def _meta_for(cog_name: str) -> Dict[str, str]:
    return CATEGORY_META.get(cog_name, {"emoji": DEFAULT_EMOJI, "label": cog_name, "blurb": ""})


def _flatten(cmd) -> List[Tuple[str, str]]:
    """Recursively flatten a Command/Group into [(qualified_name, description), ...]."""
    if isinstance(cmd, app_commands.Group):
        out: List[Tuple[str, str]] = []
        for sub in cmd.commands:
            out.extend(_flatten(sub))
        return out
    return [(cmd.qualified_name, cmd.description or "No description.")]


def _cog_commands(cog: commands.Cog) -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = []
    for top in cog.get_app_commands():
        entries.extend(_flatten(top))
    entries.sort(key=lambda pair: pair[0].lower())
    return entries


def _collect_categories(bot: commands.Bot) -> List[Tuple[str, commands.Cog, List[Tuple[str, str]]]]:
    """Return [(cog_qualified_name, cog, [(command, description), ...]), ...] for every
    cog that actually exposes at least one slash command, sorted for display."""
    rows = []
    for name, cog in bot.cogs.items():
        if name == "HelpCog":
            continue  # don't list the help command as its own category
        cmds = _cog_commands(cog)
        if cmds:
            rows.append((name, cog, cmds))
    rows.sort(key=lambda r: _meta_for(r[0])["label"].lower())
    return rows


class CategorySelect(discord.ui.Select):
    def __init__(self, view: "HelpView"):
        self._help_view = view
        options = []
        for name, _cog, cmds in view.categories:
            meta = _meta_for(name)
            options.append(
                discord.SelectOption(
                    label=meta["label"],
                    value=name,
                    description=(meta["blurb"] or f"{len(cmds)} command(s)")[:100],
                    emoji=meta["emoji"],
                )
            )
        super().__init__(placeholder="Browse a category…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        embed = self._help_view.build_category_embed(chosen)
        await interaction.response.edit_message(embed=embed, view=self._help_view)


class BackButton(discord.ui.Button):
    def __init__(self, view: "HelpView"):
        super().__init__(label="Overview", style=discord.ButtonStyle.secondary, emoji="⬅️")
        self._help_view = view

    async def callback(self, interaction: discord.Interaction):
        embed = self._help_view.build_overview_embed()
        await interaction.response.edit_message(embed=embed, view=self._help_view)


class HelpView(discord.ui.View):
    def __init__(self, bot: commands.Bot, categories: List[Tuple[str, commands.Cog, List[Tuple[str, str]]]], author_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.categories = categories
        self.author_id = author_id
        self.add_item(CategorySelect(self))
        self.add_item(BackButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This help menu isn't yours — run `/help` to get your own.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    def build_overview_embed(self) -> discord.Embed:
        total = sum(len(cmds) for _, _, cmds in self.categories)
        embed = discord.Embed(
            title="📖 UtilaBot Help",
            description=(
                f"**{total}** commands across **{len(self.categories)}** categories.\n"
                "Pick a category from the dropdown below, or run `/help category:` "
                "to jump straight to one."
            ),
            color=discord.Color.blurple(),
        )
        for name, _cog, cmds in self.categories:
            meta = _meta_for(name)
            blurb = meta["blurb"] or f"{len(cmds)} command(s)."
            embed.add_field(
                name=f"{meta['emoji']} {meta['label']} ({len(cmds)})",
                value=blurb,
                inline=False,
            )
        embed.set_footer(text="Tip: some commands may be restricted by server admins.")
        return embed

    def build_category_embed(self, cog_name: str) -> discord.Embed:
        match = next((row for row in self.categories if row[0] == cog_name), None)
        if match is None:
            return self.build_overview_embed()
        name, _cog, cmds = match
        meta = _meta_for(name)
        embed = discord.Embed(
            title=f"{meta['emoji']} {meta['label']}",
            description=meta["blurb"] or None,
            color=discord.Color.blurple(),
        )
        for qualified_name, description in cmds:
            embed.add_field(name=f"/{qualified_name}", value=description, inline=False)
        embed.set_footer(text=f"{len(cmds)} command(s) • Use the dropdown to browse other categories.")
        return embed


class HelpCog(commands.Cog):
    """Central help command: organizes every command by cog, with descriptions."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _category_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        categories = _collect_categories(self.bot)
        current_l = (current or "").lower()
        choices = []
        for name, _cog, cmds in categories:
            meta = _meta_for(name)
            if current_l in meta["label"].lower():
                choices.append(app_commands.Choice(name=f"{meta['label']} ({len(cmds)})", value=name))
        return choices[:25]

    @app_commands.command(name="help", description="Show all commands, organized by category.")
    @app_commands.describe(category="Jump straight to a specific category.")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def help_command(self, interaction: discord.Interaction, category: Optional[str] = None):
        categories = _collect_categories(self.bot)
        view = HelpView(self.bot, categories, interaction.user.id)

        if category and any(name == category for name, _cog, _cmds in categories):
            embed = view.build_category_embed(category)
        else:
            embed = view.build_overview_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
