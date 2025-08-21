import random
import asyncio
from typing import List, Optional, Callable
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

# --- economy integration ---
# We only use ensure_user; credits are adjusted on Balance.credits in your DB
from utils.common import ensure_user  # provided by your project


# ---------------- Card helpers ----------------

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
RANK_VALUES = {**{str(i): i for i in range(2, 11)}, "J": 10, "Q": 10, "K": 10, "A": 11}

def new_deck() -> List[str]:
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def hand_value(cards: List[str]) -> int:
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

def is_blackjack(cards: List[str]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21

def render_cards(cards: List[str], hide_first: bool = False) -> str:
    if hide_first and cards:
        return "🂠 " + " ".join(f"`{c}`" for c in cards[1:])
    return " ".join(f"`{c}`" for c in cards)

def outcome_text(player_total: int, dealer_total: int) -> str:
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
    Interactive blackjack game that settles bets via your existing economy (DB).
    We call the provided apply_credit(user_id, delta) coroutine to adjust credits.
    """
    def __init__(
        self,
        *,
        player: discord.Member,
        bet: int,
        apply_credit: Callable[[int, int], "asyncio.Future"],  # (user_id, delta)
        timeout: int = 60,
    ):
        super().__init__(timeout=timeout)
        self.player = player
        self.bet = max(0, int(bet))
        self.apply_credit = apply_credit

        self.deck: List[str] = new_deck()
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
        if hand_value(self.player_hand) >= 21:
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
        p_total = hand_value(self.player_hand)
        d_total = hand_value(self.dealer_hand) if reveal_dealer else None

        title = "🂡 Blackjack"
        if self.finished:
            title += " — Final"
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.set_author(
            name=self.player.display_name,
            icon_url=self.player.display_avatar.url if hasattr(self.player.display_avatar, "url") else discord.Embed.Empty
        )

        dealer_hand_str = render_cards(self.dealer_hand, hide_first=not reveal_dealer)
        dealer_title = f"Dealer ({d_total if reveal_dealer else '?'})"
        embed.add_field(name=dealer_title, value=dealer_hand_str, inline=False)

        player_hand_str = render_cards(self.player_hand, hide_first=False)
        embed.add_field(name=f"Your Hand ({p_total})", value=player_hand_str, inline=False)

        if self.bet:
            embed.add_field(name="Bet", value=f"**{self.bet}** credits", inline=True)

        if not self.finished:
            if is_blackjack(self.player_hand):
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
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        p_total = hand_value(self.player_hand)
        d_total = hand_value(self.dealer_hand)

        # Settle bet using your economy
        result_line = ""
        if self.bet > 0 and not self.paid:
            if p_total > 21:
                await self.apply_credit(self.player.id, -self.bet)
                result_line = f"💥 Bust. Lost **{self.bet}** credits."
            elif d_total > 21 or p_total > d_total:
                win = int(self.bet * 1.5) if is_blackjack(self.player_hand) else self.bet
                await self.apply_credit(self.player.id, win)
                result_line = f"🏆 You win **{win}** credits."
            elif p_total == d_total:
                result_line = "🤝 Push. Bet refunded."
            else:
                await self.apply_credit(self.player.id, -self.bet)
                result_line = f"🫤 Dealer wins. Lost **{self.bet}** credits."
            self.paid = True

        embed = self._build_embed(reveal_dealer=True)
        base_outcome = outcome_text(p_total, d_total)
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


# ---------------- Games Cog ----------------

class GamesCog(commands.Cog):
    """
    Games cog that integrates blackjack with your economy DB.
    We assume `self.bot.SessionLocal` exists (SQLAlchemy sessionmaker) as in your EconomyCog.
    """

    def __init__(self, bot):
        self.bot = bot

    # economy helper: adjust credits atomically
    async def _apply_credit(self, user_id: int, delta: int) -> int:
        # run in thread to avoid blocking loop if your DB is sync
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

    @app_commands.command(name="coinflip", description="Flip a coin.")
    async def coinflip(self, inter: discord.Interaction):
        await inter.response.send_message(f"The coin landed on **{random.choice(['Heads','Tails'])}**.")

    @app_commands.command(name="highlow", description="Guess if next number (1-100) is higher or lower.")
    async def highlow(self, inter: discord.Interaction, guess: str):
        base = random.randint(1, 100)
        nxt = random.randint(1, 100)
        res = "higher" if nxt > base else ("lower" if nxt < base else "equal")
        win = (guess.lower().startswith(res[:1])) or (res == "equal" and guess.lower().startswith("e"))
        outcome = "✅ Correct!" if win else "❌ Nope."
        await inter.response.send_message(f"Base: **{base}** → Next: **{nxt}** ({res}). {outcome}")

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

    @app_commands.command(name="trivia", description="Quick trivia (True/False).")
    async def trivia(self, inter: discord.Interaction):
        q = random.choice([
            ("The capital of Australia is Sydney.", False),
            ("The Python language was named after Monty Python.", True),
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