import random
import asyncio
from typing import List, Optional, Deque
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

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
        r = c[:-1] if c[:-1] in RANKS else c[0]
        v = RANK_VALUES.get(r, 0)
        total += v
        if r == "A":
            aces += 1
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

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
    def __init__(self, *, player: discord.Member, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.player = player
        self.deck: List[str] = new_deck()
        self.player_hand: List[str] = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand: List[str] = [self.deck.pop(), self.deck.pop()]
        self.message: Optional[discord.Message] = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("Only the game owner can press these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.success, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player_hand.append(self.deck.pop())
        player_total = hand_value(self.player_hand)
        if player_total >= 21:
            await self._finish_round(interaction)
        else:
            await self._update_embed(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.primary, emoji="🛑")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish_round(interaction)

    @discord.ui.button(label="Surrender", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finished = True
        self.disable_all_items()
        dealer_total = hand_value(self.dealer_hand)
        embed = self._build_embed(reveal_dealer=True)
        embed.add_field(name="Result", value="🏳️ **You surrendered. Dealer wins.**", inline=False)
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

        if not self.finished:
            if p_total == 21 and len(self.player_hand) == 2:
                embed.set_footer(text="Blackjack! Press Stand to reveal.")
            else:
                embed.set_footer(text="Click Hit or Stand. Auto-stand in 60s of inactivity.")
        else:
            embed.set_footer(text="Game over. Start a new /blackjack to play again.")
        return embed

    async def _update_embed(self, interaction: discord.Interaction):
        embed = self._build_embed(reveal_dealer=False)
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.message.edit(embed=embed, view=self)

    async def _finish_round(self, interaction: Optional[discord.Interaction], *, from_timeout: bool = False):
        self.finished = True
        self.disable_all_items()
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        p_total = hand_value(self.player_hand)
        d_total = hand_value(self.dealer_hand)
        result = outcome_text(p_total, d_total)

        embed = self._build_embed(reveal_dealer=True)
        if from_timeout:
            embed.add_field(name="Result", value=f"⏱️ **Timeout** — {result}", inline=False)
        else:
            embed.add_field(name="Result", value=result, inline=False)

        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.message.edit(embed=embed, view=self)

# ---------------- Games Cog ----------------

class GamesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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

    @app_commands.command(name="blackjack", description="Play an interactive blackjack game vs. the dealer.")
    async def blackjack(self, inter: discord.Interaction):
        view = BlackjackView(player=inter.user, timeout=60)
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