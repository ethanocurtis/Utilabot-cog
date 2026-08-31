"""Builds assets/cobbleverse_pokedex.json from a local checkout of the
cazuike/cobbleverse-wiki repo (https://github.com/cazuike/cobbleverse-wiki).

That wiki is generated from the actual Cobbleverse modpack files (spawn
JSONs, showdown stats, quest datapacks), so it's the closest thing to an
authoritative source for "what's in this pack and how do I get it."

Usage:
    git clone --depth 1 https://github.com/cazuike/cobbleverse-wiki /tmp/cobbleverse-wiki
    python scripts/build_cobbleverse_pokedex.py /tmp/cobbleverse-wiki

Re-run this whenever the wiki repo gets new commits to refresh
assets/cobbleverse_pokedex.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

STAT_NAME_MAP = {
    "HP": "hp",
    "Attack": "atk",
    "Defense": "def",
    "Sp. Atk": "spa",
    "Sp. Def": "spd",
    "Speed": "spe",
}

ADMONITION_RE = re.compile(r'^!!! (\w+) "(.*)"\s*$')
DEXNUM_RE = re.compile(r'<p class="dex-num">No\. (\d+)</p>')
TYPE_RE = re.compile(r'type-badge type-[\w-]+">([^<]+)</span>')
METRICS_RE = re.compile(
    r"<strong>Height:</strong>\s*([\d.]+)\s*m.*?<strong>Weight:</strong>\s*([\d.]+)\s*kg"
)
STARTER_TITLE_RE = re.compile(r"Starter\s*[—-]\s*(.+?)\s*category")
STARTER_LEVEL_RE = re.compile(r"Starts at \*\*level (\d+)\*\*")
WILDSPAWN_LEVEL_RE = re.compile(r"\*\*Level ([\d\-]+)\*\*")
WILDSPAWN_BUCKET_RE = re.compile(r"bucket `([\w-]+)`")
WILDSPAWN_TIME_RE = re.compile(r"\*(daylight only|night only|[\w /]+ only)\*")
WILDSPAWN_BIOMES_RE = re.compile(r"\*Biomes:\*\s*(.+?)\s*$", re.MULTILINE)
STAT_ROW_RE = re.compile(
    r"\|\s*\*\*(HP|Attack|Defense|Sp\. Atk|Sp\. Def|Speed)\*\*\s*\|\s*(\d+)\s*\|"
)
STAT_TOTAL_RE = re.compile(r"\|\s*\*\*Total\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|")
ABILITY_RE = re.compile(r"^-\s*\*\*(.+?)\*\*(\s*\*\(hidden ability\)\*)?\s*$", re.MULTILINE)
EVO_CHAIN_RE = re.compile(
    r'<div class="evo-chain" markdown>\s*(.*?)\s*</div>', re.DOTALL
)
MOVE_ROW_RE = re.compile(
    r'<span class="move-level">([^<]+)</span><div class="move-grid">(.*?)</div></div>'
)
MOVE_CHIP_RE = re.compile(
    r'<span class="move-chip[^"]*"[^>]*>(?:<span class="move-cat[^"]*">[^<]*</span>)?([^<]+)</span>'
)
LEVELUP_TAB_RE = re.compile(
    r'===\s*"Level-up"\s*\n\n(.*?)(?=\n===\s*"|\Z)', re.DOTALL
)


def parse_admonitions(section_text: str) -> List[Dict[str, Any]]:
    """Splits a '## How to obtain' section into its !!! admonition blocks,
    each with its (dedented) body text."""
    lines = section_text.split("\n")
    blocks: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        m = ADMONITION_RE.match(lines[i])
        if not m:
            i += 1
            continue
        kind, title = m.group(1), m.group(2)
        i += 1
        body_lines = []
        while i < len(lines):
            line = lines[i]
            if line.strip() == "":
                # Blank line: keep going only if the *next* non-blank line
                # is still indented (part of this block).
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j < len(lines) and (lines[j].startswith("    ") or lines[j].startswith("\t")):
                    body_lines.append("")
                    i += 1
                    continue
                break
            if line.startswith("    "):
                body_lines.append(line[4:])
                i += 1
                continue
            break
        blocks.append({"kind": kind, "title": title, "body": "\n".join(body_lines)})
    return blocks


def parse_obtain_section(text: str) -> Dict[str, Any]:
    blocks = parse_admonitions(text)
    starter = None
    wild_spawns: List[Dict[str, Any]] = []
    raid_den_boss = False
    special_obtain: List[Dict[str, Any]] = []
    other_notes: List[str] = []

    for b in blocks:
        kind, title, body = b["kind"], b["title"], b["body"]
        if kind == "tip" and title.lower().startswith("starter"):
            cat_m = STARTER_TITLE_RE.search(title)
            lvl_m = STARTER_LEVEL_RE.search(body)
            starter = {
                "category": cat_m.group(1) if cat_m else title,
                "level": int(lvl_m.group(1)) if lvl_m else None,
            }
        elif kind == "info" and title.lower().startswith("wild spawn"):
            if "placeholder for a quest or event trigger" in body:
                # Weight-0 entries mark a quest/event trigger, not a real
                # wild spawn -- surfaced instead via special_obtain/legendary_quest.
                continue
            location = title.split("·", 1)[1].strip() if "·" in title else title
            lvl_m = WILDSPAWN_LEVEL_RE.search(body)
            bucket_m = WILDSPAWN_BUCKET_RE.search(body)
            time_m = WILDSPAWN_TIME_RE.search(body)
            biomes_m = WILDSPAWN_BIOMES_RE.search(body)
            biomes_preview = biomes_m.group(1).strip() if biomes_m else None
            if biomes_preview:
                biomes_preview = re.sub(r",\s*\*and \d+ more\*\s*$", "", biomes_preview)
            wild_spawns.append(
                {
                    "location": location,
                    "level_range": lvl_m.group(1) if lvl_m else None,
                    "bucket": bucket_m.group(1) if bucket_m else None,
                    "time": time_m.group(1) if time_m else None,
                    "biomes_preview": biomes_preview,
                }
            )
        elif kind == "abstract" and "raid den boss" in title.lower():
            raid_den_boss = True
        elif kind == "example" and title.lower().startswith("special obtain method"):
            region = title.split("·", 1)[1].strip() if "·" in title else None
            first_line = next((l for l in body.split("\n") if l.strip()), "").strip()
            prereq_m = re.search(r"\*\*Prerequisite:\*\*\s*(.+)", body)
            special_obtain.append(
                {
                    "region": region,
                    "text": first_line,
                    "prerequisite": prereq_m.group(1).strip() if prereq_m else None,
                }
            )
        else:
            first_line = next((l for l in body.split("\n") if l.strip()), "").strip()
            other_notes.append(f"{title}: {first_line}" if first_line else title)

    # Dedupe wild spawn entries by location, keep at most 6 for brevity.
    seen = set()
    deduped = []
    for ws in wild_spawns:
        key = ws["location"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ws)
    deduped = deduped[:6]

    return {
        "starter": starter,
        "wild_spawns": deduped,
        "raid_den_boss": raid_den_boss,
        "special_obtain": special_obtain,
        "other_notes": other_notes,
    }


def parse_base_stats(text: str) -> Optional[Dict[str, int]]:
    stats = {}
    for m in STAT_ROW_RE.finditer(text):
        stats[STAT_NAME_MAP[m.group(1)]] = int(m.group(2))
    total_m = STAT_TOTAL_RE.search(text)
    if total_m:
        stats["total"] = int(total_m.group(1))
    return stats or None


def parse_abilities(text: str) -> List[Dict[str, Any]]:
    out = []
    for m in ABILITY_RE.finditer(text):
        out.append({"name": m.group(1).strip(), "hidden": bool(m.group(2))})
    return out


def parse_evolution(text: str) -> Optional[str]:
    m = EVO_CHAIN_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip() or None


def parse_level_up_moves(moves_section: str) -> List[Dict[str, Any]]:
    tab_m = LEVELUP_TAB_RE.search(moves_section)
    if not tab_m:
        return []
    out = []
    for row_m in MOVE_ROW_RE.finditer(tab_m.group(1)):
        level, grid = row_m.group(1), row_m.group(2)
        moves = [mv.strip() for mv in MOVE_CHIP_RE.findall(grid)]
        for mv in moves:
            out.append({"level": level, "move": mv})
    return out


def split_sections(body: str) -> Dict[str, str]:
    """Splits the file (after the header div) into '## Section' -> text."""
    parts = re.split(r"^## (.+)$", body, flags=re.MULTILINE)
    # parts[0] is preamble before first ##; then alternating heading/text
    sections = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        text = parts[i + 1] if i + 1 < len(parts) else ""
        sections[heading] = text
    return sections, parts[0]


def parse_pokemon_file(path: Path) -> Optional[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    name_m = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
    dex_m = DEXNUM_RE.search(text)
    if not name_m or not dex_m:
        return None
    name = name_m.group(1).strip()
    dex = int(dex_m.group(1))
    types = TYPE_RE.findall(text)
    metrics_m = METRICS_RE.search(text)
    height_m = float(metrics_m.group(1)) if metrics_m else None
    weight_kg = float(metrics_m.group(2)) if metrics_m else None

    flavor_m = re.search(r"^>\s*(.+)$", text, re.MULTILINE)
    flavor = flavor_m.group(1).strip() if flavor_m else None

    sections, _preamble = split_sections(text)

    obtain = parse_obtain_section(sections.get("How to obtain", ""))
    base_stats = parse_base_stats(sections.get("Base stats", ""))
    abilities = parse_abilities(sections.get("Abilities", ""))
    evolution = parse_evolution(sections.get("Evolution", ""))
    moves_level_up = parse_level_up_moves(sections.get("Moves", ""))

    return {
        "dex": dex,
        "name": name,
        "slug": path.stem,
        "types": types,
        "height_m": height_m,
        "weight_kg": weight_kg,
        "flavor": flavor,
        "sprite": f"{dex:03d}.png",
        **obtain,
        "base_stats": base_stats,
        "abilities": abilities,
        "evolution": evolution,
        "moves_level_up": moves_level_up,
    }


LEGEND_REGION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
LEGEND_ROW_RE = re.compile(
    r"^\|\s*<img[^>]*>\s*\|\s*\[\*\*(?P<name>[^\]*]+)\*\*\]\(\.\./pokemon/(?P<dex>\d{4})-[^)]+\.md\)"
    r"(?:\s*\*(?P<note>\([^)]*\))\*)?<br><small>No\.\s*\d+</small>\s*\|\s*(?P<types>.+?)\s*\|"
    r"\s*(?P<item>.+?)\s*\|\s*(?P<radar>.+?)\s*\|\s*\*(?P<prereq>[^*]*)\*\s*\|\s*(?P<howto>.+?)\s*\|\s*$",
    re.MULTILINE,
)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
CODE_RE = re.compile(r"[``]([\w:]+)[``]")


def parse_legendaries_index(path: Path) -> Dict[int, Dict[str, Any]]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # Track which region heading precedes each row by walking headings + rows
    # in document order.
    events = []
    for m in LEGEND_REGION_RE.finditer(text):
        events.append((m.start(), "region", m.group(1).strip()))
    for m in LEGEND_ROW_RE.finditer(text):
        events.append((m.start(), "row", m))
    events.sort(key=lambda e: e[0])

    out: Dict[int, Dict[str, Any]] = {}
    current_region = None
    for _, kind, payload in events:
        if kind == "region":
            current_region = payload
            continue
        m = payload
        dex = int(m.group("dex"))
        item_bold = BOLD_RE.search(m.group("item"))
        item_code = CODE_RE.search(m.group("item"))
        radar_raw = m.group("radar").strip()
        radar_bold = BOLD_RE.search(radar_raw)
        radar_code = CODE_RE.search(radar_raw)
        entry = {
            "region": current_region,
            "note": m.group("note"),
            "gating_item": item_bold.group(1) if item_bold else None,
            "gating_item_id": item_code.group(1) if item_code else None,
            "radar": radar_bold.group(1) if radar_bold else (None if radar_raw == "—" else radar_raw),
            "radar_id": radar_code.group(1) if radar_code else None,
            "prerequisite": m.group("prereq").strip() or None,
            "how_to_get": m.group("howto").strip(),
        }
        # A species can have multiple quest rows (e.g. Mewtwo's two forms);
        # keep the first, but note if there's more than one.
        if dex not in out:
            out[dex] = entry
        else:
            out[dex].setdefault("_also", []).append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wiki_dir", type=Path, help="Path to a cazuike/cobbleverse-wiki checkout")
    ap.add_argument(
        "-o",
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "cobbleverse_pokedex.json",
    )
    args = ap.parse_args()

    pokemon_dir = args.wiki_dir / "docs" / "pokemon"
    if not pokemon_dir.is_dir():
        print(f"error: {pokemon_dir} not found (wrong wiki checkout path?)", file=sys.stderr)
        return 1

    legendaries = parse_legendaries_index(args.wiki_dir / "docs" / "legendaries" / "index.md")

    entries = []
    failures = []
    for md_path in sorted(pokemon_dir.glob("*.md")):
        try:
            entry = parse_pokemon_file(md_path)
        except Exception as e:  # noqa: BLE001
            failures.append((md_path.name, str(e)))
            continue
        if entry is None:
            failures.append((md_path.name, "missing name/dex"))
            continue
        legend = legendaries.get(entry["dex"])
        if legend:
            entry["legendary_quest"] = legend
        entries.append(entry)

    entries.sort(key=lambda e: e["dex"])

    print(f"Parsed {len(entries)} / {len(list(pokemon_dir.glob('*.md')))} pages", file=sys.stderr)
    if failures:
        print(f"{len(failures)} failures:", file=sys.stderr)
        for name, err in failures[:20]:
            print(f"  {name}: {err}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "https://github.com/cazuike/cobbleverse-wiki",
                "generated_from_commit": None,
                "count": len(entries),
                "pokemon": entries,
            },
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
