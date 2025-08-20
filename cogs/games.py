import random, asyncio
import discord
from discord.ext import commands
from discord import app_commands

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
        outcome = "✅ Correct!" if guess.lower().startswith(res[:1]) or (res=='equal' and guess.lower().startswith('e')) else "❌ Nope."
        await inter.response.send_message(f"Base: **{base}** → Next: **{nxt}** ({res}). {outcome}")

    @app_commands.command(name="blackjack", description="Simple blackjack vs. dealer.")
    async def blackjack(self, inter: discord.Interaction):
        deck = [v for v in list(range(2,11))+['J','Q','K','A']]*4
        random.shuffle(deck)
        def hand_val(cards):
            val, aces = 0, 0
            for c in cards:
                if isinstance(c,int): val += c
                elif c in ['J','Q','K']: val += 10
                else: val += 11; aces += 1
            while val>21 and aces>0:
                val -= 10; aces -= 1
            return val
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        msg = await inter.response.send_message(f"Your hand: {player} ({hand_val(player)}). Dealer shows: {dealer[0]}. Type **hit** or **stand** within 20s.")
        try:
            m = await inter.client.wait_for('message', timeout=20.0, check=lambda x: x.author.id==inter.user.id and x.channel.id==inter.channel.id and x.content.lower() in ('hit','stand'))
        except asyncio.TimeoutError:
            return await inter.followup.send("⏱️ Timeout. Standing.")
        if m.content.lower()=='hit':
            player.append(deck.pop())
        # dealer plays
        while hand_val(dealer)<17: dealer.append(deck.pop())
        pv, dv = hand_val(player), hand_val(dealer)
        if pv>21: outcome="You bust."
        elif dv>21 or pv>dv: outcome="You win!"
        elif pv==dv: outcome="Push."
        else: outcome="Dealer wins."
        await inter.followup.send(f"Your hand: {player} ({pv})\nDealer: {dealer} ({dv})\n**{outcome}**")

    @app_commands.command(name="trivia", description="Quick trivia (True/False).")
    async def trivia(self, inter: discord.Interaction):
        q = random.choice([
            ("The capital of Australia is Sydney.", False),
            ("The Python language was named after Monty Python.", True),
            ("Electrons are larger than atoms.", False),
        ])
        await inter.response.send_message(f"🧠 {q[0]} Answer with **true** or **false** in 15s.")
        try:
            m = await inter.client.wait_for('message', timeout=15.0, check=lambda x: x.author.id==inter.user.id and x.channel.id==inter.channel.id and x.content.lower() in ('true','false'))
        except asyncio.TimeoutError:
            return await inter.followup.send("⏱️ Time!")
        correct = "true" if q[1] else "false"
        await inter.followup.send("✅ Correct!" if m.content.lower()==correct else f"❌ Nope. Correct is **{correct}**.")

async def setup(bot: commands.Bot):
    await bot.add_cog(GamesCog(bot))
