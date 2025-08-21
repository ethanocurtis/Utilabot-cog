# cogs/pins.py
import re
import datetime as dt
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(?P<guild>\d{17,20})/(?P<channel>\d{17,20})/(?P<message>\d{17,20})"
)

MAX_NOTE_LEN = 200

def _is_admin(inter: discord.Interaction) -> bool:
    try:
        if isinstance(inter.channel, (discord.TextChannel, discord.Thread)):
            perms = inter.channel.permissions_for(inter.user)
            return bool(perms.administrator or perms.manage_guild)
    except Exception:
        pass
    return False


class Pins(commands.Cog):
    """
    Virtual pins stored in your DB (per channel). Persisted & reboot-safe.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Expecting WxStore-like adapter on bot.store with .db SQLAlchemy Engine
        self.db = bot.store.db
        self._ensure_table()

    # ---------- DB bootstrap ----------
    def _ensure_table(self):
        with self.db.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS virtual_pins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Helpful index for listing
            try:
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_virtual_pins_channel ON virtual_pins(channel_id, id)"
                )
            except Exception:
                pass

    # ---------- helpers ----------
    def _insert_pin(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        url: str,
        note: Optional[str],
    ) -> int:
        with self.db.begin() as conn:
            res = conn.exec_driver_sql(
                """
                INSERT INTO virtual_pins(guild_id, channel_id, message_id, user_id, url, note, created_at)
                VALUES (:g, :c, :m, :u, :url, :note, :ts)
                """,
                {
                    "g": guild_id,
                    "c": channel_id,
                    "m": message_id,
                    "u": user_id,
                    "url": url,
                    "note": note or None,
                    "ts": dt.datetime.utcnow().isoformat(),
                },
            )
            return res.lastrowid  # sqlite

    def _list_pins(self, channel_id: int, limit: int = 10) -> List[Tuple]:
        with self.db.connect() as conn:
            rows = conn.exec_driver_sql(
                """
                SELECT id, user_id, url, note, created_at
                FROM virtual_pins
                WHERE channel_id = :c
                ORDER BY id ASC
                LIMIT :lim
                """,
                {"c": channel_id, "lim": limit},
            ).fetchall()
        return rows

    def _remove_pin(self, pin_id: int, requester_id: int, is_admin: bool) -> bool:
        with self.db.begin() as conn:
            if is_admin:
                res = conn.exec_driver_sql(
                    "DELETE FROM virtual_pins WHERE id=:i", {"i": pin_id}
                )
            else:
                res = conn.exec_driver_sql(
                    "DELETE FROM virtual_pins WHERE id=:i AND user_id=:u",
                    {"i": pin_id, "u": requester_id},
                )
            return res.rowcount > 0  # type: ignore[attr-defined]

    # ---------- commands ----------
    group = app_commands.Group(name="pin", description="Virtual pins (saved message links)")

    @group.command(name="add", description="Save a message link as a virtual pin for this channel.")
    @app_commands.describe(
        message_link="Right-click a message → Copy Message Link",
        note="Optional label shown in /pin list (max 200 chars)",
    )
    async def pin_add(self, inter: discord.Interaction, message_link: str, note: Optional[str] = None):
        if not isinstance(inter.channel, (discord.TextChannel, discord.Thread)):
            return await inter.response.send_message("Use this in a text channel.", ephemeral=True)

        m = LINK_RE.fullmatch(message_link.strip())
        if not m:
            return await inter.response.send_message(
                "That doesn't look like a valid Discord message link.", ephemeral=True
            )

        guild_id = int(m.group("guild"))
        channel_id = int(m.group("channel"))
        message_id = int(m.group("message"))

        # sanity checks: same guild/channel
        if inter.guild is None or guild_id != inter.guild.id:
            return await inter.response.send_message(
                "Link must be from this server.", ephemeral=True
            )
        if channel_id != inter.channel.id:
            return await inter.response.send_message(
                "Link must be from this channel.", ephemeral=True
            )

        # optional: try to fetch to ensure it exists and produce a nicer preview
        fetched_msg = None
        try:
            ch = inter.channel
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                fetched_msg = await ch.fetch_message(message_id)
        except Exception:
            fetched_msg = None  # not fatal; still allow saving

        clean_note = (note or "").strip()[:MAX_NOTE_LEN] or None
        pin_id = self._insert_pin(
            inter.guild.id, inter.channel.id, message_id, inter.user.id, message_link, clean_note
        )

        # Build a confirmation embed
        embed = discord.Embed(
            title=f"📌 Saved Pin #{pin_id:04d}",
            description=f"[Jump to message]({message_link})",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Channel", value=f"<#{inter.channel.id}>", inline=True)
        embed.add_field(name="Saved by", value=f"<@{inter.user.id}>", inline=True)
        if clean_note:
            embed.add_field(name="Note", value=clean_note, inline=False)
        if fetched_msg:
            preview = (fetched_msg.content or "").strip()
            if preview:
                embed.add_field(name="Preview", value=(preview[:300] + "…") if len(preview) > 300 else preview, inline=False)

        await inter.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="list", description="Show saved pins for this channel.")
    @app_commands.describe(limit="How many to show (1-20)")
    async def pin_list(self, inter: discord.Interaction, limit: Optional[int] = 10):
        if not isinstance(inter.channel, (discord.TextChannel, discord.Thread)):
            return await inter.response.send_message("Use this in a text channel.", ephemeral=True)

        lim = max(1, min(int(limit or 10), 20))
        rows = self._list_pins(inter.channel.id, lim)
        if not rows:
            return await inter.response.send_message("No virtual pins yet in this channel.", ephemeral=True)

        embed = discord.Embed(
            title=f"📌 Virtual Pins for #{inter.channel.name}",
            description="(Oldest first)",
            color=discord.Color.blurple(),
        )
        for pid, uid, url, note, created in rows:
            label = f"#{pid:04d}  —  <@{uid}>  —  {dt.datetime.fromisoformat(created).strftime('%Y-%m-%d %H:%M UTC')}"
            body = f"[Jump to message]({url})"
            if note:
                body += f"\n**Note:** {note}"
            embed.add_field(name=label, value=body, inline=False)

        await inter.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="remove", description="Remove a saved pin by ID.")
    async def pin_remove(self, inter: discord.Interaction, pin_id: int):
        is_admin = _is_admin(inter)
        ok = self._remove_pin(pin_id, inter.user.id, is_admin)
        if ok:
            await inter.response.send_message(f"🗑️ Removed pin `#{pin_id:04d}`.", ephemeral=True)
        else:
            # clarify permission error versus not found
            if is_admin:
                await inter.response.send_message("No pin found with that ID in this channel.", ephemeral=True)
            else:
                await inter.response.send_message(
                    "No pin found with that ID that you own (or it’s in another channel).", ephemeral=True
                )


async def setup(bot: commands.Bot):
    await bot.add_cog(Pins(bot))