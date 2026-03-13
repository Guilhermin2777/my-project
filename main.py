import os
import json
import time
import base64
import asyncio
import re
from collections import deque
from urllib import request, parse

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_TRACK_REGEX = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[^/]+/)?track/|spotify:track:)([A-Za-z0-9]+)"
)

_SPOTIFY_TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0,
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "default_search": "ytsearch",
    "quiet": True,
    "nocheckcertificate": True,
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class GuildPlayer:
    def __init__(self):
        self.voice_client: discord.VoiceClient | None = None
        self.queue = deque()
        self.current = None
        self.panel_message: discord.Message | None = None
        self.panel_channel_id: int | None = None

        self.track_started_at: float | None = None
        self.paused_at: float | None = None
        self.paused_total: float = 0.0

        self.refresh_lock = asyncio.Lock()


players: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]


def format_duration(seconds):
    if seconds is None:
        return "ao vivo/desconhecida"

    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02}:{sec:02}"
    return f"{minutes}:{sec:02}"


def reset_progress(player: GuildPlayer):
    player.track_started_at = None
    player.paused_at = None
    player.paused_total = 0.0


def start_progress(player: GuildPlayer):
    player.track_started_at = time.monotonic()
    player.paused_at = None
    player.paused_total = 0.0


def pause_progress(player: GuildPlayer):
    if player.paused_at is None:
        player.paused_at = time.monotonic()


def resume_progress(player: GuildPlayer):
    if player.paused_at is not None:
        player.paused_total += time.monotonic() - player.paused_at
        player.paused_at = None


def get_elapsed_seconds(player: GuildPlayer):
    if not player.current or player.track_started_at is None:
        return 0

    now = player.paused_at if player.paused_at is not None else time.monotonic()
    elapsed = int(now - player.track_started_at - player.paused_total)

    duration = player.current.get("duration")
    if duration:
        elapsed = min(max(elapsed, 0), int(duration))
    else:
        elapsed = max(elapsed, 0)

    return elapsed


def get_remaining_seconds(player: GuildPlayer):
    if not player.current:
        return 0

    duration = player.current.get("duration")
    if not duration:
        return None

    elapsed = get_elapsed_seconds(player)
    return max(0, int(duration) - elapsed)


def make_progress_bar(elapsed: int, total: int | None, size: int = 10):
    if not total or total <= 0:
        return "🔴 Ao vivo"

    ratio = max(0, min(elapsed / total, 1))
    filled = int(ratio * size)

    if filled >= size:
        filled = size

    empty = size - filled
    return "▰" * filled + "▱" * empty


def extract_spotify_track_id(query: str):
    match = SPOTIFY_TRACK_REGEX.search(query)
    if match:
        return match.group(1)
    return None


def get_spotify_access_token():
    now = time.time()

    if (
        _SPOTIFY_TOKEN_CACHE["access_token"]
        and now < _SPOTIFY_TOKEN_CACHE["expires_at"] - 30
    ):
        return _SPOTIFY_TOKEN_CACHE["access_token"]

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError("SPOTIFY_CLIENT_ID ou SPOTIFY_CLIENT_SECRET não configurados.")

    credentials = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()

    data = parse.urlencode({"grant_type": "client_credentials"}).encode()

    req = request.Request(
        "https://accounts.spotify.com/api/token",
        data=data,
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    with request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode())

    access_token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))

    _SPOTIFY_TOKEN_CACHE["access_token"] = access_token
    _SPOTIFY_TOKEN_CACHE["expires_at"] = now + expires_in

    return access_token


def get_spotify_track(track_id: str):
    token = get_spotify_access_token()

    req = request.Request(
        f"https://api.spotify.com/v1/tracks/{track_id}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )

    with request.urlopen(req, timeout=15) as response:
        data = json.loads(response.read().decode())

    artists = ", ".join(artist["name"] for artist in data.get("artists", []))
    title = data.get("name", "Sem título")
    album = data.get("album", {})
    images = album.get("images", [])
    thumbnail = images[0]["url"] if images else None

    return {
        "title": title,
        "artist": artists or "Desconhecido",
        "duration": int(data.get("duration_ms", 0) / 1000) if data.get("duration_ms") else None,
        "webpage_url": data.get("external_urls", {}).get("spotify", f"https://open.spotify.com/track/{track_id}"),
        "thumbnail": thumbnail,
        "search_query": f"{title} {artists} official audio",
        "source": "spotify",
    }


async def extract_track(query: str):
    spotify_track_id = extract_spotify_track_id(query)

    if spotify_track_id:
        loop = asyncio.get_running_loop()

        def _spotify_extract():
            spotify_data = get_spotify_track(spotify_track_id)
            data = ytdl.extract_info(spotify_data["search_query"], download=False)

            if "entries" in data:
                entries = [e for e in data["entries"] if e]
                if not entries:
                    return None
                data = entries[0]

            return {
                "title": spotify_data["title"],
                "artist": spotify_data["artist"],
                "stream_url": data["url"],
                "webpage_url": spotify_data["webpage_url"],
                "duration": spotify_data["duration"],
                "thumbnail": spotify_data["thumbnail"] or data.get("thumbnail"),
                "source": "spotify",
            }

        return await loop.run_in_executor(None, _spotify_extract)

    loop = asyncio.get_running_loop()

    def _extract():
        data = ytdl.extract_info(query, download=False)
        if "entries" in data:
            entries = [e for e in data["entries"] if e]
            if not entries:
                return None
            data = entries[0]

        return {
            "title": data.get("title", "Sem título"),
            "artist": data.get("uploader") or data.get("channel") or "Desconhecido",
            "stream_url": data["url"],
            "webpage_url": data.get("webpage_url", query),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
            "source": "youtube",
        }

    return await loop.run_in_executor(None, _extract)


async def ensure_user_in_voice(interaction: discord.Interaction):
    if not interaction.guild:
        raise RuntimeError("Use este comando dentro de um servidor.")

    if not interaction.user.voice or not interaction.user.voice.channel:
        raise RuntimeError("Entre em um canal de voz primeiro.")

    player = get_player(interaction.guild.id)
    user_channel = interaction.user.voice.channel

    if player.voice_client and player.voice_client.is_connected():
        if player.voice_client.channel != user_channel:
            await player.voice_client.move_to(user_channel)
    else:
        player.voice_client = await user_channel.connect(self_deaf=True)

    return player


async def ensure_same_voice_channel(interaction: discord.Interaction):
    if not interaction.guild:
        return "Use este botão dentro de um servidor."

    player = get_player(interaction.guild.id)

    if not player.voice_client or not player.voice_client.is_connected():
        return "O bot não está em um canal de voz."

    if not interaction.user.voice or not interaction.user.voice.channel:
        return "Entre em um canal de voz primeiro."

    if interaction.user.voice.channel != player.voice_client.channel:
        return "Entre no mesmo canal de voz do bot para usar esse botão."

    return None


def build_now_playing_embed(guild: discord.Guild):
    player = get_player(guild.id)

    if not player.current:
        embed = discord.Embed(
            title="🎶 PLAYER DE MÚSICA",
            description="Nenhuma música tocando no momento.",
            color=discord.Color.dark_grey()
        )
        if bot.user:
            embed.set_author(
                name="Jeffery Music",
                icon_url=bot.user.display_avatar.url
            )
        embed.set_footer(text="Use /play para iniciar uma música")
        return embed

    track = player.current
    requester = f"<@{track.get('requester_id')}>" if track.get("requester_id") else "Desconhecido"
    elapsed = get_elapsed_seconds(player)
    remaining = get_remaining_seconds(player)

    status = "⏹️ Parado"
    if player.voice_client:
        if player.voice_client.is_paused():
            status = "⏸️ Pausado"
        elif player.voice_client.is_playing():
            status = "▶️ Tocando"

    source_name = "Spotify" if track.get("source") == "spotify" else "YouTube"

    if track.get("source") == "spotify":
        embed_color = discord.Color.from_rgb(29, 185, 84)
    else:
        embed_color = discord.Color.from_rgb(237, 66, 69)

    embed = discord.Embed(
        title="🎵 PLAYER DE MÚSICA",
        description=(
            "🎶 **Tocando Agora**\n"
            f"[{track['title']}]({track['webpage_url']})"
        ),
        color=embed_color
    )

    if bot.user:
        embed.set_author(
            name="Jeffery Music",
            icon_url=bot.user.display_avatar.url
        )

    embed.add_field(
        name="👤 Artista",
        value=(
            f"**Artista:** {track.get('artist', 'Desconhecido')}\n"
            f"**Duração:** {format_duration(track.get('duration'))}\n"
            f"**Solicitado por:** {requester}"
        ),
        inline=True
    )

    embed.add_field(
        name="📊 Status",
        value=(
            f"**Estado:** {status}\n"
            f"**Origem:** {source_name}\n"
            f"**Na fila:** {len(player.queue)}"
        ),
        inline=True
    )

    embed.add_field(
        name="⚙️ Configurações",
        value=(
            f"🎧 Fonte: {source_name}\n"
            f"📋 Fila ativa: {'Sim' if len(player.queue) > 0 else 'Não'}"
        ),
        inline=False
    )

    embed.add_field(
        name="⏱️ Progresso",
        value=(
            f"{make_progress_bar(elapsed, track.get('duration'))}\n"
            f"**Tempo:** `{format_duration(elapsed)}` / `{format_duration(track.get('duration'))}`\n"
            f"**Restante:** `{format_duration(remaining) if remaining is not None else 'ao vivo/desconhecido'}`"
        ),
        inline=False
    )

    embed.add_field(
        name="📦 Fila",
        value=f"{len(player.queue)} música(s) na fila",
        inline=False
    )

    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])

    embed.set_footer(text="Atualização automática a cada 5s • 🗑️ apaga só o painel")
    return embed


async def delete_panel(guild_id: int):
    player = get_player(guild_id)

    if player.panel_message:
        try:
            await player.panel_message.delete()
        except Exception:
            pass
        finally:
            player.panel_message = None


async def delete_panel_only(guild: discord.Guild):
    player = get_player(guild.id)
    await delete_panel(guild.id)
    player.panel_message = None


async def disconnect_and_cleanup(guild: discord.Guild):
    player = get_player(guild.id)

    await delete_panel(guild.id)

    if player.voice_client and player.voice_client.is_connected():
        try:
            await player.voice_client.disconnect()
        except Exception:
            pass

    player.voice_client = None
    player.current = None
    player.queue.clear()
    reset_progress(player)


async def send_new_panel(guild: discord.Guild):
    player = get_player(guild.id)

    if not player.panel_channel_id:
        return

    channel = guild.get_channel(player.panel_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(player.panel_channel_id)
        except Exception:
            return

    await delete_panel(guild.id)

    try:
        msg = await channel.send(
            embed=build_now_playing_embed(guild),
            view=MusicControls(guild.id)
        )
        player.panel_message = msg
    except Exception as e:
        print(f"Erro ao enviar painel: {e}")


async def refresh_panel(guild: discord.Guild):
    player = get_player(guild.id)

    async with player.refresh_lock:
        if not player.current:
            await delete_panel(guild.id)
            return

        if not player.panel_message:
            await send_new_panel(guild)
            return

        try:
            await player.panel_message.edit(
                embed=build_now_playing_embed(guild),
                view=MusicControls(guild.id)
            )
        except discord.NotFound:
            player.panel_message = None
            await send_new_panel(guild)
        except Exception as e:
            print(f"Erro ao atualizar painel: {e}")


@tasks.loop(seconds=5)
async def panel_auto_update():
    for guild_id, player in list(players.items()):
        if not player.current or not player.panel_message:
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        try:
            await refresh_panel(guild)
        except Exception as e:
            print(f"Erro no auto update do painel ({guild_id}): {e}")


@panel_auto_update.before_loop
async def before_panel_auto_update():
    await bot.wait_until_ready()


class MusicControls(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        error = await ensure_same_voice_channel(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        player = get_player(self.guild_id)

        if player.voice_client and player.voice_client.is_playing():
            player.voice_client.pause()
            pause_progress(player)
            await interaction.response.defer()
            await refresh_panel(interaction.guild)
        else:
            await interaction.response.send_message("Não há música tocando agora.", ephemeral=True)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        error = await ensure_same_voice_channel(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        player = get_player(self.guild_id)

        if player.voice_client and player.voice_client.is_paused():
            player.voice_client.resume()
            resume_progress(player)
            await interaction.response.defer()
            await refresh_panel(interaction.guild)
        else:
            await interaction.response.send_message("Não há música pausada.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        error = await ensure_same_voice_channel(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        player = get_player(self.guild_id)

        if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
            player.voice_client.stop()
            reset_progress(player)
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Não há música para pular.", ephemeral=True)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await refresh_panel(interaction.guild)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)

        lines = []
        if player.current:
            lines.append(f"**Tocando agora:** {player.current['title']}")

        if player.queue:
            preview = list(player.queue)[:10]
            for i, track in enumerate(preview, start=1):
                lines.append(f"`{i}.` {track['title']} ({format_duration(track['duration'])})")
        else:
            lines.append("Fila vazia.")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.secondary, row=1)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(self.guild_id)

        if player.current:
            await interaction.response.send_message(
                f"🔗 Link da música atual:\n{player.current['webpage_url']}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("Nenhuma música tocando agora.", ephemeral=True)

    @discord.ui.button(emoji="👋", style=discord.ButtonStyle.secondary, row=1)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        error = await ensure_same_voice_channel(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer()
        await disconnect_and_cleanup(interaction.guild)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.secondary, row=2)
    async def delete_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        error = await ensure_same_voice_channel(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except Exception:
            await delete_panel_only(interaction.guild)
        else:
            player = get_player(self.guild_id)
            player.panel_message = None

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=2)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        error = await ensure_same_voice_channel(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        player = get_player(self.guild_id)

        if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
            player.voice_client.stop()

        reset_progress(player)
        await interaction.response.defer()
        await disconnect_and_cleanup(interaction.guild)


async def play_next(guild: discord.Guild):
    player = get_player(guild.id)

    if not player.voice_client or not player.voice_client.is_connected():
        return

    if not player.queue:
        await disconnect_and_cleanup(guild)
        return

    track = player.queue.popleft()
    player.current = track
    start_progress(player)

    source = discord.FFmpegPCMAudio(track["stream_url"], **FFMPEG_OPTIONS)

    def after_play(error):
        if error:
            print(f"Erro no player: {error}")

        reset_progress(player)
        future = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
        try:
            future.result()
        except Exception as exc:
            print(f"Erro ao tocar próxima música: {exc}")

    player.voice_client.play(source, after=after_play)
    await send_new_panel(guild)


@bot.event
async def on_ready():
    synced = await bot.tree.sync()

    if not panel_auto_update.is_running():
        panel_auto_update.start()

    print(f"Bot online como {bot.user} | comandos: {len(synced)}")


@bot.tree.command(name="play", description="Toca uma música ou adiciona na fila")
@app_commands.describe(busca="Nome da música ou link")
async def play(interaction: discord.Interaction, busca: str):
    await interaction.response.defer(thinking=True)

    try:
        player = await ensure_user_in_voice(interaction)
        player.panel_channel_id = interaction.channel_id

        track = await extract_track(busca)

        if not track:
            await interaction.followup.send("Não encontrei nada com essa busca.")
            return

        track["requester_id"] = interaction.user.id
        player.queue.append(track)

        if not player.voice_client.is_playing() and not player.voice_client.is_paused() and player.current is None:
            await play_next(interaction.guild)
            await interaction.followup.send(
                f"▶️ Tocando agora: **{track['title']}**\nDuração: `{format_duration(track['duration'])}`"
            )
        else:
            await interaction.followup.send(
                f"➕ Adicionada na fila: **{track['title']}**\nDuração: `{format_duration(track['duration'])}`"
            )
            await refresh_panel(interaction.guild)

    except Exception as e:
        await interaction.followup.send(f"Erro: {e}")


@bot.tree.command(name="pause", description="Pausa a música atual")
async def pause(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.")
        return

    player = get_player(interaction.guild.id)

    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        pause_progress(player)
        await interaction.response.send_message("⏸️ Música pausada.")
        await refresh_panel(interaction.guild)
    else:
        await interaction.response.send_message("Não há música tocando agora.")


@bot.tree.command(name="resume", description="Continua a música pausada")
async def resume(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.")
        return

    player = get_player(interaction.guild.id)

    if player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        resume_progress(player)
        await interaction.response.send_message("▶️ Música retomada.")
        await refresh_panel(interaction.guild)
    else:
        await interaction.response.send_message("Não há música pausada.")


@bot.tree.command(name="skip", description="Pula a música atual")
async def skip(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.")
        return

    player = get_player(interaction.guild.id)

    if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
        player.voice_client.stop()
        reset_progress(player)
        await interaction.response.send_message("⏭️ Música pulada.")
    else:
        await interaction.response.send_message("Não há música para pular.")


@bot.tree.command(name="stop", description="Para tudo, apaga o player e sai do canal")
async def stop(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.")
        return

    player = get_player(interaction.guild.id)

    if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
        player.voice_client.stop()

    reset_progress(player)
    await disconnect_and_cleanup(interaction.guild)
    await interaction.response.send_message("⏹️ Reprodução parada, player apagado e bot desconectado.")


@bot.tree.command(name="queue", description="Mostra a fila atual")
async def queue(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.")
        return

    player = get_player(interaction.guild.id)

    lines = []
    if player.current:
        lines.append(f"**Tocando agora:** {player.current['title']}")

    if player.queue:
        preview = list(player.queue)[:10]
        for i, track in enumerate(preview, start=1):
            lines.append(f"`{i}.` {track['title']} ({format_duration(track['duration'])})")
    else:
        lines.append("Fila vazia.")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="leave", description="Desconecta o bot do canal e apaga o player")
async def leave(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use em um servidor.")
        return

    player = get_player(interaction.guild.id)

    if player.voice_client and player.voice_client.is_connected():
        await disconnect_and_cleanup(interaction.guild)
        await interaction.response.send_message("👋 Saí do canal de voz e apaguei o player.")
    else:
        await interaction.response.send_message("Eu não estou em canal de voz.")


bot.run(TOKEN)
