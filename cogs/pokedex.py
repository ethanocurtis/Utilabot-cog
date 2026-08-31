"""
Cobbleverse Pokédex lookup.

Looks up species info (types, stats, abilities, evolution) plus, more
importantly, *how to actually get that Pokémon in the Cobbleverse modpack*
(starter pick, wild spawn biomes/levels/rarity, raid den boss, or the
legendary/mythical quest chain -- gating item, radar, prerequisite).

Data comes from a one-time build (scripts/build_cobbleverse_pokedex.py)
against the community-maintained cazuike/cobbleverse-wiki repo, which is
itself generated from the modpack's own config/spawn/datapack files. Re-run
that script and commit the refreshed assets/cobbleverse_pokedex.json when
the wiki (or the pack) gets updated -- this cog does no network calls at
lookup time.

The dataset lives under assets/, not data/, on purpose: docker-compose
bind-mounts a host directory over /app/data for runtime state (bot.db,
minecraft config, etc.), which would shadow anything shipped there in
the image. assets/ isn't mounted, so it survives.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# Resolved relative to this file (not the process CWD) so it's found
# regardless of where the bot is launched from.
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "cobbleverse_pokedex.json")
SPRITE_BASE_URL = "https://raw.githubusercontent.com/cazuike/cobbleverse-wiki/main/docs/assets/pokemon/"
WIKI_SOURCE_URL = "https://github.com/cazuike/cobbleverse-wiki"

STAT_LABELS = [
    ("hp", "HP"),
    ("atk", "Atk"),
    ("def", "Def"),
    ("spa", "SpA"),
    ("spd", "SpD"),
    ("spe", "Spe"),
]

# Standard (Gen 6+) type effectiveness chart: attacker -> {defender: multiplier}.
# Omitted pairs default to 1x. This is generic Pokémon ruleset, not Cobbleverse-
# specific, so it's hardcoded rather than sourced from the wiki (which doesn't
# document it -- see the "How the wiki covers this" note in the cog docstring).
ALL_TYPES = [
    "Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting", "Poison",
    "Ground", "Flying", "Psychic", "Bug", "Rock", "Ghost", "Dragon", "Dark",
    "Steel", "Fairy",
]
TYPE_CHART: Dict[str, Dict[str, float]] = {
    "Normal": {"Rock": 0.5, "Ghost": 0, "Steel": 0.5},
    "Fire": {"Fire": 0.5, "Water": 0.5, "Grass": 2, "Ice": 2, "Bug": 2, "Rock": 0.5, "Dragon": 0.5, "Steel": 2},
    "Water": {"Fire": 2, "Water": 0.5, "Grass": 0.5, "Ground": 2, "Rock": 2, "Dragon": 0.5},
    "Electric": {"Water": 2, "Electric": 0.5, "Grass": 0.5, "Ground": 0, "Flying": 2, "Dragon": 0.5},
    "Grass": {"Fire": 0.5, "Water": 2, "Grass": 0.5, "Poison": 0.5, "Ground": 2, "Flying": 0.5, "Bug": 0.5, "Rock": 2, "Dragon": 0.5, "Steel": 0.5},
    "Ice": {"Fire": 0.5, "Water": 0.5, "Grass": 2, "Ice": 0.5, "Ground": 2, "Flying": 2, "Dragon": 2, "Steel": 0.5},
    "Fighting": {"Normal": 2, "Ice": 2, "Poison": 0.5, "Flying": 0.5, "Psychic": 0.5, "Bug": 0.5, "Rock": 2, "Ghost": 0, "Dark": 2, "Steel": 2, "Fairy": 0.5},
    "Poison": {"Grass": 2, "Poison": 0.5, "Ground": 0.5, "Rock": 0.5, "Ghost": 0.5, "Steel": 0, "Fairy": 2},
    "Ground": {"Fire": 2, "Electric": 2, "Grass": 0.5, "Poison": 2, "Flying": 0, "Bug": 0.5, "Rock": 2, "Steel": 2},
    "Flying": {"Electric": 0.5, "Grass": 2, "Fighting": 2, "Bug": 2, "Rock": 0.5, "Steel": 0.5},
    "Psychic": {"Fighting": 2, "Poison": 2, "Psychic": 0.5, "Dark": 0, "Steel": 0.5},
    "Bug": {"Fire": 0.5, "Grass": 2, "Fighting": 0.5, "Poison": 0.5, "Flying": 0.5, "Psychic": 2, "Ghost": 0.5, "Dark": 2, "Steel": 0.5, "Fairy": 0.5},
    "Rock": {"Fire": 2, "Ice": 2, "Fighting": 0.5, "Ground": 0.5, "Flying": 2, "Bug": 2, "Steel": 0.5},
    "Ghost": {"Normal": 0, "Psychic": 2, "Ghost": 2, "Dark": 0.5},
    "Dragon": {"Dragon": 2, "Steel": 0.5, "Fairy": 0},
    "Dark": {"Fighting": 0.5, "Psychic": 2, "Ghost": 2, "Dark": 0.5, "Fairy": 0.5},
    "Steel": {"Fire": 0.5, "Water": 0.5, "Electric": 0.5, "Ice": 2, "Rock": 2, "Steel": 0.5, "Fairy": 2},
    "Fairy": {"Fire": 0.5, "Fighting": 2, "Poison": 0.5, "Dragon": 2, "Dark": 2, "Steel": 0.5},
}


def _type_matchups(defender_types: List[str]) -> Dict[str, float]:
    """Multiplier each attacking type deals against this type combo."""
    out = {}
    for atk in ALL_TYPES:
        mult = 1.0
        for d in defender_types:
            mult *= TYPE_CHART.get(atk, {}).get(d, 1.0)
        out[atk] = mult
    return out


def _format_matchups(mults: Dict[str, float]) -> str:
    tiers = {4.0: [], 2.0: [], 0.5: [], 0.25: [], 0.0: []}
    for atk, mult in mults.items():
        if mult in tiers:
            tiers[mult].append(atk)
    lines = []
    if tiers[4.0]:
        lines.append(f"💥 **Weak x4:** {', '.join(tiers[4.0])}")
    if tiers[2.0]:
        lines.append(f"⚠️ **Weak x2:** {', '.join(tiers[2.0])}")
    if tiers[0.5]:
        lines.append(f"🛡️ **Resists x0.5:** {', '.join(tiers[0.5])}")
    if tiers[0.25]:
        lines.append(f"🛡️ **Resists x0.25:** {', '.join(tiers[0.25])}")
    if tiers[0.0]:
        lines.append(f"🚫 **Immune:** {', '.join(tiers[0.0])}")
    return "\n".join(lines) if lines else "Neutral to all types."

TYPE_COLORS = {
    "Normal": 0xA8A878, "Fire": 0xF08030, "Water": 0x6890F0, "Electric": 0xF8D030,
    "Grass": 0x78C850, "Ice": 0x98D8D8, "Fighting": 0xC03028, "Poison": 0xA040A0,
    "Ground": 0xE0C068, "Flying": 0xA890F0, "Psychic": 0xF85888, "Bug": 0xA8B820,
    "Rock": 0xB8A038, "Ghost": 0x705898, "Dragon": 0x7038F8, "Dark": 0x705848,
    "Steel": 0xB8B8D0, "Fairy": 0xEE99AC,
}
DEFAULT_COLOR = 0x3B4CCA


def _stat_bar(value: int, width: int = 9, scale: int = 200) -> str:
    """Tiny block-character bar, scaled against a 200 reference point
    (comfortably above most base stats, without every bar maxing out)."""
    filled = max(0, min(width, round((value or 0) / scale * width)))
    return "█" * filled + "░" * (width - filled)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


class Pokedex(commands.Cog):
    """Cobbleverse Pokédex: species info + how to obtain, in-pack."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.by_dex: Dict[int, Dict[str, Any]] = {}
        self.by_name: Dict[str, Dict[str, Any]] = {}
        self.names: List[str] = []
        self.source_commit: Optional[str] = None
        self._load_data()

    def _load_data(self) -> None:
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            log.warning(
                "Pokedex data file not found at %s -- /pokedex will report no data. "
                "Run scripts/build_cobbleverse_pokedex.py to generate it.",
                DATA_PATH,
            )
            return
        except Exception:
            log.exception("Failed to load Pokedex data from %s", DATA_PATH)
            return

        entries = payload.get("pokemon", [])
        self.source_commit = payload.get("generated_from_commit")
        for entry in entries:
            self.by_dex[entry["dex"]] = entry
            self.by_name[entry["name"].lower()] = entry
        self.names = sorted((e["name"] for e in entries), key=str.lower)
        log.info("Loaded %d Cobbleverse Pokédex entries", len(entries))

    # ---------- lookup helpers ----------

    def _resolve(self, query: str) -> Optional[Dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return None
        if query.isdigit():
            return self.by_dex.get(int(query))
        # Also accept "#025" / "No. 025" style input.
        stripped = query.lstrip("#").strip()
        if stripped.lower().startswith("no."):
            stripped = stripped[3:].strip()
        if stripped.isdigit():
            return self.by_dex.get(int(stripped))
        exact = self.by_name.get(query.lower())
        if exact:
            return exact
        return None

    def _suggest(self, query: str, limit: int = 5) -> List[str]:
        return difflib.get_close_matches(query, self.names, n=limit, cutoff=0.5)

    async def _pokemon_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        current_l = (current or "").lower().strip()
        if not current_l:
            pool = self.names[:25]
        else:
            starts = [n for n in self.names if n.lower().startswith(current_l)]
            contains = [n for n in self.names if current_l in n.lower() and n not in starts]
            pool = (starts + contains)[:25]
        out = []
        for n in pool:
            entry = self.by_name[n.lower()]
            out.append(app_commands.Choice(name=f"#{entry['dex']:03d} {n}", value=n))
        return out

    # ---------- embed building ----------

    def _build_embed(self, entry: Dict[str, Any]) -> discord.Embed:
        dex = entry["dex"]
        name = entry["name"]
        types = entry.get("types") or []
        color = TYPE_COLORS.get(types[0], DEFAULT_COLOR) if types else DEFAULT_COLOR

        title = f"#{dex:03d} {name}"
        description = entry.get("flavor") or (" / ".join(types) if types else None)

        embed = discord.Embed(title=title, description=description, color=color)
        embed.set_thumbnail(url=SPRITE_BASE_URL + entry["sprite"])

        # Row 1: three short facts side by side.
        embed.add_field(name="Type", value=" / ".join(types) or "Unknown", inline=True)
        h = entry.get("height_m")
        w = entry.get("weight_kg")
        embed.add_field(
            name="Height / Weight",
            value=(f"{h} m · {w} kg" if h is not None and w is not None else "Unknown"),
            inline=True,
        )
        abilities = entry.get("abilities") or []
        if abilities:
            ability_lines = [
                f"{a['name']} *(hidden)*" if a.get("hidden") else a["name"] for a in abilities
            ]
            embed.add_field(name="Abilities", value="\n".join(ability_lines), inline=True)

        # Row 2: base stats get the full width so the bars have room.
        stats = entry.get("base_stats")
        if stats:
            lines = [
                f"{label:<4}{stats.get(key, 0):>4} {_stat_bar(stats.get(key, 0))}"
                for key, label in STAT_LABELS
            ]
            lines.append(f"{'Tot':<4}{stats.get('total', 0):>4}")
            embed.add_field(name="Base Stats", value="```\n" + "\n".join(lines) + "\n```", inline=False)

        # Row 3: type matchups, full width.
        if types:
            matchup_text = _format_matchups(_type_matchups(types))
            embed.add_field(name="Type Matchups", value=_truncate(matchup_text, 1024), inline=False)

        # Row 4: evolution, short — inline so it doesn't force an almost-empty row.
        evolution = entry.get("evolution")
        if evolution:
            embed.add_field(name="Evolution", value=_truncate(evolution, 1024), inline=True)

        embed.add_field(name="How to obtain in Cobbleverse", value=self._obtain_text(entry), inline=False)

        footer = f"Cobbleverse Pokédex · #{dex}/1025 · data from cazuike/cobbleverse-wiki"
        embed.set_footer(text=footer)
        return embed

    def _obtain_text(self, entry: Dict[str, Any]) -> str:
        lines: List[str] = []

        starter = entry.get("starter")
        if starter:
            lvl = f" (Lv. {starter['level']})" if starter.get("level") else ""
            lines.append(f"🟢 **Starter** — pick from the **{starter['category']}** category{lvl}.")

        legend = entry.get("legendary_quest")
        if legend:
            note = f" {legend['note']}" if legend.get("note") else ""
            item = legend.get("gating_item") or "an item"
            item_id = f" (`{legend['gating_item_id']}`)" if legend.get("gating_item_id") else ""
            lines.append(f"🏆 **Legendary/quest Pokémon**{note} — {legend.get('region') or 'unknown region'}.")
            need = f"Needs **{item}**{item_id}"
            if legend.get("radar"):
                need += f", tracked with the **{legend['radar']}**"
            lines.append(need + ".")
            if legend.get("prerequisite"):
                lines.append(f"Prerequisite: *{legend['prerequisite']}*.")

        for special in (entry.get("special_obtain") or [])[:2]:
            if legend:
                break  # already covered by the legendary_quest summary above
            region = f" ({special['region']})" if special.get("region") else ""
            lines.append(f"✨ **Special obtain**{region}: {special['text']}")
            if special.get("prerequisite"):
                lines.append(f"Prerequisite: {special['prerequisite']}")

        if entry.get("raid_den_boss"):
            lines.append("⚔️ Also appears as a **Raid Den** boss.")

        wild_spawns = entry.get("wild_spawns") or []
        for ws in wild_spawns[:3]:
            bits = [ws["location"]]
            meta = []
            if ws.get("level_range"):
                meta.append(f"Lv {ws['level_range']}")
            if ws.get("bucket"):
                meta.append(ws["bucket"])
            if ws.get("time"):
                meta.append(ws["time"])
            header = f"🌿 **{bits[0]}** — {', '.join(meta)}" if meta else f"🌿 **{bits[0]}**"
            lines.append(header)
            if ws.get("biomes_preview"):
                lines.append(f"   Biomes: {_truncate(ws['biomes_preview'], 150)}")
        if len(wild_spawns) > 3:
            lines.append(f"…and {len(wild_spawns) - 3} more spawn location(s).")

        # Skip notes that just restate the Evolution field or a radar already
        # named in the legendary_quest summary above -- keep this section
        # focused on obtain methods, not a catch-all for every admonition.
        skip_prefixes = ("Evolution:", "Tracker")
        shown_notes = 0
        for note in entry.get("other_notes") or []:
            if note.startswith(skip_prefixes):
                continue
            lines.append(f"ℹ️ {note}")
            shown_notes += 1
            if shown_notes >= 2:
                break

        if not lines:
            lines.append(
                "No confirmed obtain method in the wiki yet — likely evolution-only, "
                "trade-only, or not documented for this pack version."
            )

        return _truncate("\n".join(lines), 1024)

    # ---------- commands ----------

    @app_commands.command(name="pokedex", description="Look up a Pokémon's Cobbleverse info: stats, types, and how to get it.")
    @app_commands.describe(pokemon="Pokémon name or dex number")
    @app_commands.autocomplete(pokemon=_pokemon_autocomplete)
    async def pokedex(self, interaction: discord.Interaction, pokemon: str):
        if not self.by_dex:
            return await interaction.response.send_message(
                "⚠️ Pokédex data isn't loaded. Ask an admin to run `scripts/build_cobbleverse_pokedex.py`.",
                ephemeral=True,
            )

        entry = self._resolve(pokemon)
        if not entry:
            suggestions = self._suggest(pokemon)
            msg = f"Couldn't find **{pokemon}** in the Cobbleverse dex."
            if suggestions:
                msg += " Did you mean: " + ", ".join(f"**{s}**" for s in suggestions) + "?"
            return await interaction.response.send_message(msg, ephemeral=True)

        await interaction.response.send_message(embed=self._build_embed(entry))

    @app_commands.command(name="pokedex_random", description="Show a random Cobbleverse Pokémon.")
    async def pokedex_random(self, interaction: discord.Interaction):
        if not self.by_dex:
            return await interaction.response.send_message(
                "⚠️ Pokédex data isn't loaded. Ask an admin to run `scripts/build_cobbleverse_pokedex.py`.",
                ephemeral=True,
            )
        import random

        entry = random.choice(list(self.by_dex.values()))
        await interaction.response.send_message(embed=self._build_embed(entry))


async def setup(bot: commands.Bot):
    await bot.add_cog(Pokedex(bot))
