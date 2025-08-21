
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional, List, Deque
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YoutubeDLError

log = logging.getLogger("utilabot.audio_slash")

LANGUAGE = "en"
YOUTUBE_LINK_PATTERN = re.compile(r"(https?://)?(www\.)?(youtube.com/watch\?v=|youtu.be/)([\w\-]+)")
MAX_OPTIONS = 15            # tighter keeps autocomplete snappy
MAX_OPTION_SIZE = 100
MAX_VIDEO_LENGTH = 60 * 60 * 4  # 4h cap

YTDL_SEARCH_OPTS = {
    "quiet": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "noplaylist": True,
    "default_search": "ytsearch",
    "socket_timeout": 2.0,  # prevent long hangs => Discord 10062
    "extractor_args": {"youtube": {"lang": [LANGUAGE]}},
}

YTDL_STREAM_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "socket_timeout": 5.0,
    "extractor_args": {"youtube": {"lang": [LANGUAGE]}},
    "geo_bypass": True,
}

FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

@dataclass
class Track:
    title: str
    url: str
    duration: Optional[int] = None
    webpage_url: Optional[str] = None
    requester_id: Optional[int] = None

def _format_time(seconds: Optional[int]) -> str:
    if not seconds:
        return "LIVE"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

def _format_choice_title(entry: dict) -> str:
    title = entry.get("title", "Unknown")
    if entry.get("duration"):
        name = f"({_format_time(entry['duration'])}) {title}"
    else:
        name = f"(🔴LIVE) {title}"
    author = f" — {entry.get('channel', entry.get('uploader', ''))}"
    if len(author) > MAX_OPTION_SIZE // 2:
        author = author[: MAX_OPTION_SIZE // 2 - 3] + "..."
    if len(name) + len(author) > MAX_OPTION_SIZE:
        return name[: MAX_OPTION_SIZE - len(author) - 3] + "..." + author
    return name + author

class GuildAudioState:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: Deque[Track] = deque()
        self.now: Optional[Track] = None
        self.next_event = asyncio.Event()
        self.volume: float = 0.5
        self.shuffle: bool = False
        self.repeat: bool = False

    def clear(self):
        self.queue.clear()
        self.now = None
        self.next_event.clear()
        self.repeat = False
        self.shuffle = False

class AudioSlash(commands.Cog):
    """Minimal audio slash cog (discord.py) with YouTube search and a basic queue.

    Requires: ffmpeg in PATH.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ytdl_search = YoutubeDL(YTDL_SEARCH_OPTS)
        self.ytdl_stream = YoutubeDL(YTDL_STREAM_OPTS)
        self.states: dict[int, GuildAudioState] = {}

    def get_state(self, guild_id: int) -> GuildAudioState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildAudioState(guild_id)
        return self.states[guild_id]

    async def cog_unload(self):
        # Stop all players
        for gid, state in list(self.states.items()):
            vc = self._get_vc(gid)
            if vc and vc.is_connected():
                await vc.disconnect(force=True)
        self.states.clear()

    # --------------------- helpers ---------------------

    def _get_vc(self, guild_id: int) -> Optional[discord.VoiceClient]:
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return None
        return guild.voice_client

    async def _ensure_voice(self, inter: discord.Interaction) -> discord.VoiceClient:
        assert inter.guild and inter.user and isinstance(inter.user, discord.Member)
        voice = inter.user.voice
        if not voice or not voice.channel:
            raise app_commands.AppCommandError("You're not connected to a voice channel.")
        vc = inter.guild.voice_client
        if vc and vc.channel.id != voice.channel.id:
            await vc.move_to(voice.channel)
        elif not vc:
            vc = await voice.channel.connect(self_deaf=True)
        return vc

    async def _search_youtube(self, query: str) -> Optional[dict]:
        def _extract():
            if YOUTUBE_LINK_PATTERN.match(query):
                return self.ytdl_stream.extract_info(query, download=False)
            return self.ytdl_search.extract_info(query, download=False, ie_key=None)
        return await asyncio.to_thread(_extract)

    async def _extract_stream(self, url: str) -> dict:
        return await asyncio.to_thread(self.ytdl_stream.extract_info, url, False)

    def _build_source(self, stream_url: str, *, volume: float) -> discord.PCMVolumeTransformer:
        audio = discord.FFmpegPCMAudio(
            stream_url,
            before_options=FFMPEG_BEFORE_OPTS,
            options=FFMPEG_OPTIONS,
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)

    async def _start_player_if_needed(self, inter: discord.Interaction):
        guild_id = inter.guild_id
        state = self.get_state(guild_id)
        vc = self._get_vc(guild_id)
        if not vc:
            return

        async def _play_loop():
            while vc and vc.is_connected():
                try:
                    state.next_event.clear()
                    # If not repeating, pull next
                    if not state.repeat:
                        if state.queue:
                            state.now = state.queue.popleft()
                        else:
                            state.now = None
                            # idle: wait or disconnect after 3 minutes
                            try:
                                await asyncio.wait_for(state.next_event.wait(), timeout=180)
                                continue
                            except asyncio.TimeoutError:
                                await vc.disconnect()
                                break
                    if not state.now:
                        continue

                    info = await self._extract_stream(state.now.url)
                    stream_url = info.get("url") or info.get("webpage_url")
                    if not stream_url:
                        log.warning("No stream url for %s", state.now)
                        continue
                    src = self._build_source(stream_url, volume=state.volume)

                    done_evt = asyncio.Event()

                    def after_play(err: Optional[Exception]):
                        if err:
                            log.exception("Player error: %s", err)
                        self.bot.loop.call_soon_threadsafe(done_evt.set)

                    vc.play(src, after=after_play)
                    await done_evt.wait()
                    # if repeat, keep same track in state.now and loop
                except Exception as e:
                    log.exception("Play loop error: %s", e)
                    await asyncio.sleep(2)

        # Start background player if not already
        if not getattr(vc, "_audio_player_task", None) or vc._audio_player_task.done():
            vc._audio_player_task = self.bot.loop.create_task(_play_loop())

    # --------------------- slash commands ---------------------

    when_choices = [
        app_commands.Choice(name="Add to end of the queue", value="end"),
        app_commands.Choice(name="Play after current song", value="next"),
        app_commands.Choice(name="Start playing immediately", value="now"),
    ]

    @app_commands.command(name="play", description="Play a YouTube track or search query.")
    @app_commands.describe(search="YouTube link or search terms", when="Where to place the track in the queue")
    @app_commands.choices(when=when_choices)
    @app_commands.guild_only()
    async def play(self, inter: discord.Interaction, search: str, when: Optional[str] = "end"):
        await inter.response.defer(thinking=True, ephemeral=False)
        vc = await self._ensure_voice(inter)
        state = self.get_state(inter.guild_id)

        try:
            info = await self._search_youtube(search if YOUTUBE_LINK_PATTERN.match(search) else f"ytsearch1:{search}")
            if not info:
                return await inter.followup.send("No results.")
            if info.get("entries"):
                info = info["entries"][0]
            if info.get("duration") and info["duration"] > MAX_VIDEO_LENGTH:
                return await inter.followup.send("⛔ Video too long.")
            title = info.get("title", "Unknown")
            webpage_url = info.get("webpage_url") or info.get("url") or search
            track = Track(title=title, url=webpage_url, duration=info.get("duration"), webpage_url=webpage_url, requester_id=inter.user.id)

            # Queue placement
            if when == "now":
                if state.now:
                    state.queue.appendleft(state.now)
                state.now = track
                state.next_event.set()
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                await inter.followup.send(f"▶️ Now playing **{track.title}**")
            elif when == "next":
                state.queue.appendleft(track)
                state.next_event.set()
                await inter.followup.send(f"⏭️ Queued next: **{track.title}**")
            else:
                state.queue.append(track)
                state.next_event.set()
                await inter.followup.send(f"➕ Added to queue: **{track.title}**")

            await self._start_player_if_needed(inter)
        except YoutubeDLError:
            log.exception("YouTube error")
            await inter.followup.send("Failed to fetch that track.")

    # ---- Autocomplete MUST be defined after 'play' so @play.autocomplete sees the symbol ----
    @play.autocomplete("search")
    async def youtube_autocomplete(self, inter: discord.Interaction, current: str):
        current = (current or "").strip()
        if len(current) < 3:
            return []

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    self.ytdl_search.extract_info,
                    f"ytsearch{MAX_OPTIONS}:{current}",
                    False
                ),
                timeout=2.3,
            )
            if not isinstance(results, dict):
                return []

            entries = (results.get("entries") or [])[:MAX_OPTIONS]
            choices = []
            for e in entries:
                if not e:
                    continue
                name = _format_choice_title(e)[:100]
                value = (e.get("webpage_url") or e.get("url") or "").strip()
                if not value:
                    continue
                choices.append(app_commands.Choice(name=name, value=value[:100]))
            return choices
        except asyncio.TimeoutError:
            return []
        except Exception:
            log.exception("Autocomplete error")
            return [app_commands.Choice(name="Autocomplete error. Keep typing…", value=current[:100])]

    @app_commands.command(name="pause", description="Pause or resume playback.")
    @app_commands.guild_only()
    async def pause(self, inter: discord.Interaction):
        vc = await self._ensure_voice(inter)
        if vc.is_paused():
            vc.resume()
            await inter.response.send_message("▶️ Resumed")
        else:
            if vc.is_playing():
                vc.pause()
                await inter.response.send_message("⏸️ Paused")
            else:
                await inter.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue.")
    @app_commands.guild_only()
    async def stop(self, inter: discord.Interaction):
        vc = await self._ensure_voice(inter)
        state = self.get_state(inter.guild_id)
        state.clear()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await inter.response.send_message("⏹️ Stopped and cleared the queue.")

    @app_commands.command(name="skip", description="Skip tracks in the queue.")
    @app_commands.describe(position="Skip N tracks (default: 1)")
    @app_commands.guild_only()
    async def skip(self, inter: discord.Interaction, position: Optional[int] = 1):
        position = max(1, int(position or 1))
        vc = await self._ensure_voice(inter)
        state = self.get_state(inter.guild_id)
        skipped: List[str] = []
        if position > 1:
            for _ in range(min(position - 1, len(state.queue))):
                t = state.queue.popleft()
                skipped.append(t.title)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await inter.response.send_message(f"⏭️ Skipped {position} track(s).{' Dropped: ' + ', '.join(skipped) if skipped else ''}")

    @app_commands.command(name="queue", description="Show what's playing and queued.")
    @app_commands.guild_only()
    async def queue(self, inter: discord.Interaction):
        state = self.get_state(inter.guild_id)
        embed = discord.Embed(title="Music Queue")
        if state.now:
            embed.add_field(name="Now Playing", value=f"[{state.now.title}]({state.now.webpage_url}) — {_format_time(state.now.duration)}", inline=False)
        else:
            embed.add_field(name="Now Playing", value="Nothing", inline=False)
        if state.queue:
            desc = []
            for i, t in enumerate(list(state.queue)[:10], start=1):
                desc.append(f"**{i}.** [{t.title}]({t.webpage_url}) — {_format_time(t.duration)}")
            more = len(state.queue) - 10
            if more > 0:
                desc.append(f"...and **{more}** more")
            embed.add_field(name="Up Next", value="\n".join(desc), inline=False)
        else:
            embed.add_field(name="Up Next", value="(empty)", inline=False)
        embed.add_field(name="Settings", value=f"Shuffle: {'On' if state.shuffle else 'Off'} • Repeat: {'On' if state.repeat else 'Off'} • Volume: {int(state.volume*100)}%", inline=False)
        await inter.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Set volume (1-150%).")
    @app_commands.describe(volume="New volume between 1 and 150")
    @app_commands.guild_only()
    async def volume(self, inter: discord.Interaction, volume: app_commands.Range[int, 1, 150]):
        vc = await self._ensure_voice(inter)
        state = self.get_state(inter.guild_id)
        state.volume = float(volume) / 100.0
        if vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = state.volume
        await inter.response.send_message(f"🔊 Volume set to **{volume}%**")

    @app_commands.command(name="shuffle", description="Toggle shuffle mode.")
    @app_commands.guild_only()
    async def shuffle(self, inter: discord.Interaction):
        state = self.get_state(inter.guild_id)
        state.shuffle = not state.shuffle
        if state.shuffle and len(state.queue) > 1:
            import random
            q = list(state.queue)
            random.shuffle(q)
            state.queue = deque(q)
        await inter.response.send_message(f"🔀 Shuffle {'enabled' if state.shuffle else 'disabled'}.")

    @app_commands.command(name="repeat", description="Toggle repeat (repeat current track).")
    @app_commands.guild_only()
    async def repeat(self, inter: discord.Interaction):
        state = self.get_state(inter.guild_id)
        state.repeat = not state.repeat
        await inter.response.send_message(f"🔁 Repeat {'enabled' if state.repeat else 'disabled'}.")

async def setup(bot: commands.Bot):
    await bot.add_cog(AudioSlash(bot))
