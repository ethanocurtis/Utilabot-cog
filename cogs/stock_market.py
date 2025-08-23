# cogs/stock_market.py
# Fictional Stock Market Sim Cog (uses your economy_helpers directly)
# - 50 synthetic companies (persisted to data/market_state.json)
# - Price simulation loop (random walk + temporary events with impact/decay)
# - Interactive UI: list/paging, search, detail, Buy/Sell modals
# - Portfolio with P/L
# - /stocks_top (net worth leaderboard), /stocks_movers (gainers/losers)
# - /stocks_config (set/view announce channel, admin)
# - /stocks_sub (user DM subscription to market events)

from __future__ import annotations
import asyncio
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---- Your helpers ----
from utils.economy_helpers import (
    with_session, get_balance as eh_get, charge, payout
)
from utils.common import ensure_user

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MARKET_FILE = DATA_DIR / "market_state.json"
PORTFOLIO_FILE = DATA_DIR / "portfolios.json"
CONFIG_FILE = DATA_DIR / "stock_config.json"

PRICE_TICK_MINUTES = 15         # how often prices change
EVENT_EVERY_HOURS = 2           # how often to roll a possible event
EVENT_CHANCE = 0.55             # chance an event actually spawns when checked
EVENT_DURATION_TICKS = 6        # how many price ticks an event lingers/decays

RANDOM_SEED = 1337              # deterministic initial seed
PAGE_SIZE = 10                  # rows per page in UI
MAX_SYMBOL_LEN = 6

# ---------- Data Models ----------

@dataclass
class EventEffect:
    kind: str               # 'company' or 'sector'
    target: str             # symbol or sector name
    impact: float           # multiplicative drift per tick, e.g. +0.08 or -0.12
    ticks_left: int         # duration left

@dataclass
class Company:
    symbol: str
    name: str
    sector: str
    price: float
    last_price: float
    volatility: float       # e.g. 0.02 low, 0.06 high
    drift: float = 0.0      # baseline drift over time (small)
    effects: List[EventEffect] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "name": self.name, "sector": self.sector,
            "price": self.price, "last_price": self.last_price,
            "volatility": self.volatility, "drift": self.drift,
            "effects": [e.__dict__ for e in self.effects],
        }

    @staticmethod
    def from_dict(d: dict) -> "Company":
        c = Company(
            symbol=d["symbol"], name=d["name"], sector=d["sector"],
            price=float(d["price"]), last_price=float(d.get("last_price", d["price"])),
            volatility=float(d["volatility"]), drift=float(d.get("drift", 0.0)),
        )
        c.effects = [EventEffect(**e) for e in d.get("effects", [])]
        return c

# ---------- Persistence ----------

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)

# ---------- Market Seeder ----------

SECTORS = ["Tech", "Food", "Energy", "Games", "Shipping", "Finance", "Media", "Health", "Industrial", "Retail"]

NAME_LEFT = [
    "Aurora", "Nimbus", "Vertex", "Quantum", "Solstice", "Lumen", "Cascade", "Pioneer", "Stellar", "Cinder",
    "Horizon", "Nova", "Echo", "Atlas", "Summit", "Drift", "Apex", "Harbor", "Forge", "Beacon",
    "Mariner", "Falcon", "Boreal", "Keystone", "Monarch", "Nebula", "Paragon", "Riverton", "Silver", "Zenith"
]
NAME_RIGHT = ["Tech", "Foods", "Energy", "Play", "Lines", "Capital", "Media", "Health", "Works", "Retail"]

def _make_ticker(name: str) -> str:
    base = "".join([c for c in name.upper() if c.isalpha()])
    if len(base) <= MAX_SYMBOL_LEN:
        return base
    parts = name.upper().split()
    if len(parts) >= 2:
        left = parts[0][:3]
        right = parts[1][:3]
        return (left + right)[:MAX_SYMBOL_LEN]
    return base[:MAX_SYMBOL_LEN]

def seed_companies(n=50) -> List[Company]:
    rnd = random.Random(RANDOM_SEED)
    companies: List[Company] = []
    used = set()
    # distribute price & volatility bands
    # 10 ultra (1000–2500), 15 high (250–800), 15 mid (50–200), 10 low (5–30)
    bands = (
        [(rnd.uniform(1000, 2500), rnd.uniform(0.015, 0.035)) for _ in range(10)] +
        [(rnd.uniform(250, 800), rnd.uniform(0.020, 0.045)) for _ in range(15)] +
        [(rnd.uniform(50, 200), rnd.uniform(0.018, 0.040)) for _ in range(15)] +
        [(rnd.uniform(5, 30), rnd.uniform(0.025, 0.060)) for _ in range(10)]
    )
    while len(companies) < n and bands:
        left = rnd.choice(NAME_LEFT)
        right = rnd.choice(NAME_RIGHT)
        sec = rnd.choice(SECTORS)
        name = f"{left} {right}"
        sym = _make_ticker(name.replace(" ", ""))[:MAX_SYMBOL_LEN]
        # ensure uniqueness
        i = 0
        base = sym
        while sym in used:
            i += 1
            sym = (base[:MAX_SYMBOL_LEN - len(str(i))] + str(i))[:MAX_SYMBOL_LEN]
        used.add(sym)
        price, vol = bands.pop(0)
        companies.append(Company(
            symbol=sym, name=name, sector=sec,
            price=round(price, 2), last_price=round(price, 2),
            volatility=vol, drift=rnd.uniform(-0.002, 0.003)
        ))
    return companies

def load_market() -> Dict[str, Company]:
    data = _load_json(MARKET_FILE, {})
    if not data:
        comps = seed_companies(50)
        market = {c.symbol: c for c in comps}
        _save_json(MARKET_FILE, {k: v.to_dict() for k, v in market.items()})
        return market
    return {k: Company.from_dict(v) for k, v in data.items()}

def save_market(market: Dict[str, Company]):
    _save_json(MARKET_FILE, {k: v.to_dict() for k, v in market.items()})

def load_portfolios() -> Dict[str, dict]:
    return _load_json(PORТFOLIO_FILE, {})  # <-- NOTE: typo guard below will fix
# Fix a potential typo in case someone pastes wrong: ensure correct file var used
PORTFOLIO_FILE = DATA_DIR / "portfolios.json"  # re-affirm

def load_portfolios() -> Dict[str, dict]:
    return _load_json(PORTFOLIO_FILE, {})

def save_portfolios(portfolios: Dict[str, dict]):
    _save_json(PORTFOLIO_FILE, portfolios)

# ---------- UI Helpers ----------

def fmt_money(x: float) -> str:
    s = f"{x:,.2f}"
    return s[:-3] if s.endswith(".00") else s

def trend_arrow(cur: float, prev: float) -> str:
    if cur > prev: return "📈"
    if cur < prev: return "📉"
    return "➖"

def symbol_autocomplete_list(itx: discord.Interaction, query: str, max_items=20) -> List[app_commands.Choice[str]]:
    cog = itx.client.get_cog("StockMarket")
    market = cog.market if cog else {}
    q = (query or "").lower().strip()
    items = []
    for c in market.values():
        if q in c.symbol.lower() or q in c.name.lower():
            items.append(app_commands.Choice(name=f"{c.symbol} — {c.name}", value=c.symbol))
        if len(items) >= max_items:
            break
    if not items:
        top = sorted(market.values(), key=lambda c: c.price, reverse=True)[:max_items]
        items = [app_commands.Choice(name=f"{c.symbol} — {c.name}", value=c.symbol) for c in top]
    return items

# ---------- Cog ----------

class StockMarket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.market: Dict[str, Company] = load_market()
        self.portfolios: Dict[str, dict] = load_portfolios()
        self.lock = asyncio.Lock()

        # config (announce channel + DM subscribers)
        self.announce_channel_id: Optional[int] = None
        self.subscribers: set[int] = set()
        self._load_config()

        # tasks
        self._price_loop.start()
        self._event_loop.start()

    # ---------- Config persistence ----------
    def _load_config(self):
        data = _load_json(CONFIG_FILE, {})
        self.announce_channel_id = data.get("announce_channel_id")
        subs = data.get("subscribers", [])
        if isinstance(subs, list):
            try:
                self.subscribers = {int(u) for u in subs}
            except Exception:
                self.subscribers = set()
        else:
            self.subscribers = set()

    def _save_config(self):
        _save_json(CONFIG_FILE, {
            "announce_channel_id": self.announce_channel_id,
            "subscribers": sorted(list(self.subscribers))
        })

    def cog_unload(self):
        self._price_loop.cancel()
        self._event_loop.cancel()

    # ----- Background loops -----

    @tasks.loop(minutes=PRICE_TICK_MINUTES)
    async def _price_loop(self):
        async with self.lock:
            rnd = random.Random()
            for c in self.market.values():
                base_mu = c.drift
                # sum active effects on this company (company-wide or sector)
                effect_mu = 0.0
                remaining_effects = []
                for eff in c.effects:
                    if (eff.kind == "company" and eff.target == c.symbol) or (eff.kind == "sector" and eff.target == c.sector):
                        effect_mu += eff.impact
                        eff.ticks_left -= 1
                        if eff.ticks_left > 0:
                            # decay impact over time
                            eff.impact *= 0.75
                            remaining_effects.append(eff)
                c.effects = remaining_effects

                # random walk with volatility
                shock = rnd.gauss(mu=0.0, sigma=c.volatility)
                mu = base_mu + effect_mu
                pct_change = mu + shock
                new_price = max(0.50, c.price * (1.0 + pct_change))
                c.last_price = c.price
                c.price = round(new_price, 2)

            save_market(self.market)

    @_price_loop.before_loop
    async def _before_price_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=EVENT_EVERY_HOURS)
    async def _event_loop(self):
        if random.random() > EVENT_CHANCE:
            return
        async with self.lock:
            # 60% company, 40% sector
            if random.random() < 0.6:
                tgt = random.choice(list(self.market.values()))
                kind = "company"
                target = tgt.symbol
            else:
                kind = "sector"
                target = random.choice(SECTORS)

            scenario = random.choice([
                ("Product launch hype 🚀", +0.10),
                ("Earnings beat 💹", +0.08),
                ("Analyst upgrade ⭐", +0.06),
                ("Supply chain issues 🧱", -0.07),
                ("Regulatory setback ⚖️", -0.09),
                ("Data breach 🔓", -0.08),
                ("Patent win 🧠", +0.07),
                ("Short squeeze 🔥", +0.12),
                ("PR scandal 🗞️", -0.10),
                ("New partnership 🤝", +0.05),
            ])
            label, impact = scenario

            affected = []
            for c in self.market.values():
                if (kind == "company" and c.symbol == target) or (kind == "sector" and c.sector == target):
                    c.effects.append(EventEffect(kind=kind, target=target, impact=impact, ticks_left=EVENT_DURATION_TICKS))
                    affected.append(c.symbol)

            save_market(self.market)

        # announce (outside lock)
        await self._announce_event(kind, target, label, impact, affected)

    @_event_loop.before_loop
    async def _before_event_loop(self):
        await self.bot.wait_until_ready()

    async def _announce_event(self, kind: str, target: str, label: str, impact: float, affected_symbols: List[str]):
        # Channel announce
        if self.announce_channel_id:
            channel = self.bot.get_channel(self.announce_channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    await channel.send(
                        f"📢 **Market Event** — {('Company' if kind=='company' else 'Sector')} **{target}**\n"
                        f"{label} (impact {impact:+.0%}). Affected: {', '.join(affected_symbols[:10])}"
                        f"{' …' if len(affected_symbols) > 10 else ''}"
                    )
                except Exception:
                    pass
        # DM subscribers
        if self.subscribers:
            msg = (
                f"📢 **Market Event** — {('Company' if kind=='company' else 'Sector')} **{target}**\n"
                f"{label} (impact {impact:+.0%})."
            )
            for uid in list(self.subscribers):
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if user:
                    try:
                        await user.send(msg)
                    except Exception:
                        # if DM fails (privacy), drop them from list quietly
                        self.subscribers.discard(uid)
                        self._save_config()

    # ----- Portfolio helpers -----

    def _get_user_pf(self, user_id: int) -> dict:
        pf = self.portfolios.get(str(user_id))
        if not pf:
            pf = {"holdings": {}, "realized_pl": 0.0}
            self.portfolios[str(user_id)] = pf
        return pf

    def _save_pf(self):
        save_portfolios(self.portfolios)

    def _holdings_value(self, user_id: int) -> float:
        pf = self._get_user_pf(user_id)
        total = 0.0
        for sym, h in pf.get("holdings", {}).items():
            if h.get("shares", 0) <= 0:
                continue
            c = self.market.get(sym)
            if not c:
                continue
            total += c.price * h["shares"]
        return total

    def _net_worth(self, session, user_id: int) -> Tuple[float, float, int]:
        """Return (holdings_value, cash_balance, user_id)."""
        ensure_user(session, user_id)
        cash = eh_get(session, user_id)
        hv = self._holdings_value(user_id)
        return hv, float(cash), user_id

    # ----- Commands -----

    @app_commands.command(name="stocks", description="Browse the fictional market (interactive).")
    async def stocks_browser(self, interaction: discord.Interaction, query: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            view = MarketView(self, query=query)
            embed = view.page_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="stock_buy", description="Buy shares of a company.")
    @app_commands.describe(symbol="Ticker symbol (autocomplete)", quantity="How many shares to buy")
    @app_commands.autocomplete(symbol=lambda itx, cur: symbol_autocomplete_list(itx, cur))
    async def stock_buy(self, interaction: discord.Interaction, symbol: str, quantity: int):
        await self._do_buy(interaction, symbol, quantity)

    @app_commands.command(name="stock_sell", description="Sell shares of a company.")
    @app_commands.describe(symbol="Ticker symbol (autocomplete)", quantity="How many shares to sell")
    @app_commands.autocomplete(symbol=lambda itx, cur: symbol_autocomplete_list(itx, cur))
    async def stock_sell(self, interaction: discord.Interaction, symbol: str, quantity: int):
        await self._do_sell(interaction, symbol, quantity)

    @app_commands.command(name="portfolio", description="View your portfolio & P/L.")
    async def portfolio_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            pf = self._get_user_pf(interaction.user.id)
            embed = self._portfolio_embed(interaction.user, pf)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stock_search", description="Search companies by symbol or name.")
    async def stock_search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            q = query.lower().strip()
            matches = [c for c in self.market.values() if q in c.symbol.lower() or q in c.name.lower()]
            matches = sorted(matches, key=lambda c: c.symbol)[:25]
            if not matches:
                await interaction.followup.send(f"🔎 No results for `{query}`.", ephemeral=True)
                return
            lines = []
            for c in matches:
                arrow = trend_arrow(c.price, c.last_price)
                lines.append(f"**{c.symbol}** · {c.name} · {c.sector} — {arrow} {fmt_money(c.price)}")
            embed = discord.Embed(title=f"Search results for “{query}”", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- Leaderboard & Movers ---

    @app_commands.command(name="stocks_top", description="Show top users by net worth (cash + holdings).")
    @app_commands.describe(limit="How many users to show (default 10, max 25)")
    async def stocks_top(self, interaction: discord.Interaction, limit: Optional[int] = 10):
        limit = max(1, min(int(limit or 10), 25))
        await interaction.response.defer(ephemeral=True)

        async with self.lock:
            user_ids = {int(uid) for uid in self.portfolios.keys()}
            user_ids.add(interaction.user.id)

        rows: List[Tuple[float, float, int]] = []
        with with_session(self.bot.SessionLocal) as session:
            for uid in user_ids:
                hv, cash, _ = self._net_worth(session, uid)
                rows.append((hv, cash, uid))

        rows.sort(key=lambda r: (r[0] + r[1]), reverse=True)
        rows = rows[:limit]

        lines = []
        rank = 1
        for hv, cash, uid in rows:
            total = hv + cash
            member = interaction.guild.get_member(uid) if interaction.guild else None
            name = member.display_name if member else f"User {uid}"
            lines.append(f"**#{rank}** — **{name}** · Total **{fmt_money(total)}** *(Cash {fmt_money(cash)} • Hold {fmt_money(hv)})*")
            rank += 1

        if not lines:
            await interaction.followup.send("No participants yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🏆 Top Net Worth",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stocks_movers", description="Show top gainers and losers since last tick.")
    @app_commands.describe(limit="How many per side (default 5, max 15)")
    async def stocks_movers(self, interaction: discord.Interaction, limit: Optional[int] = 5):
        limit = max(1, min(int(limit or 5), 15))
        await interaction.response.defer(ephemeral=True)

        async with self.lock:
            def pct(c: Company) -> float:
                return 0.0 if c.last_price <= 0 else (c.price - c.last_price) / c.last_price

            comps = list(self.market.values())
            comps_nonflat = [c for c in comps if c.last_price > 0 and c.price != c.last_price]
            if not comps_nonflat:
                await interaction.followup.send("No movers yet — check back after a price tick.", ephemeral=True)
                return

            gainers = sorted(comps_nonflat, key=lambda c: pct(c), reverse=True)[:limit]
            losers  = sorted(comps_nonflat, key=lambda c: pct(c))[:limit]

            def lines(items: List[Company]) -> List[str]:
                out = []
                for c in items:
                    p = pct(c)
                    arrow = "📈" if p > 0 else "📉"
                    out.append(f"{arrow} **{c.symbol}** {fmt_money(c.price)} ({p:+.2%}) — {c.name}")
                return out

            emb = discord.Embed(title="🚥 Market Movers", color=discord.Color.blurple())
            emb.add_field(name=f"Top {len(gainers)} Gainers", value="\n".join(lines(gainers)), inline=False)
            emb.add_field(name=f"Top {len(losers)} Losers",  value="\n".join(lines(losers)),  inline=False)

        await interaction.followup.send(embed=emb, ephemeral=True)

    # --- Config & Subscriptions ---

    @app_commands.command(name="stocks_config", description="Set or view the market announce channel (admins).")
    @app_commands.describe(channel="Channel for event announcements")
    async def stocks_config(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        # Permission check (works for both text & slash)
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need **Manage Server** to change this.", ephemeral=True)
            return

        if channel:
            self.announce_channel_id = channel.id
            self._save_config()
            await interaction.response.send_message(f"✅ Market events will announce in {channel.mention}.", ephemeral=True)
        else:
            if self.announce_channel_id:
                ch = interaction.guild.get_channel(self.announce_channel_id)
                if ch:
                    await interaction.response.send_message(f"📢 Current announce channel: {ch.mention}", ephemeral=True)
                    return
            await interaction.response.send_message("No announce channel is set.", ephemeral=True)

    @app_commands.command(name="stocks_sub", description="Subscribe or unsubscribe from DM alerts for market events.")
    @app_commands.describe(on="True to subscribe, False to unsubscribe")
    async def stocks_sub(self, interaction: discord.Interaction, on: bool):
        uid = interaction.user.id
        if on:
            self.subscribers.add(uid)
            self._save_config()
            await interaction.response.send_message("✅ Subscribed. I’ll DM you when market events happen.", ephemeral=True)
        else:
            self.subscribers.discard(uid)
            self._save_config()
            await interaction.response.send_message("✅ Unsubscribed from market event DMs.", ephemeral=True)

    # ----- Trading logic -----

    async def _do_buy(self, interaction: discord.Interaction, symbol: str, quantity: int):
        if quantity <= 0:
            await interaction.response.send_message("Quantity must be positive.", ephemeral=True)
            return

        async with self.lock:
            c = self.market.get(symbol.upper())
            if not c:
                await interaction.response.send_message("Unknown symbol.", ephemeral=True)
                return
            total_cost = int(round(c.price * quantity))

        # Economy ops (outside lock)
        with with_session(self.bot.SessionLocal) as session:
            ensure_user(session, interaction.user.id)
            bal = eh_get(session, interaction.user.id)
            if bal < total_cost:
                await interaction.response.send_message(
                    f"❌ Insufficient funds. Need **{fmt_money(total_cost)}**, you have **{fmt_money(bal)}**.",
                    ephemeral=True
                )
                return
            try:
                charge(session, interaction.user.id, total_cost)  # debit
            except Exception:
                await interaction.response.send_message("❌ Payment failed.", ephemeral=True)
                return

        # Update portfolio
        async with self.lock:
            pf = self._get_user_pf(interaction.user.id)
            h = pf["holdings"].get(c.symbol, {"shares": 0, "cost_basis": 0.0})
            new_shares = h["shares"] + quantity
            new_cb = 0.0 if new_shares <= 0 else (h["cost_basis"] * h["shares"] + c.price * quantity) / new_shares
            pf["holdings"][c.symbol] = {"shares": new_shares, "cost_basis": new_cb}
            self._save_pf()

        await interaction.response.send_message(
            f"✅ Bought **{quantity}** × **{c.symbol}** at **{fmt_money(c.price)}** each "
            f"(total **{fmt_money(total_cost)}**).",
            ephemeral=True
        )

    async def _do_sell(self, interaction: discord.Interaction, symbol: str, quantity: int):
        if quantity <= 0:
            await interaction.response.send_message("Quantity must be positive.", ephemeral=True)
            return

        async with self.lock:
            c = self.market.get(symbol.upper())
            if not c:
                await interaction.response.send_message("Unknown symbol.", ephemeral=True)
                return
            pf = self._get_user_pf(interaction.user.id)
            h = pf["holdings"].get(c.symbol)
            if not h or h["shares"] < quantity:
                owned = 0 if not h else h["shares"]
                await interaction.response.send_message(f"❌ You only own **{owned}** shares of **{c.symbol}**.", ephemeral=True)
                return

            proceeds = int(round(c.price * quantity))
            cost = h["cost_basis"] * quantity
            realized = c.price * quantity - cost
            pf["realized_pl"] += realized
            h["shares"] -= quantity
            if h["shares"] == 0:
                h["cost_basis"] = 0.0
            pf["holdings"][c.symbol] = h
            self._save_pf()

        # Pay the user (outside lock)
        with with_session(self.bot.SessionLocal) as session:
            ensure_user(session, interaction.user.id)
            payout(session, interaction.user.id, proceeds)

        pnl = "profit" if realized >= 0 else "loss"
        await interaction.response.send_message(
            f"✅ Sold **{quantity}** × **{c.symbol}** at **{fmt_money(c.price)}** "
            f"for **{fmt_money(proceeds)}** (**{pnl} {fmt_money(abs(realized))}**).",
            ephemeral=True
        )

    # ----- Embeds / Views -----

    def _portfolio_embed(self, user: discord.User | discord.Member, pf: dict) -> discord.Embed:
        holdings = pf.get("holdings", {})
        lines = []
        total_value = 0.0
        for sym, h in holdings.items():
            if h["shares"] <= 0:
                continue
            c = self.market.get(sym)
            if not c:
                continue
            cur_val = c.price * h["shares"]
            total_value += cur_val
            unreal = (c.price - h["cost_basis"]) * h["shares"]
            arrow = trend_arrow(c.price, c.last_price)
            lines.append(
                f"**{sym}** · {c.name} — {h['shares']} @ {fmt_money(h['cost_basis'])} "
                f"→ {arrow} **{fmt_money(c.price)}** (Unrealized {'+' if unreal>=0 else '-'}{fmt_money(abs(unreal))})"
            )
        desc = "You don't own any stocks yet. Try `/stocks` to browse." if not lines else "\n".join(lines)

        embed = discord.Embed(
            title=f"{user.display_name}'s Portfolio",
            description=desc,
            color=discord.Color.green()
        )
        embed.add_field(name="Portfolio Value", value=f"**{fmt_money(total_value)}**", inline=True)
        embed.add_field(name="Realized P/L", value=f"**{fmt_money(pf.get('realized_pl', 0.0))}**", inline=True)
        return embed

    def _company_embed(self, c: Company) -> discord.Embed:
        arrow = trend_arrow(c.price, c.last_price)
        desc = (
            f"**Sector:** {c.sector}\n"
            f"**Price:** {arrow} {fmt_money(c.price)}  *(prev {fmt_money(c.last_price)})*\n"
            f"**Volatility:** {c.volatility:.3f} · **Drift:** {c.drift:+.3f}\n"
        )
        if c.effects:
            effs = [f"{e.kind}:{e.target} {e.impact:+.2%} ({e.ticks_left}t)" for e in c.effects]
            desc += f"**Active Events:** {', '.join(effs)}\n"
        embed = discord.Embed(
            title=f"{c.symbol} — {c.name}",
            description=desc,
            color=discord.Color.blurple()
        )
        return embed

# ---------- Views ----------

class MarketView(discord.ui.View):
    def __init__(self, cog: StockMarket, query: Optional[str] = None):
        super().__init__(timeout=120)
        self.cog = cog
        self.page = 0
        self.query = (query or "").lower().strip()
        self._refresh_options()

    def _filtered(self) -> List[Company]:
        comps = list(self.cog.market.values())
        if self.query:
            comps = [c for c in comps if self.query in c.symbol.lower() or self.query in c.name.lower() or self.query in c.sector.lower()]
        return sorted(comps, key=lambda c: c.symbol)

    def _refresh_options(self):
        comps = self._filtered()
        start = self.page * PAGE_SIZE
        self.current = comps[start:start + PAGE_SIZE]

    def page_embed(self) -> discord.Embed:
        self._refresh_options()
        if not self.current:
            return discord.Embed(title="Market", description="No matches.", color=discord.Color.red())
        lines = []
        for c in self.current:
            arrow = trend_arrow(c.price, c.last_price)
            lines.append(f"**{c.symbol}** · {c.name} · {c.sector} — {arrow} {fmt_money(c.price)}")
        desc = "\n".join(lines)
        emb = discord.Embed(
            title="📊 Fictional Market",
            description=desc,
            color=discord.Color.gold()
        )
        emb.set_footer(text=f"Page {self.page+1} — Use buttons to navigate • Use Select to open details")
        # refresh select options
        self.select_comp.options = [
            discord.SelectOption(label=f"{c.symbol}", description=f"{c.name[:80]}", value=c.symbol) for c in self.current
        ]
        # toggle buttons
        total = len(self._filtered())
        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = ((self.page + 1) * PAGE_SIZE >= total)
        return emb

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.page_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(embed=self.page_embed(), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.page_embed(), view=self)

    @discord.ui.select(placeholder="Open company…", min_values=1, max_values=1, options=[
        discord.SelectOption(label="temp", value="temp")  # replaced each render
    ])
    async def select_comp(self, interaction: discord.Interaction, select: discord.ui.Select):
        symbol = select.values[0]
        c = self.cog.market.get(symbol)
        if not c:
            await interaction.response.send_message("Symbol vanished, try refresh.", ephemeral=True)
            return
        view = CompanyView(self.cog, c)
        await interaction.response.edit_message(embed=self.cog._company_embed(c), view=view)

class CompanyView(discord.ui.View):
    def __init__(self, cog: StockMarket, company: Company):
        super().__init__(timeout=120)
        self.cog = cog
        self.company = company

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success)
    async def buy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BuyModal(self.cog, self.company.symbol))

    @discord.ui.button(label="Sell", style=discord.ButtonStyle.danger)
    async def sell_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SellModal(self.cog, self.company.symbol))

    @discord.ui.button(label="Back to List", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MarketView(self.cog)
        await interaction.response.edit_message(embed=view.page_embed(), view=view)

class BuyModal(discord.ui.Modal, title="Buy Shares"):
    qty = discord.ui.TextInput(label="Quantity", placeholder="e.g. 5", required=True, min_length=1, max_length=8)

    def __init__(self, cog: StockMarket, symbol: str):
        super().__init__()
        self.cog = cog
        self.symbol = symbol

    async def on_submit(self, interaction: discord.Interaction):
        try:
            quantity = int(str(self.qty))
        except Exception:
            await interaction.response.send_message("Enter a valid integer quantity.", ephemeral=True)
            return
        await self.cog._do_buy(interaction, self.symbol, quantity)

class SellModal(discord.ui.Modal, title="Sell Shares"):
    qty = discord.ui.TextInput(label="Quantity", placeholder="e.g. 5", required=True, min_length=1, max_length=8)

    def __init__(self, cog: StockMarket, symbol: str):
        super().__init__()
        self.cog = cog
        self.symbol = symbol

    async def on_submit(self, interaction: discord.Interaction):
        try:
            quantity = int(str(self.qty))
        except Exception:
            await interaction.response.send_message("Enter a valid integer quantity.", ephemeral=True)
            return
        await self.cog._do_sell(interaction, self.symbol, quantity)

# ---------- Setup ----------

async def setup(bot: commands.Bot):
    """Standard extension entrypoint."""
    await bot.add_cog(StockMarket(bot))
