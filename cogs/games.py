# cogs/games.py
import random
import asyncio
from typing import List, Optional, Callable
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

# ---- economy integration (provided by your project) ----
from utils.common import ensure_user  # must exist in your codebase


# ---------------- Card helpers (Blackjack) ----------------

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
RANK_VALUES = {**{str(i): i for i in range(2, 11)}, "J": 10, "Q": 10, "K": 10, "A": 11}

def _new_deck() -> List[str]:
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def _hand_value(cards: List[str]) -> int:
    total = 0
    aces = 0
    for c in cards:
        rank = c[:-1] if c[:-1] in RANKS else c[0]  # handle "10"
        v = RANK_VALUES.get(rank, 0)
        total += v
        if rank == "A":
            aces += 1
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def _is_blackjack(cards: List[str]) -> bool:
    return len(cards) == 2 and _hand_value(cards) == 21

def _render_cards(cards: List[str], hide_first: bool = False) -> str:
    if hide_first and cards:
        return "🂠 " + " ".join(f"`{c}`" for c in cards[1:])
    return " ".join(f"`{c}`" for c in cards)

def _outcome_text(player_total: int, dealer_total: int) -> str:
    if player_total > 21:
        return "💥 **Bust!** Dealer wins."
    if dealer_total > 21:
        return "🎉 **Dealer busts! You win!**"
    if player_total > dealer_total:
        return "🏆 **You win!**"
    if player_total < dealer_total:
        return "🫤 **Dealer wins.**"
    return "🤝 **Push.**"


# ---------------- Blackjack View ----------------

class BlackjackView(discord.ui.View):
    """
    Interactive blackjack that settles bets via your economy DB.
    We call the provided apply_credit(user_id, delta) coroutine to adjust credits.
    """
    def __init__(
        self,
        *,
        player: discord.Member,
        bet: int,
        apply_credit: Callable[[int, int], "asyncio.Future"],  # (user_id, delta) -> new balance
        timeout: int = 60,
    ):
        super().__init__(timeout=timeout)
        self.player = player
        self.bet = max(0, int(bet))
        self.apply_credit = apply_credit

        self.deck: List[str] = _new_deck()
        self.player_hand: List[str] = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand: List[str] = [self.deck.pop(), self.deck.pop()]
        self.message: Optional[discord.Message] = None
        self.finished = False
        self.paid = False  # guard against double-settlement

    # Fallback for discord.py variants without View.disable_all_items()
    def _disable_all(self):
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Only the game owner can press these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.success, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player_hand.append(self.deck.pop())
        if _hand_value(self.player_hand) >= 21:
            await self._finish_round(interaction)  # auto-resolve on 21/bust
        else:
            await self._update_embed(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.primary, emoji="🛑")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish_round(interaction)

    @discord.ui.button(label="Surrender", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finished = True
        self._disable_all()
        # Lose ceil(bet/2)
        if self.bet > 0 and not self.paid:
            loss = (self.bet + 1) // 2
            await self.apply_credit(self.player.id, -loss)
            self.paid = True

        embed = self._build_embed(reveal_dealer=True)
        embed.add_field(name="Result", value=f"🏳️ **You surrendered.** Lost **{(self.bet + 1)//2}** credits.", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        if self.message and not self.finished:
            try:
                await self._finish_round(None, from_timeout=True)
            except Exception:
                pass

    def _build_embed(self, *, reveal_dealer: bool = False) -> discord.Embed:
        p_total = _hand_value(self.player_hand)
        d_total = _hand_value(self.dealer_hand) if reveal_dealer else None

        title = "🂡 Blackjack"
        if self.finished:
            title += " — Final"
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.set_author(
            name=self.player.display_name,
            icon_url=self.player.display_avatar.url if hasattr(self.player.display_avatar, "url") else discord.Embed.Empty
        )

        dealer_hand_str = _render_cards(self.dealer_hand, hide_first=not reveal_dealer)
        dealer_title = f"Dealer ({d_total if reveal_dealer else '?'})"
        embed.add_field(name=dealer_title, value=dealer_hand_str, inline=False)

        player_hand_str = _render_cards(self.player_hand, hide_first=False)
        embed.add_field(name=f"Your Hand ({p_total})", value=player_hand_str, inline=False)

        if self.bet:
            embed.add_field(name="Bet", value=f"**{self.bet}** credits", inline=True)

        if not self.finished:
            if _is_blackjack(self.player_hand):
                embed.set_footer(text="Blackjack! Press Stand to reveal.")
            else:
                embed.set_footer(text="Click Hit or Stand. Auto-stand in 60s of inactivity.")
        else:
            embed.set_footer(text="Game over. Start a new /blackjack to play again.")
        return embed

    async def _update_embed(self, interaction: Optional[discord.Interaction]):
        embed = self._build_embed(reveal_dealer=False)
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.message.edit(embed=embed, view=self)

    async def _finish_round(self, interaction: Optional[discord.Interaction], *, from_timeout: bool = False):
        self.finished = True
        self._disable_all()

        # Dealer draws to 17+ (stand on soft 17)
        while _hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        p_total = _hand_value(self.player_hand)
        d_total = _hand_value(self.dealer_hand)

        # Settle bet using your economy
        result_line = ""
        if self.bet > 0 and not self.paid:
            if p_total > 21:
                await self.apply_credit(self.player.id, -self.bet)
                result_line = f"💥 Bust. Lost **{self.bet}** credits."
            elif d_total > 21 or p_total > d_total:
                win = int(self.bet * 1.5) if _is_blackjack(self.player_hand) else self.bet
                await self.apply_credit(self.player.id, win)
                result_line = f"🏆 You win **{win}** credits."
            elif p_total == d_total:
                result_line = "🤝 Push. Bet refunded."
            else:
                await self.apply_credit(self.player.id, -self.bet)
                result_line = f"🫤 Dealer wins. Lost **{self.bet}** credits."
            self.paid = True

        embed = self._build_embed(reveal_dealer=True)
        base_outcome = _outcome_text(p_total, d_total)
        if from_timeout and not result_line:
            result_line = "⏱️ Timeout. "

        if result_line:
            embed.add_field(name="Result", value=f"{result_line} {base_outcome}", inline=False)
        else:
            embed.add_field(name="Result", value=base_outcome, inline=False)

        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.message.edit(embed=embed, view=self)


# ---------------- Coinflip View (animated + Reflip) ----------------

class CoinflipView(discord.ui.View):
    def __init__(self, *, bot, player: discord.Member, bet: int, choice: str, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.player = player
        self.bet = max(0, int(bet or 0))
        self.choice = (choice or "heads").lower()
        self.message: Optional[discord.Message] = None
        self._spinning = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Only the game owner can use this button.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message:
            for c in self.children:
                c.disabled = True
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    def _apply_credit_sync(self, user_id: int, delta: int) -> int:
        # sync DB work executed in a thread; returns new balance
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, user_id)
            bal.credits += int(delta)
            s.commit()
            return int(bal.credits)

    async def _animate_and_resolve(self, *, edit_resp):
        """Runs a short coin 'spin' animation and resolves the bet."""
        if self._spinning:  # prevent spam during animation
            return
        self._spinning = True

        frames = ["🪙", "🔄", "🪙", "🔄", "🪙", "🔄", "🪙"]
        for i, f in enumerate(frames):
            try:
                await edit_resp(
                    embed=discord.Embed(
                        title="🪙 Coin Flip",
                        description=f"{f} Flipping…",
                        color=discord.Color.blurple(),
                    ),
                    view=self,
                )
            except Exception:
                pass
            await asyncio.sleep(0.15 if i < len(frames) - 1 else 0.05)

        outcome = random.choice(["heads", "tails"])
        win = (self.choice == outcome)

        # settle bet (even odds)
        result_line = "(No bet placed)"
        new_balance_display = None
        color = discord.Color.green() if win else discord.Color.red()

        if self.bet > 0:
            delta = self.bet if win else -self.bet
            new_balance = await asyncio.to_thread(self._apply_credit_sync, self.player.id, delta)
            result_line = f"🏆 You won **{self.bet}** credits!" if win else f"💸 You lost **{self.bet}** credits."
            new_balance_display = f"{new_balance} credits"

        # final embed
        embed = discord.Embed(
            title="🪙 Coin Flip",
            description=f"The coin landed on **{outcome.title()}**!",
            color=color,
        )
        embed.add_field(name="Your Guess", value=self.choice.title(), inline=True)
        if self.bet > 0:
            embed.add_field(name="Bet", value=f"{self.bet} credits", inline=True)
            embed.add_field(name="Result", value=result_line, inline=False)
            embed.add_field(name="Balance", value=new_balance_display, inline=True)
        else:
            embed.add_field(name="Result", value=result_line, inline=False)
        embed.set_footer(text="Even odds — win +bet / lose −bet")

        await edit_resp(embed=embed, view=self)
        self._spinning = False

    @discord.ui.button(label="Reflip", style=discord.ButtonStyle.primary, emoji="🔁")
    async def reflip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Edit the original message again (same bet & choice)
        if self.message:
            await self._animate_and_resolve(edit_resp=self.message.edit)
        else:
            await self._animate_and_resolve(edit_resp=interaction.edit_original_response)

#-----high low view-----
class HighLowView(discord.ui.View):
    def __init__(self, *, player: discord.Member, base: int, bet: int, apply_credit: Callable[[int, int], "asyncio.Future"], timeout: int = 30):
        super().__init__(timeout=timeout)
        self.player = player
        self.base = int(base)
        self.bet = max(0, int(bet or 0))
        self.apply_credit = apply_credit
        self.message: Optional[discord.Message] = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Only the game owner can use these buttons.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message and not self.finished:
            for c in self.children:
                c.disabled = True
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def _resolve(self, interaction: discord.Interaction, guess: str):
        self.finished = True
        for c in self.children:
            c.disabled = True

        # reveal next number (1..100)
        nxt = random.randint(1, 100)
        actual = "higher" if nxt > self.base else ("lower" if nxt < self.base else "equal")
        win = (guess == actual)

        # settle bet (even odds)
        result_line = "(No bet placed)"
        new_balance_display = None
        if self.bet > 0:
            delta = self.bet if win else -self.bet
            new_balance = await self.apply_credit(self.player.id, delta)
            result_line = f"🏆 You won **{self.bet}** credits!" if win else f"💸 You lost **{self.bet}** credits."
            new_balance_display = f"{new_balance} credits"

        color = discord.Color.green() if win else discord.Color.red()
        embed = discord.Embed(title="🔺 High / 🔻 Low", color=color)
        embed.add_field(name="Base", value=f"**{self.base}**", inline=True)
        embed.add_field(name="Your Guess", value=guess.title(), inline=True)
        embed.add_field(name="Next", value=f"**{nxt}** ({actual})", inline=True)

        if self.bet > 0:
            embed.add_field(name="Result", value=result_line, inline=False)
            embed.add_field(name="Bet", value=f"{self.bet} credits", inline=True)
            embed.add_field(name="Balance", value=new_balance_display, inline=True)
        else:
            embed.add_field(name="Result", value=result_line, inline=False)

        embed.set_footer(text="Even odds — win +bet / lose −bet")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Higher", style=discord.ButtonStyle.success, emoji="🔺")
    async def btn_higher(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "higher")

    @discord.ui.button(label="Equal", style=discord.ButtonStyle.secondary, emoji="➖")
    async def btn_equal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "equal")

    @discord.ui.button(label="Lower", style=discord.ButtonStyle.danger, emoji="🔻")
    async def btn_lower(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "lower")


# ---------------- Games Cog ----------------

class GamesCog(commands.Cog):
    """
    Games cog that integrates with your economy DB (via bot.SessionLocal + ensure_user).
    """

    def __init__(self, bot):
        self.bot = bot

    # economy helper: adjust credits atomically
    async def _apply_credit(self, user_id: int, delta: int) -> int:
        def _work():
            with self.bot.SessionLocal() as s:
                _, bal = ensure_user(s, user_id)
                bal.credits += int(delta)
                s.commit()
                return int(bal.credits)
        return await asyncio.to_thread(_work)

    # economy helper: get current balance
    async def _get_balance(self, user_id: int) -> int:
        def _work():
            with self.bot.SessionLocal() as s:
                _, bal = ensure_user(s, user_id)
                s.commit()
                return int(bal.credits)
        return await asyncio.to_thread(_work)

    # --------- Coinflip (animated + Reflip) ---------

    @app_commands.command(name="coinflip", description="Bet on a coin flip.")
    @app_commands.describe(bet="Bet amount in credits")
    @app_commands.choices(
        choice=[
            app_commands.Choice(name="Heads", value="heads"),
            app_commands.Choice(name="Tails", value="tails"),
        ]
    )
    async def coinflip(self, inter: discord.Interaction, choice: str, bet: int = 0):
        # Validate bet vs current balance
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            current = int(bal.credits)
            if bet < 0:
                return await inter.response.send_message("❌ Bet must be ≥ 0.", ephemeral=True)
            if bet > current:
                return await inter.response.send_message(
                    f"❌ You only have **{current}** credits.", ephemeral=True
                )

        # Send initial message with spinner frame, attach view, then animate
        view = CoinflipView(bot=self.bot, player=inter.user, bet=bet, choice=choice, timeout=30)
        embed = discord.Embed(
            title="🪙 Coin Flip",
            description="🪙 Flipping…",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Your Guess", value=choice.title(), inline=True)
        if bet > 0:
            embed.add_field(name="Bet", value=f"{bet} credits", inline=True)
        await inter.response.send_message(embed=embed, view=view)
        view.message = await inter.original_response()

        # animate and resolve the first flip
        await view._animate_and_resolve(edit_resp=view.message.edit)

    # ---------------- Horse Racing (multiplayer, animated, betting) ----------------

from dataclasses import dataclass, field

TRACK_LENGTH = 26
TICK_SECONDS = 1.1
JOIN_COUNTDOWN_EDIT_EVERY = 5
EMOJIS = ["🐎", "🐴", "🦄", "🐆", "🦓", "🐢"]
FINISH_FLAG = "🏁"
MEDALS = ["🥇", "🥈", "🥉", "🏅"]

def _hr_progress_bar(pos: int, total: int) -> str:
    pos = max(0, min(pos, total))
    return "▰" * pos + "▱" * (total - pos)

@dataclass
class HRRacer:
    user_id: int
    display: str
    emoji: str
    bet: int
    pos: int = 0
    boosted: bool = False

@dataclass
class HRState:
    guild_id: int
    channel_id: int
    host_id: int
    min_bet: int
    max_bet: int
    max_players: int
    join_seconds: int
    message_id: int | None = None
    lobby_open: bool = True
    racers: dict[int, HRRacer] = field(default_factory=dict)
    pot: int = 0
    running: bool = False
    finished: bool = False
    winners: list[int] = field(default_factory=list)

class HRBetModal(discord.ui.Modal, title="Join Horse Race"):
    bet_amount = discord.ui.TextInput(
        label="Your bet amount",
        style=discord.TextStyle.short,
        placeholder="e.g., 250",
        required=True,
    )
    def __init__(self, cog: "GamesCog", state: HRState):
        super().__init__()
        self.cog = cog
        self.state = state
    async def on_submit(self, interaction: discord.Interaction):
        if not self.state.lobby_open:
            return await interaction.response.send_message("Lobby closed.", ephemeral=True)
        try:
            amt = int(str(self.bet_amount).replace(",", ""))
        except ValueError:
            return await interaction.response.send_message("Enter a valid number.", ephemeral=True)
        if amt < self.state.min_bet:
            return await interaction.response.send_message(f"Minimum bet is {self.state.min_bet}", ephemeral=True)
        if self.state.max_bet and amt > self.state.max_bet:
            return await interaction.response.send_message(f"Maximum bet is {self.state.max_bet}", ephemeral=True)
        bal = await self.cog._get_balance(interaction.user.id)
        if amt > bal:
            return await interaction.response.send_message(f"❌ You only have {bal} credits.", ephemeral=True)
        await self.cog._apply_credit(interaction.user.id, -amt)
        emoji = EMOJIS[len(self.state.racers) % len(EMOJIS)]
        self.state.racers[interaction.user.id] = HRRacer(interaction.user.id, interaction.user.display_name, emoji, amt)
        self.state.pot = sum(r.bet for r in self.state.racers.values())
        await interaction.response.send_message(f"✅ Bet **{amt}** placed!", ephemeral=True)

class HRBoost(discord.ui.Button):
    def __init__(self, state: HRState):
        super().__init__(label="Boost!", style=discord.ButtonStyle.primary, emoji="💨")
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        r = self.state.racers.get(interaction.user.id)
        if not r:
            return await interaction.response.send_message("Not in this race.", ephemeral=True)
        if r.boosted:
            return await interaction.response.send_message("You already boosted.", ephemeral=True)
        r.boosted = True
        r.pos = min(TRACK_LENGTH, r.pos + random.randint(1, 2))
        await interaction.response.send_message("💥 Turbo!", ephemeral=True)

class HRRaceView(discord.ui.View):
    def __init__(self, state: HRState):
        super().__init__(timeout=None)
        self.add_item(HRBoost(state))

# ---- GamesCog horse race methods ----
class GamesCog(commands.Cog):
    ...

    @app_commands.command(name="horserace", description="Start a multiplayer horse race with betting.")
    async def horserace(self, inter: discord.Interaction, min_bet: int = 50, max_bet: int = 0, max_players: int = 6, join_seconds: int = 45):
        state = HRState(inter.guild.id, inter.channel_id, inter.user.id, min_bet, max_bet, max_players, join_seconds)
        emb = self._hr_render_lobby(state)
        view = discord.ui.View()
        join_btn = discord.ui.Button(label="Join", style=discord.ButtonStyle.success, emoji="➕")
        async def _join_cb(i: discord.Interaction): await i.response.send_modal(HRBetModal(self, state))
        join_btn.callback = _join_cb
        view.add_item(join_btn)
        await inter.response.send_message(embed=emb, view=view)
        msg = await inter.original_response()
        state.message_id = msg.id

        # countdown
        for remaining in range(join_seconds, 0, -1):
            if not state.lobby_open: break
            if remaining % 5 == 0 or remaining <= 5:
                await msg.edit(embed=self._hr_render_lobby(state, remaining))
            await asyncio.sleep(1)

        # auto start/solo/cancel
        if len(state.racers) >= 2:
            await msg.edit(embed=self._hr_render_race(state, prestart=True), view=HRRaceView(state))
            await self._hr_run(state, msg)
        elif len(state.racers) == 1:
            lone = next(iter(state.racers.values()))
            await msg.edit(embed=discord.Embed(title="🏇 Solo Run", description=f"{lone.display} races alone! Profit!"), view=HRRaceView(state))
            await self._hr_run(state, msg, solo=True)
        else:
            await msg.edit(embed=discord.Embed(title="No players joined.", color=discord.Color.red()), view=None)

    # --- helpers ---
    def _hr_render_lobby(self, state: HRState, remaining: int | None = None):
        desc = f"**Pot:** {state.pot}\n"
        if remaining: desc += f"⏳ {remaining}s left"
        emb = discord.Embed(title="🏇 Horse Race — Lobby", description=desc, color=discord.Color.blurple())
        if state.racers:
            emb.add_field(name="Players", value="\n".join(f"{r.emoji} {r.display} — {r.bet}" for r in state.racers.values()), inline=False)
        return emb
    def _hr_render_race(self, state: HRState, prestart=False):
        emb = discord.Embed(title="🏇 Horse Race", color=discord.Color.green())
        lines = []
        for r in state.racers.values():
            bar = _hr_progress_bar(r.pos, TRACK_LENGTH)
            lines.append(f"{r.emoji} {r.display}\n`{bar}` {FINISH_FLAG}")
        emb.description = "\n".join(lines)
        return emb
    async def _hr_run(self, state: HRState, msg: discord.Message, solo=False):
        order = list(state.racers.keys())
        while not state.finished:
            for uid in order:
                r = state.racers[uid]
                r.pos = min(TRACK_LENGTH, r.pos + random.randint(0, 3))
            leaders = [r for r in state.racers.values() if r.pos >= TRACK_LENGTH]
            if leaders:
                state.finished = True
                state.winners = [leaders[0].user_id]
            await msg.edit(embed=self._hr_render_race(state), view=HRRaceView(state) if not state.finished else None)
            await asyncio.sleep(TICK_SECONDS)
        # payout
        if solo:
            lone = next(iter(state.racers.values()))
            await self._apply_credit(lone.user_id, lone.bet * 2)  # net profit
        else:
            pot = sum(r.bet for r in state.racers.values())
            win = state.racers[state.winners[0]]
            await self._apply_credit(win.user_id, pot)
        await msg.edit(embed=discord.Embed(title="🏁 Finished!", description="Race over!"), view=None)
    
    # --------- Blackjack ---------

    @app_commands.command(name="blackjack", description="Play blackjack vs. dealer with optional bet.")
    @app_commands.describe(bet="Bet amount in credits (default 0)")
    async def blackjack(self, inter: discord.Interaction, bet: Optional[int] = 0):
        bet = max(0, int(bet or 0))
        if bet > 0:
            bal = await self._get_balance(inter.user.id)
            if bet > bal:
                return await inter.response.send_message(
                    f"❌ You only have **{bal}** credits.", ephemeral=True
                )

        view = BlackjackView(
            player=inter.user,
            bet=bet,
            apply_credit=self._apply_credit,
            timeout=60,
        )
        embed = view._build_embed(reveal_dealer=False)
        await inter.response.send_message(embed=embed, view=view)
        view.message = await inter.original_response()

    # --------- Small extras (no credits) ---------

    @app_commands.command(name="highlow", description="Guess if the next number (1–100) is higher, lower, or equal. Betting supported.")
    @app_commands.describe(bet="Bet amount in credits (default 0)")
    async def highlow(self, inter: discord.Interaction, bet: Optional[int] = 0):
        bet = max(0, int(bet or 0))

        # validate bet vs balance (uses your economy DB)
        with self.bot.SessionLocal() as s:
            _, bal = ensure_user(s, inter.user.id)
            current = int(bal.credits)
            if bet > current:
                return await inter.response.send_message(
                    f"❌ You only have **{current}** credits.", ephemeral=True
                )

        # roll the base number and show it before the guess
        base = random.randint(1, 100)
        view = HighLowView(player=inter.user, base=base, bet=bet, apply_credit=self._apply_credit, timeout=30)

        embed = discord.Embed(title="🔺 High / 🔻 Low", color=discord.Color.blurple())
        embed.add_field(name="Base", value=f"**{base}**", inline=True)
        if bet > 0:
            embed.add_field(name="Bet", value=f"{bet} credits", inline=True)
        embed.set_footer(text="Pick Higher, Lower, or Equal")

        await inter.response.send_message(embed=embed, view=view)
        view.message = await inter.original_response()

    @app_commands.command(name="trivia", description="Quick trivia (True/False).")
    async def trivia(self, inter: discord.Interaction):
        q = random.choice([
            ("The Python language was named after Monty Python.", True),
            ("The capital of Australia is Sydney.", False),
            ("Electrons are larger than atoms.", False),
        ])
        await inter.response.send_message(f"🧠 {q[0]} Answer with **true** or **false** in 15s.")
        try:
            m = await inter.client.wait_for(
                "message",
                timeout=15.0,
                check=lambda x: x.author.id == inter.user.id and x.channel.id == inter.channel.id and x.content.lower() in ("true","false")
            )
        except asyncio.TimeoutError:
            return await inter.followup.send("⏱️ Time!")
        correct = "true" if q[1] else "false"
        await inter.followup.send("✅ Correct!" if m.content.lower() == correct else f"❌ Nope. Correct is **{correct}**.")

async def setup(bot: commands.Bot):
    await bot.add_cog(GamesCog(bot))