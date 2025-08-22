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


# --------- Horse Race (slash) ---------
@app_commands.command(name="horserace", description="Create a multiplayer horse race with betting and animations.")
@app_commands.describe(
    min_bet="Minimum bet per player (default 50)",
    max_bet="Maximum bet (0 = unlimited)",
    max_players="Max players (2–12)",
    join_seconds="Join window in seconds (10–180)",
)
async def horserace(self, inter: discord.Interaction, min_bet: int = 50, max_bet: int = 0, max_players: int = 6, join_seconds: int = 45):
    # per-channel race guard stored on cog (create if missing)
    if not hasattr(self, "_hr_active"):
        self._hr_active = {}
    if inter.channel_id in self._hr_active:
        return await inter.response.send_message("There is already an active race in this channel.", ephemeral=True)

    max_players = max(2, min(12, int(max_players or 6)))
    min_bet = max(1, int(min_bet or 50))
    max_bet = max(0, int(max_bet or 0))
    join_seconds = max(10, min(180, int(join_seconds or 45)))

    state = HRState(
        guild_id=inter.guild.id,
        channel_id=inter.channel_id,
        host_id=inter.user.id,
        min_bet=min_bet,
        max_bet=max_bet,
        max_players=max_players,
        join_seconds=join_seconds,
    )
    self._hr_active[inter.channel_id] = state

    view = self._hr_make_lobby_view(state)
    emb = self._hr_render_lobby(state)
    await inter.response.send_message(embed=emb, view=view)
    msg = await inter.original_response()
    state.message_id = msg.id

    try:
        for remaining in range(join_seconds, 0, -1):
            if not state.lobby_open:
                break
            if remaining % JOIN_COUNTDOWN_EDIT_EVERY == 0 or remaining in (join_seconds, 5, 4, 3, 2, 1):
                await msg.edit(embed=self._hr_render_lobby(state, remaining), view=view)
            await asyncio.sleep(1)

        if state.lobby_open:
            state.lobby_open = False
            if len(state.racers) >= 2:
                state.running = True
                await msg.edit(embed=self._hr_render_race(state, prestart=True), view=self._hr_make_race_view(state))
                await self._hr_run(state)
            elif len(state.racers) == 1:
                only = next(iter(state.racers.values()))
                state.solo_allowed = True
                offer = discord.Embed(
                    title="🏇 Horse Race — Not enough players",
                    description=f"Only **{only.display}** joined.\nYou can **Race Alone** for a chance at a **solo profit (+bet)**, or Cancel to refund.",
                    color=discord.Color.orange(),
                )
                await msg.edit(embed=offer, view=HRSoloView(self, state, only.user_id))
                return
            else:
                em = discord.Embed(
                    title="🏇 Horse Race — No players",
                    description="No one joined. Lobby closed.",
                    color=discord.Color.red(),
                )
                await msg.edit(embed=em, view=None)
                self._hr_active.pop(inter.channel_id, None)
                return
    finally:
        async def _delayed_cleanup():
            await asyncio.sleep(2)
            self._hr_active.pop(inter.channel_id, None)
        asyncio.create_task(_delayed_cleanup())
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
    racers: dict[int, HRRacer] = field(default_factory=dict)  # user_id -> racer
    pot: int = 0
    running: bool = False
    finished: bool = False
    winners: list[int] = field(default_factory=list)  # user_ids
    solo_allowed: bool = False  # set when only 1 player at close

class HRBetModal(discord.ui.Modal, title="Join Horse Race"):
    bet_amount = discord.ui.TextInput(
        label="Your bet amount",
        style=discord.TextStyle.short,
        placeholder="e.g., 250",
        required=True,
        min_length=1,
        max_length=12,
    )
    def __init__(self, cog: "GamesCog", state: HRState):
        super().__init__()
        self.cog = cog
        self.state = state

    async def on_submit(self, interaction: discord.Interaction):
        if not self.state.lobby_open:
            return await interaction.response.send_message("Lobby is closed.", ephemeral=True)
        # validate int
        try:
            amt = int(str(self.bet_amount).strip().replace(",", ""))
        except ValueError:
            return await interaction.response.send_message("Enter a valid whole number.", ephemeral=True)
        if amt < self.state.min_bet:
            return await interaction.response.send_message(f"Minimum bet is {self.state.min_bet:,}.", ephemeral=True)
        if self.state.max_bet and amt > self.state.max_bet:
            return await interaction.response.send_message(f"Maximum bet is {self.state.max_bet:,}.", ephemeral=True)
        if len(self.state.racers) >= self.state.max_players and interaction.user.id not in self.state.racers:
            return await interaction.response.send_message("This lobby is full.", ephemeral=True)

        # debit immediately into the pot using your economy DB
        bal = await self.cog._get_balance(interaction.user.id)
        if amt > bal:
            return await interaction.response.send_message(f"❌ You only have **{bal}** credits.", ephemeral=True)
        await self.cog._apply_credit(interaction.user.id, -amt)

        # add/replace bet
        if interaction.user.id in self.state.racers:
            prev = self.state.racers[interaction.user.id].bet
            # refund previous bet, then take new (net delta)
            delta = amt - prev
            if delta != 0:
                await self.cog._apply_credit(interaction.user.id, -delta if delta > 0 else +(-delta))
        emoji = EMOJIS[len(self.state.racers) % len(EMOJIS)] if interaction.user.id not in self.state.racers else self.state.racers[interaction.user.id].emoji
        self.state.racers[interaction.user.id] = HRRacer(
            user_id=interaction.user.id,
            display=interaction.user.display_name,
            emoji=emoji,
            bet=amt,
        )
        self.state.pot = sum(r.bet for r in self.state.racers.values())
        await interaction.response.send_message(f"You're in for **{amt:,}**. Good luck, {interaction.user.mention}! {emoji}", ephemeral=True)

        # refresh lobby
        try:
            channel = interaction.client.get_channel(self.state.channel_id)
            if channel and self.state.message_id:
                msg = await channel.fetch_message(self.state.message_id)
                view = self.cog._hr_make_lobby_view(self.state)
                await msg.edit(embed=self.cog._hr_render_lobby(self.state), view=view)
        except Exception:
            pass

class HRBoost(discord.ui.Button):
    def __init__(self, cog: "GamesCog", state: HRState):
        super().__init__(label="Boost!", style=discord.ButtonStyle.primary, emoji="💨")
        self.cog = cog
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        if not self.state.running or self.state.finished:
            return await interaction.response.send_message("Race is not running.", ephemeral=True)
        r = self.state.racers.get(interaction.user.id)
        if not r:
            return await interaction.response.send_message("You're not in this race.", ephemeral=True)
        if r.boosted:
            return await interaction.response.send_message("You've already used your boost.", ephemeral=True)
        r.boosted = True
        r.pos = min(TRACK_LENGTH, r.pos + random.randint(1, 2))
        await interaction.response.send_message("You hit the turbo! 💥", ephemeral=True)

class HRLobbyView(discord.ui.View):
    def __init__(self, cog: "GamesCog", state: HRState):
        super().__init__(timeout=None)
        self.cog = cog
        self.state = state

        self.join_btn = discord.ui.Button(label="Join", style=discord.ButtonStyle.success, emoji="➕")
        self.leave_btn = discord.ui.Button(label="Leave", style=discord.ButtonStyle.secondary, emoji="🚫")
        self.start_btn = discord.ui.Button(label="Start", style=discord.ButtonStyle.primary, emoji="🏇")
        self.cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, emoji="🗑️")

        self.join_btn.callback = self.on_join
        self.leave_btn.callback = self.on_leave
        self.start_btn.callback = self.on_start
        self.cancel_btn.callback = self.on_cancel

        self.add_item(self.join_btn)
        self.add_item(self.leave_btn)
        self.add_item(self.start_btn)
        self.add_item(self.cancel_btn)

    async def on_join(self, interaction: discord.Interaction):
        if not self.state.lobby_open:
            return await interaction.response.send_message("Lobby is closed.", ephemeral=True)
        if len(self.state.racers) >= self.state.max_players and interaction.user.id not in self.state.racers:
            return await interaction.response.send_message("This lobby is full.", ephemeral=True)
        await interaction.response.send_modal(HRBetModal(self.cog, self.state))

    async def on_leave(self, interaction: discord.Interaction):
        if not self.state.lobby_open:
            return await interaction.response.send_message("Lobby is closed.", ephemeral=True)
        r = self.state.racers.pop(interaction.user.id, None)
        if not r:
            return await interaction.response.send_message("You aren't in this lobby.", ephemeral=True)
        # refund
        await self.cog._apply_credit(interaction.user.id, r.bet)
        self.state.pot = sum(x.bet for x in self.state.racers.values())
        await interaction.response.send_message("You left the lobby. Bet refunded.", ephemeral=True)
        # refresh
        try:
            channel = interaction.client.get_channel(self.state.channel_id)
            if channel and self.state.message_id:
                msg = await channel.fetch_message(self.state.message_id)
                await msg.edit(embed=self.cog._hr_render_lobby(self.state), view=self)
        except Exception:
            pass

    async def on_start(self, interaction: discord.Interaction):
        if interaction.user.id != self.state.host_id:
            return await interaction.response.send_message("Only the host can start this race.", ephemeral=True)
        if self.state.running:
            return await interaction.response.send_message("Already started.", ephemeral=True)
        if len(self.state.racers) < 2:
            return await interaction.response.send_message("Need at least 2 racers to start. Wait for others or let the lobby timer expire to choose solo.", ephemeral=True)
        self.state.lobby_open = False
        self.state.running = True

        run_view = self.cog._hr_make_race_view(self.state)
        try:
            channel = interaction.client.get_channel(self.state.channel_id)
            if channel and self.state.message_id:
                msg = await channel.fetch_message(self.state.message_id)
                await msg.edit(embed=self.cog._hr_render_race(self.state, prestart=True), view=run_view)
        except Exception:
            pass
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self.cog._hr_run(self.state)

    async def on_cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.state.host_id:
            return await interaction.response.send_message("Only the host can cancel.", ephemeral=True)
        if self.state.running:
            return await interaction.response.send_message("Too late to cancel; it's already started.", ephemeral=True)
        # refund everyone
        for r in list(self.state.racers.values()):
            await self.cog._apply_credit(r.user_id, r.bet)
        self.state.lobby_open = False
        try:
            channel = interaction.client.get_channel(self.state.channel_id)
            if channel and self.state.message_id:
                msg = await channel.fetch_message(self.state.message_id)
                emb = discord.Embed(title="🏇 Horse Race — Cancelled", description="The host cancelled the lobby. All bets refunded.", color=discord.Color.red())
                await msg.edit(embed=emb, view=None)
        except Exception:
            pass
        await interaction.response.send_message("Cancelled.", ephemeral=True)

class HRSoloView(discord.ui.View):
    def __init__(self, cog: "GamesCog", state: HRState, solo_user_id: int):
        super().__init__(timeout=30)
        self.cog = cog
        self.state = state
        self.solo_user_id = solo_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.solo_user_id:
            await interaction.response.send_message("Only the remaining player can decide.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Race Alone", style=discord.ButtonStyle.success, emoji="🏇")
    async def race_alone(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state.lobby_open = False
        self.state.running = True
        # start the race
        try:
            channel = interaction.client.get_channel(self.state.channel_id)
            if channel and self.state.message_id:
                msg = await channel.fetch_message(self.state.message_id)
                await msg.edit(embed=self.cog._hr_render_race(self.state, prestart=True), view=self.cog._hr_make_race_view(self.state))
        except Exception:
            pass
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self.cog._hr_run(self.state, solo_profit=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        # refund remaining player
        for r in list(self.state.racers.values()):
            await self.cog._apply_credit(r.user_id, r.bet)
        self.state.lobby_open = False
        try:
            channel = interaction.client.get_channel(self.state.channel_id)
            if channel and self.state.message_id:
                msg = await channel.fetch_message(self.state.message_id)
                emb = discord.Embed(title="🏇 Horse Race — Cancelled", description="Lobby ended. Bet refunded.", color=discord.Color.orange())
                await msg.edit(embed=emb, view=None)
        except Exception:
            pass
        await interaction.response.send_message("Cancelled.", ephemeral=True)

class HRRaceView(discord.ui.View):
    def __init__(self, cog: "GamesCog", state: HRState):
        super().__init__(timeout=None)
        self.add_item(HRBoost(cog, state))

# ----- GamesCog mix-in methods for horse race -----
def _hr_render_lobby(self: "GamesCog", state: HRState, remaining: int | None = None) -> discord.Embed:
    title = "🏇 Horse Race — Lobby"
    desc = (
        f"**Host:** <@{state.host_id}>\n"
        f"**Min bet:** {state.min_bet:,}  •  **Max bet:** {'∞' if state.max_bet == 0 else f'{state.max_bet:,}'}\n"
        f"**Max players:** {state.max_players}  •  **Pot:** {state.pot:,}\n"
        f"{'**Time left:** ' + str(remaining) + 's' if remaining is not None else ''}"
    )
    emb = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
    if state.racers:
        lines = [f"`{i:>2}.` {r.emoji} **{r.display}** — bet **{r.bet:,}**" for i, r in enumerate(state.racers.values(), start=1)]
        emb.add_field(name="Players", value="\n".join(lines), inline=False)
    else:
        emb.add_field(name="Players", value="*No one yet. Click **Join** to enter and place your bet!*", inline=False)
    emb.set_footer(text="Use the Join button to enter. Start when you're ready!")
    return emb

def _hr_make_lobby_view(self: "GamesCog", state: HRState) -> discord.ui.View:
    v = HRLobbyView(self, state)
    if not state.lobby_open:
        for item in v.children:
            try:
                item.disabled = True
            except Exception:
                pass
    return v

def _hr_make_race_view(self: "GamesCog", state: HRState) -> discord.ui.View:
    return HRRaceView(self, state)

def _hr_render_race(self: "GamesCog", state: HRState, prestart: bool = False) -> discord.Embed:
    title = "🏇 Horse Race — GO!" if not prestart else "🏇 Horse Race — Ready..."
    emb = discord.Embed(title=title, color=discord.Color.green())
    emb.description = f"**Pot:** {state.pot:,}   •   **Finish:** {FINISH_FLAG}\n"
    lines = []
    for r in state.racers.values():
        bar = _hr_progress_bar(r.pos, TRACK_LENGTH)
        lines.append(f"{r.emoji}  **{r.display}**\n`{bar}` {FINISH_FLAG}\n")
    if lines:
        emb.add_field(name="Track", value="\n".join(lines), inline=False)
    standings = sorted(state.racers.values(), key=lambda x: (x.pos, x.bet), reverse=True)
    board = []
    for idx, r in enumerate(standings[:4], start=1):
        medal = MEDALS[idx - 1] if idx <= len(MEDALS) else f"{idx}."
        board.append(f"{medal} {r.emoji} **{r.display}** — {r.pos}/{TRACK_LENGTH}")
    if board:
        emb.add_field(name="Standings", value="\n".join(board), inline=False)
    emb.set_footer(text="Hit Boost once per race for a tiny push • Good luck!")
    return emb

def _hr_render_finish(self: "GamesCog", state: HRState) -> discord.Embed:
    winners = [state.racers[uid] for uid in state.winners]
    if len(winners) == 1:
        win_text = f"{winners[0].emoji} **{winners[0].display}** wins!"
    else:
        win_text = ", ".join(f"{r.emoji} **{r.display}**" for r in winners) + " **tie for the win!**"
    payout_each = state.pot // max(1, len(winners)) if winners else 0
    emb = discord.Embed(title="🏁 Race Finished!", color=discord.Color.gold())
    emb.description = f"{win_text}\n**Pot:** {state.pot:,} • **Payout:** {payout_each:,} each"
    track_lines = []
    for r in state.racers.values():
        bar = _hr_progress_bar(r.pos, TRACK_LENGTH)
        mark = "✅" if r.user_id in state.winners else ""
        track_lines.append(f"{r.emoji} **{r.display}** {mark}\n`{bar}` {FINISH_FLAG}\n")
    emb.add_field(name="Final Track", value="\n".join(track_lines), inline=False)
    return emb

async def _hr_run(self: "GamesCog", state: HRState, *, solo_profit: bool = False):
    channel = self.bot.get_channel(state.channel_id)
    if not channel or not state.message_id:
        return
    msg = await channel.fetch_message(state.message_id)

    # 3..2..1..
    try:
        await msg.edit(embed=self._hr_render_race(state, prestart=True), view=self._hr_make_race_view(state))
        await asyncio.sleep(1.2)
        for count in ("3️⃣", "2️⃣", "1️⃣", "🏁"):
            em = self._hr_render_race(state, prestart=False)
            em.set_footer(text=f"Start in {count} • Hit Boost once per race for a tiny push")
            await msg.edit(embed=em, view=self._hr_make_race_view(state))
            await asyncio.sleep(0.9)
    except Exception:
        pass

    state.running = True
    order = list(state.racers.keys())
    while not state.finished:
        for uid in order:
            r = state.racers.get(uid)
            if not r:
                continue
            base = random.randint(0, 3)
            bonus = 1 if r.boosted and random.random() < 0.60 else 0
            r.pos = min(TRACK_LENGTH, r.pos + base + bonus)
        leaders = [r for r in state.racers.values() if r.pos >= TRACK_LENGTH]
        if leaders:
            maxpos = max(r.pos for r in leaders)
            winners = [r for r in leaders if r.pos == maxpos]
            state.finished = True
            state.winners = [r.user_id for r in winners]
        try:
            await msg.edit(embed=self._hr_render_race(state), view=self._hr_make_race_view(state) if not state.finished else None)
        except Exception:
            pass
        if state.finished:
            break
        await asyncio.sleep(TICK_SECONDS)

    # Payouts
    if state.winners:
        if len(state.racers) == 1 and solo_profit:
            # Solo run: the lone player already paid their bet into the pot; pay 2x bet (net +bet = profit).
            lone = next(iter(state.racers.values()))
            await self._apply_credit(lone.user_id, lone.bet * 2)
            state.pot = lone.bet * 2  # display in results
        else:
            payout_each = state.pot // len(state.winners)
            for uid in state.winners:
                await self._apply_credit(uid, payout_each)

    try:
        await msg.edit(embed=self._hr_render_finish(state), view=None)
    except Exception:
        pass

# Bind helper methods to GamesCog at runtime
GamesCog._hr_render_lobby = _hr_render_lobby
GamesCog._hr_make_lobby_view = _hr_make_lobby_view
GamesCog._hr_make_race_view = _hr_make_race_view
GamesCog._hr_render_race = _hr_render_race
GamesCog._hr_render_finish = _hr_render_finish
GamesCog._hr_run = _hr_run

# Command to start a horse race

async def _hr_horserace_impl(self: GamesCog, inter: discord.Interaction, min_bet: int = 50, max_bet: int = 0, max_players: int = 6, join_seconds: int = 45):
    # per-channel race guard stored on cog (create if missing)
    if not hasattr(self, "_hr_active"):
        self._hr_active = {}
    if inter.channel_id in self._hr_active:
        return await inter.response.send_message("There is already an active race in this channel.", ephemeral=True)

    max_players = max(2, min(12, int(max_players or 6)))
    min_bet = max(1, int(min_bet or 50))
    max_bet = max(0, int(max_bet or 0))
    join_seconds = max(10, min(180, int(join_seconds or 45)))

    state = HRState(
        guild_id=inter.guild.id,
        channel_id=inter.channel_id,
        host_id=inter.user.id,
        min_bet=min_bet,
        max_bet=max_bet,
        max_players=max_players,
        join_seconds=join_seconds,
    )
    self._hr_active[inter.channel_id] = state

    view = self._hr_make_lobby_view(state)
    emb = self._hr_render_lobby(state)
    await inter.response.send_message(embed=emb, view=view)
    msg = await inter.original_response()
    state.message_id = msg.id

    # countdown
    try:
        for remaining in range(join_seconds, 0, -1):
            if not state.lobby_open:
                break
            if remaining % JOIN_COUNTDOWN_EDIT_EVERY == 0 or remaining in (join_seconds, 5, 4, 3, 2, 1):
                await msg.edit(embed=self._hr_render_lobby(state, remaining), view=view)
            await asyncio.sleep(1)

        if state.lobby_open:
            state.lobby_open = False
            if len(state.racers) >= 2:
                state.running = True
                await msg.edit(embed=self._hr_render_race(state, prestart=True), view=self._hr_make_race_view(state))
                await self._hr_run(state)
            elif len(state.racers) == 1:
                # offer solo choice
                only = next(iter(state.racers.values()))
                state.solo_allowed = True
                offer = discord.Embed(
                    title="🏇 Horse Race — Not enough players",
                    description=f"Only **{only.display}** joined.\nYou can **Race Alone** for a chance at a **solo profit (+bet)**, or Cancel to refund.",
                    color=discord.Color.orange(),
                )
                await msg.edit(embed=offer, view=HRSoloView(self, state, only.user_id))
                # the solo view will start/cancel; we don't clean up here
                return
            else:
                # no players
                em = discord.Embed(
                    title="🏇 Horse Race — No players",
                    description="No one joined. Lobby closed.",
                    color=discord.Color.red(),
                )
                await msg.edit(embed=em, view=None)
                self._hr_active.pop(inter.channel_id, None)
                return
    finally:
        # Cleanup after finishing (if race finished via _hr_run, it doesn't remove this key)
        # We ensure cleanup by checking finished flag; if solo view handles, it will be cleaned later.
        async def _delayed_cleanup():
            await asyncio.sleep(2)
            self._hr_active.pop(inter.channel_id, None)
        asyncio.create_task(_delayed_cleanup())




# Properly register horserace as a Cog method to avoid Interaction being treated as an option
async def horserace(self, inter: discord.Interaction, min_bet: int = 50, max_bet: int = 0, max_players: int = 6, join_seconds: int = 45):
    return await _hr_horserace_impl(self, inter, min_bet, max_bet, max_players, join_seconds)

GamesCog.horserace = app_commands.command(
    name="horserace",
    description="Create a multiplayer horse race with betting and animations."
)(horserace)

async def setup(bot: commands.Bot):
    await bot.add_cog(GamesCog(bot))