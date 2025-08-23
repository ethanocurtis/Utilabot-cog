# cogs/text_adventure.py
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import aiosqlite

# =============================================================================
# Economy/Shop Adapter (USES YOUR HELPERS)
# =============================================================================
from utils.economy_helpers import (
    with_session,
    ensure_user,   # re-exported from utils.common by your helper module
    get_balance,
    can_afford,
    charge,
    payout,
)

try:
    # Optional: real shop model (adjust name if different)
    from utils.db import ShopItem  # type: ignore
except Exception:
    ShopItem = None  # fallback; shop lookups will return None


class EconomyAdapter:
    """
    Adapter backed by your SQLAlchemy DB + helpers.
    Expects: bot.SessionLocal to be a SQLAlchemy session factory.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.SessionLocal = getattr(bot, "SessionLocal", None)
        if self.SessionLocal is None:
            raise RuntimeError("TextAdventure: bot.SessionLocal is required for EconomyAdapter.")

    async def get_balance(self, guild_id: int, user_id: int) -> int:
        def _fn():
            with with_session(self.SessionLocal) as s:
                ensure_user(s, user_id)
                return get_balance(s, user_id)
        return await asyncio.to_thread(_fn)

    async def can_afford(self, guild_id: int, user_id: int, amount: int) -> bool:
        def _fn():
            with with_session(self.SessionLocal) as s:
                ensure_user(s, user_id)
                return can_afford(s, user_id, amount)
        return await asyncio.to_thread(_fn)

    async def debit(self, guild_id: int, user_id: int, amount: int) -> None:
        """Charge user (raises on insufficient funds)."""
        def _fn():
            with with_session(self.SessionLocal) as s:
                ensure_user(s, user_id)
                charge(s, user_id, amount)
        await asyncio.to_thread(_fn)

    async def credit(self, guild_id: int, user_id: int, amount: int) -> None:
        """Credit user (no checks)."""
        def _fn():
            with with_session(self.SessionLocal) as s:
                ensure_user(s, user_id)
                payout(s, user_id, amount)
        await asyncio.to_thread(_fn)

    async def shop_get_item(self, guild_id: int, item_name: str) -> Optional[Dict[str, Any]]:
        """
        Looks up an item by name in your ShopItem table (if available).
        Return {'name': <str>, 'price': <int>} or None.

        - Matches case-insensitively when possible.
        - If ShopItem has a guild_id column, scope by guild.
        - Detects price column among common names.
        """
        if ShopItem is None:
            return None

        def _detect_price_field(model) -> Optional[str]:
            for f in ("price", "cost", "amount", "value"):
                if hasattr(model, f):
                    return f
            return None

        def _fn():
            with with_session(self.SessionLocal) as s:
                q = s.query(ShopItem)
                if hasattr(ShopItem, "guild_id"):
                    q = q.filter(getattr(ShopItem, "guild_id") == guild_id)
                name_col = getattr(ShopItem, "name", None)
                if name_col is not None:
                    try:
                        q = q.filter(name_col.ilike(item_name))
                    except Exception:
                        q = q.filter(name_col == item_name)
                item = q.first()
                if not item:
                    return None
                price_field = _detect_price_field(item)
                if not price_field:
                    return None
                return {
                    "name": getattr(item, "name", item_name),
                    "price": int(getattr(item, price_field)),
                }

        return await asyncio.to_thread(_fn)


# =============================================================================
# Adventure Data Structures (content)
# =============================================================================

@dataclass
class Choice:
    label: str
    next: str
    requires: Optional[str] = None        # item required to click this choice
    reward_item: Optional[str] = None     # item to grant on choosing this
    reward_money: Optional[int] = None    # currency to grant on choosing this
    flag_set: Optional[str] = None        # set a story flag when chosen


@dataclass
class Node:
    id: str
    text: str
    requires: Optional[str] = None        # item required to ENTER this node
    choices: List[Choice] = None
    end: bool = False                     # whether this is an ending node


@dataclass
class Adventure:
    name: str
    nodes: Dict[str, Node]
    start: str

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Adventure":
        nodes: Dict[str, Node] = {}
        for key, nd in d["nodes"].items():
            choices = []
            for ch in nd.get("choices", []):
                choices.append(Choice(
                    label=ch["label"],
                    next=ch["next"],
                    requires=ch.get("requires"),
                    reward_item=ch.get("reward_item"),
                    reward_money=ch.get("reward_money"),
                    flag_set=ch.get("flag_set"),
                ))
            nodes[key] = Node(
                id=key,
                text=nd["text"],
                requires=nd.get("requires"),
                choices=choices,
                end=nd.get("end", False),
            )
        return Adventure(name=d["name"], nodes=nodes, start=d.get("start", "start"))


# =============================================================================
# UI Views
# =============================================================================

class ChoiceButton(discord.ui.Button):
    def __init__(
        self,
        label: str,
        style: discord.ButtonStyle,
        choice: Choice,
        disabled: bool,
        cog: "TextAdventure",
        adventure: Adventure,
        guild_id: int,
        user_id: int,
        missing_item: Optional[str],
    ):
        super().__init__(label=label, style=style, disabled=disabled)
        self.choice = choice
        self.cog = cog
        self.adventure = adventure
        self.guild_id = guild_id
        self.user_id = user_id
        self.missing_item = missing_item

    async def callback(self, inter: discord.Interaction):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("This is not your adventure run.", ephemeral=True)

        # If button disabled due to requirement missing, offer purchase UI
        if self.missing_item:
            return await self.cog.offer_purchase(inter, self.guild_id, self.user_id, self.missing_item)

        # Execute rewards / flags from this choice
        if self.choice.reward_money:
            await self.cog.economy.credit(self.guild_id, self.user_id, self.choice.reward_money)
        if self.choice.reward_item:
            await self.cog.add_item(self.guild_id, self.user_id, self.choice.reward_item)
        if self.choice.flag_set:
            await self.cog.set_flag(self.guild_id, self.user_id, self.choice.flag_set, True)

        # Move to next node
        await self.cog.set_node(self.guild_id, self.user_id, self.adventure.name, self.choice.next)
        await self.cog.render_node(inter, self.adventure, self.choice.next, replace=True)


class BuyRequiredItemButton(discord.ui.Button):
    def __init__(self, item_name: str, cog: "TextAdventure", guild_id: int, user_id: int):
        super().__init__(label=f"Buy {item_name}", style=discord.ButtonStyle.success)
        self.item_name = item_name
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, inter: discord.Interaction):
        if inter.user.id != self.user_id:
            return await inter.response.send_message("This is not your purchase.", ephemeral=True)
        await self.cog.offer_purchase(inter, self.guild_id, self.user_id, self.item_name)


class ChoiceView(discord.ui.View):
    def __init__(
        self,
        cog: "TextAdventure",
        adventure: Adventure,
        node: Node,
        guild_id: int,
        user_id: int,
        missing_requirements: List[str],
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.adventure = adventure
        self.node = node
        self.guild_id = guild_id
        self.user_id = user_id

        # Build choice buttons. Disable those that require missing items.
        for ch in (node.choices or []):
            disabled = False
            missing_item = None
            if ch.requires and ch.requires in missing_requirements:
                disabled = True
                missing_item = ch.requires

            label = ch.label
            if ch.requires:
                label = f"{label} ({ch.requires})"

            self.add_item(ChoiceButton(
                label=label[:80],
                style=discord.ButtonStyle.primary if not disabled else discord.ButtonStyle.secondary,
                choice=ch,
                disabled=disabled,
                cog=cog,
                adventure=adventure,
                guild_id=guild_id,
                user_id=user_id,
                missing_item=missing_item
            ))

        # If node itself requires an item and it's missing, offer a buy button too
        if node.requires and node.requires in missing_requirements:
            self.add_item(BuyRequiredItemButton(
                item_name=node.requires,
                cog=cog,
                guild_id=guild_id,
                user_id=user_id,
            ))


# =============================================================================
# The Cog
# =============================================================================

class TextAdventure(commands.Cog):
    """
    Text Adventure engine:
      - Adventures in JSON files (data/adventures/*.json)
      - Player state in SQLite (data/adventure_state.sqlite3)
      - Enforces 'requires' for nodes/choices
      - Inline purchase if a required item exists in shop
    """
    def __init__(
        self,
        bot: commands.Bot,
        data_dir: str = "data/adventures",
        db_path: str = "data/adventure_state.sqlite3",
    ):
        self.bot = bot
        self.data_dir = data_dir
        self.db_path = db_path
        self.economy = EconomyAdapter(bot)

    # ---------- DB lifecycle ----------
    async def cog_load(self):
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS player_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                adventure_name TEXT NOT NULL,
                node_id TEXT NOT NULL,
                inventory TEXT NOT NULL,   -- JSON list
                flags TEXT NOT NULL,       -- JSON dict
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            """)
            await db.commit()

    # ---------- Storage helpers ----------
    async def _fetch_state(self, guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT adventure_name, node_id, inventory, flags, started_at, updated_at "
                "FROM player_state WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)
            )
            row = await cur.fetchone()
            await cur.close()
        if not row:
            return None
        return {
            "adventure_name": row[0],
            "node_id": row[1],
            "inventory": json.loads(row[2]),
            "flags": json.loads(row[3]),
            "started_at": row[4],
            "updated_at": row[5],
        }

    async def _upsert_state(
        self,
        guild_id: int,
        user_id: int,
        adventure_name: str,
        node_id: str,
        inventory: List[str],
        flags: Dict[str, Any],
    ) -> None:
        now = time.time()
        payload = (guild_id, user_id, adventure_name, node_id, json.dumps(inventory), json.dumps(flags), now, now)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            INSERT INTO player_state (guild_id, user_id, adventure_name, node_id, inventory, flags, started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                adventure_name=excluded.adventure_name,
                node_id=excluded.node_id,
                inventory=excluded.inventory,
                flags=excluded.flags,
                updated_at=excluded.updated_at
            """, payload)
            await db.commit()

    # Inventory & flags API
    async def add_item(self, guild_id: int, user_id: int, item: str) -> None:
        st = await self._fetch_state(guild_id, user_id)
        if not st:
            return
        inv = st["inventory"]
        inv.append(item)
        await self._upsert_state(guild_id, user_id, st["adventure_name"], st["node_id"], inv, st["flags"])

    async def has_item(self, guild_id: int, user_id: int, item: str) -> bool:
        st = await self._fetch_state(guild_id, user_id)
        return bool(st and item in st["inventory"])

    async def set_flag(self, guild_id: int, user_id: int, key: str, value: Any) -> None:
        st = await self._fetch_state(guild_id, user_id)
        if not st:
            return
        flags = st["flags"]
        flags[key] = value
        await self._upsert_state(guild_id, user_id, st["adventure_name"], st["node_id"], st["inventory"], flags)

    async def set_node(self, guild_id: int, user_id: int, adv_name: str, node_id: str) -> None:
        st = await self._fetch_state(guild_id, user_id)
        if not st:
            st = {"inventory": [], "flags": {}}
        await self._upsert_state(guild_id, user_id, adv_name, node_id, st["inventory"], st["flags"])

    # ---------- Content loading ----------
    def _list_plots(self) -> List[str]:
        files = []
        if os.path.isdir(self.data_dir):
            for fname in os.listdir(self.data_dir):
                if fname.endswith(".json"):
                    files.append(os.path.splitext(fname)[0])
        return sorted(files, key=str.lower)

    def _load_plot(self, plot_name: str) -> Optional[Adventure]:
        path = os.path.join(self.data_dir, f"{plot_name}.json")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Adventure.from_dict(data)

    # ---------- Rendering ----------
    async def render_node(self, inter: discord.Interaction, adv: Adventure, node_id: str, replace: bool = False):
        node = adv.nodes[node_id]

        # Determine missing requirements (node + choices)
        missing_requirements: List[str] = []
        if node.requires and not await self.has_item(inter.guild_id, inter.user.id, node.requires):
            missing_requirements.append(node.requires)

        for ch in (node.choices or []):
            if ch.requires:
                has = await self.has_item(inter.guild_id, inter.user.id, ch.requires)
                if not has and ch.requires not in missing_requirements:
                    missing_requirements.append(ch.requires)

        emb = discord.Embed(title=f"{adv.name} — {node.id}", description=node.text)
        if node.end:
            emb.set_footer(text="The End")
        else:
            if missing_requirements:
                emb.add_field(
                    name="Missing requirements",
                    value=", ".join(missing_requirements),
                    inline=False
                )

        view = None
        if not node.end:
            view = ChoiceView(self, adv, node, inter.guild_id, inter.user.id, missing_requirements)

        if replace:
            await inter.edit_original_response(embed=emb, view=view)
        else:
            await inter.followup.send(embed=emb, view=view, ephemeral=True)

    async def offer_purchase(self, inter: discord.Interaction, guild_id: int, user_id: int, item_name: str):
        # Look up item in your shop
        meta = await self.economy.shop_get_item(guild_id, item_name)
        if not meta:
            return await inter.response.send_message(
                f"⚠️ `{item_name}` is required, but it isn’t sold in the shop.", ephemeral=True
            )

        price = int(meta["price"])
        can = await self.economy.can_afford(guild_id, user_id, price)
        if not can:
            bal = await self.economy.get_balance(guild_id, user_id)
            return await inter.response.send_message(
                f"❌ You need `{price}` credits for **{item_name}**. Your balance: `{bal}`.",
                ephemeral=True
            )

        view = discord.ui.View()

        async def do_buy_callback(_inter: discord.Interaction):
            if _inter.user.id != user_id:
                return await _inter.response.send_message("Not your purchase.", ephemeral=True)

            try:
                await self.economy.debit(guild_id, user_id, price)  # raises if funds changed/insufficient
            except Exception:
                bal2 = await self.economy.get_balance(guild_id, user_id)
                return await _inter.response.send_message(
                    f"❌ Purchase failed: insufficient funds. Balance: `{bal2}`.",
                    ephemeral=True
                )

            await self.add_item(guild_id, user_id, item_name)
            await _inter.response.send_message(
                f"✅ Purchased **{item_name}** for `{price}`. Try your action again.",
                ephemeral=True
            )

        buy_btn = discord.ui.Button(label=f"Buy {item_name} for {price}", style=discord.ButtonStyle.success)
        buy_btn.callback = do_buy_callback
        view.add_item(buy_btn)

        await inter.response.send_message(
            f"`{item_name}` is required for that action.", view=view, ephemeral=True
        )

    # =============================================================================
    # Slash Commands
    # =============================================================================
    adv_group = app_commands.Group(name="adventure", description="Text adventures")

    @adv_group.command(name="plots", description="List available adventures.")
    async def plots(self, inter: discord.Interaction):
        plots = self._list_plots()
        if not plots:
            return await inter.response.send_message("No adventures found.", ephemeral=True)
        await inter.response.send_message("Available plots:\n• " + "\n• ".join(plots), ephemeral=True)

    @adv_group.command(name="start", description="Start a new adventure or restart the current one.")
    @app_commands.describe(plot="Name of the adventure (see /adventure plots)")
    async def start(self, inter: discord.Interaction, plot: str):
        adv = self._load_plot(plot)
        if not adv:
            return await inter.response.send_message(f"Unknown plot `{plot}`.", ephemeral=True)

        await inter.response.defer(ephemeral=True, thinking=True)
        await self.set_node(inter.guild_id, inter.user.id, adv.name, adv.start)
        # fresh inventory/flags on (re)start
        await self._upsert_state(inter.guild_id, inter.user.id, adv.name, adv.start, [], {})
        await self.render_node(inter, adv, adv.start)

    @adv_group.command(name="continue", description="Continue your current adventure.")
    async def cont(self, inter: discord.Interaction):
        st = await self._fetch_state(inter.guild_id, inter.user.id)
        if not st:
            return await inter.response.send_message(
                "You have no active adventure. Use `/adventure start`.", ephemeral=True
            )

        adv = self._load_plot(st["adventure_name"])
        if not adv:
            return await inter.response.send_message("Your adventure is missing on disk.", ephemeral=True)

        await inter.response.defer(ephemeral=True)
        await self.render_node(inter, adv, st["node_id"])

    @adv_group.command(name="inventory", description="Show your inventory & flags.")
    async def inventory(self, inter: discord.Interaction):
        st = await self._fetch_state(inter.guild_id, inter.user.id)
        if not st:
            return await inter.response.send_message("No active adventure.", ephemeral=True)
        inv = st["inventory"] or []
        flags = st["flags"] or {}
        emb = discord.Embed(title="Inventory", description=", ".join(inv) if inv else "Empty")
        if flags:
            emb.add_field(name="Flags", value=", ".join(f"{k}={v}" for k, v in flags.items()), inline=False)
        await inter.response.send_message(embed=emb, ephemeral=True)

    @adv_group.command(name="quit", description="Abandon your current run.")
    async def quit(self, inter: discord.Interaction):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM player_state WHERE guild_id=? AND user_id=?", (inter.guild_id, inter.user.id))
            await db.commit()
        await inter.response.send_message("Your adventure run was cleared.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TextAdventure(bot))
