import os
import re
import json
import time
import random
import shutil
import asyncio
import threading
import http.cookiejar
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI
from dotenv import load_dotenv

try:
    import yt_dlp
except ImportError:  # chat keeps working; music replies with a clear error instead
    yt_dlp = None

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:  # Spotify links fall back to the public embed-page reader
    spotipy = None
    SpotifyClientCredentials = None

# --- CONFIG & ENVIRONMENT ---
load_dotenv()  # must run BEFORE any os.getenv below so a local .env works too
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 10000))  # Render default

# Optional extras (all safe to leave unset):
GUILD_ID = int(os.getenv("GUILD_ID", 0))     # sync slash commands instantly to ONE server (handy for testing)
YTDLP_PROXY = os.getenv("YTDLP_PROXY")       # proxy URL used by yt-dlp
FFMPEG_PATH = os.getenv("FFMPEG_PATH")       # custom ffmpeg location
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")

# Should the bot's replies ping the person it is replying to? (the reply itself is always shown)
REPLY_PING = False

# --- RESPONSE LENGTH SETTINGS ---
# The prompt asks for ~6 sentences (simple) or ~20 (normal/complex).
# The caps below are hard limits applied in code in case a model ignores the prompt.
NORMAL_SENTENCE_CAP = 22
LONG_SENTENCE_CAP = 200      # only used when the user explicitly asks for something long
NORMAL_MAX_TOKENS = 1200
LONG_MAX_TOKENS = 7000
NORMAL_TIMEOUT = 30          # seconds per model attempt
LONG_TIMEOUT = 150
MAX_CHUNKS = 15              # max Discord messages per reply

# A user only gets "long mode" if they clearly ask for it (or add --long).
LONG_PATTERN = re.compile(
    r"--long\b"
    r"|\b(?:long|lengthy|detailed|extended)\s+(?:story|essay|answer|response|reply|explanation|version|article|speech|rant)\b"
    r"|\bin[- ]depth\b|\bcomprehensive\b"
    r"|\bas (?:long|detailed) as (?:you can|possible)\b"
    r"|\bwrite (?:me )?(?:an? )?(?:essay|novel|chapter|article)\b"
    r"|\b(?:[5-9]\d|1\d\d|200)\s+sentences\b",
    re.IGNORECASE,
)

# --- MUSIC SETTINGS ---
MAX_QUEUE = 200              # max tracks waiting in one server's queue
MAX_PLAYLIST_TRACKS = 100    # max tracks taken from one playlist / album link
IDLE_TIMEOUT = 180           # leave the voice channel after this many idle seconds
ALONE_TIMEOUT = 60           # leave after being alone in the voice channel this long
STREAM_TTL = 2 * 3600        # stream URLs go stale; re-resolve ones older than this
RESOLVE_TIMEOUT = 60         # seconds allowed to look up a link / stream
PANEL_QUEUE_LINES = 10       # queue rows shown in the player panel
POTATO_COLOR = 0xD4A373      # embed colour
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def find_ffmpeg():
    """FFMPEG_PATH, then a system ffmpeg, then the static binary shipped by imageio-ffmpeg."""
    for candidate in (FFMPEG_PATH, shutil.which("ffmpeg")):
        if candidate and (shutil.which(candidate) or os.path.isfile(candidate)):
            return candidate
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


FFMPEG_EXE = find_ffmpeg()
# ffmpeg encodes straight to Opus, so no system libopus is needed.
FFMPEG_BEFORE = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn -compression_level 5"


# =============================================================================
#  YOUTUBE COOKIES  (REQUIREMENT 1)
#  Runs at import time, before yt-dlp is ever used.
# =============================================================================
COOKIE_PATH = os.path.abspath("cookies.txt")


def _normalise_cookie_text(raw):
    """Repair the usual damage done to a cookies.txt pasted into an env-var box."""
    raw = raw.strip().strip('"').strip("'")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in raw and "\\n" in raw:      # newlines were flattened into a literal \n
        raw = raw.replace("\\n", "\n")

    lines = []
    for line in raw.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        is_comment = line.startswith("#") and not line.startswith("#HttpOnly_")
        if not is_comment and "\t" not in line:   # tabs were turned into spaces: Netscape needs real tabs
            parts = line.split(None, 6)
            if len(parts) == 7:
                line = "\t".join(parts)
        lines.append(line)

    if not lines or not lines[0].startswith("# Netscape HTTP Cookie File"):
        lines.insert(0, "# Netscape HTTP Cookie File")
    return "\n".join(lines) + "\n"


def setup_cookies():
    """Write YOUTUBE_COOKIES (or YTDLP_COOKIES) to cookies.txt. Returns True if the file exists afterwards."""
    raw = os.getenv("YOUTUBE_COOKIES") or os.getenv("YTDLP_COOKIES")
    if not raw:
        return os.path.exists(COOKIE_PATH)
    raw = raw.strip()
    # YTDLP_COOKIES used to hold a *path* to a cookie file; keep supporting that.
    if "\n" not in raw and len(raw) < 500 and os.path.isfile(raw):
        try:
            with open(raw, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            print(f"⚠️ Couldn't read cookie file '{raw}': {e}")
            return os.path.exists(COOKIE_PATH)
    text = _normalise_cookie_text(raw)
    with open(COOKIE_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    count = sum(1 for ln in text.split("\n") if ln and (not ln.startswith("#") or ln.startswith("#HttpOnly_")))
    print(f"🍪 Wrote {COOKIE_PATH} ({count} cookie lines)")
    if "youtube.com" not in text:
        print("⚠️ cookies.txt has no youtube.com entries — was it exported from a logged-in YouTube tab?")
    return True


def cookies_valid():
    """A malformed cookies.txt makes yt-dlp fail on EVERY site, so check it before handing it over."""
    if not os.path.exists(COOKIE_PATH):
        return False
    try:
        jar = http.cookiejar.MozillaCookieJar(COOKIE_PATH)
        jar.load(ignore_discard=True, ignore_expires=True)
        return True
    except Exception as e:
        print(f"⚠️ cookies.txt is not a valid Netscape cookie file, ignoring it: {type(e).__name__} {str(e)[:120]}")
        return False


HAVE_COOKIES = setup_cookies() and cookies_valid()
PLAYER_CLIENTS = [c.strip() for c in os.getenv("YTDLP_PLAYER_CLIENTS", "tv,ios,mweb").split(",") if c.strip()]

YTDL_OPTIONS = {
    # Prefer a plain HTTP(S) audio stream (more reliable in ffmpeg than HLS, esp. on SoundCloud), then anything.
    "format": "bestaudio[protocol^=http][protocol!*=m3u8]/bestaudio/best",
    "noplaylist": True,                              # a video link with &list=... plays just that video
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "source_address": "0.0.0.0",                     # IPv4 only: YouTube stream URLs are tied to the requesting IP
    "socket_timeout": 20,
    "allowed_extractors": ["default", "-generic"],   # known sites only (no "fetch any URL" fallback)
    "js_runtimes": {"deno": {}, "node": {}},         # YouTube needs a JS runtime: Deno, or Node 22+
    # Fallback clients, tried in order when one is blocked / needs a token:
    "extractor_args": {"youtube": {"player_client": PLAYER_CLIENTS}},
}
if HAVE_COOKIES:
    YTDL_OPTIONS["cookiefile"] = COOKIE_PATH
if YTDLP_PROXY:
    YTDL_OPTIONS["proxy"] = YTDLP_PROXY

# --- ASYNC OPENROUTER SETUP ---
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

DEFAULT_MODELS = [
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-chat",
]
HUNT_LIST = list(DEFAULT_MODELS)
MODEL_SLUG = re.compile(r"^[\w.\-]+/[\w.\-:]+$")

# --- GLOBAL STATE ---
memory_cache = {}
last_openers = {}  # channel id -> first few words of the potato's last reply
error_logs = []
water_count = 0
is_dead = False
START_TIME = time.time()
players = {}       # guild id -> MusicPlayer
join_locks = {}    # guild id -> asyncio.Lock (stops two /play calls connecting at once)
_session = None    # shared aiohttp session, created on first use
_spotipy = None    # shared spotipy client, created on first use

# --- DISCORD SETUP ---
intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Developer Portal


class PotatoBot(commands.Bot):
    async def setup_hook(self):
        """Registers the slash commands (/cmd, /joinvc, ...) with Discord on startup."""
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"✅ Synced {len(synced)} slash command(s) to server {GUILD_ID}")
            else:
                synced = await self.tree.sync()
                print(f"✅ Synced {len(synced)} slash command(s) globally (can take a few minutes to show up)")
        except Exception as e:
            log_error(f"slash sync failed: {type(e).__name__} {str(e)[:150]}")

    async def close(self):
        if _session is not None and not _session.closed:
            await _session.close()
        await super().close()


bot = PotatoBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    # blocks @everyone / role pings; replied_user controls the ping on Discord replies
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=REPLY_PING),
)


# =============================================================================
#  RENDER KEEP-ALIVE SERVER  (REQUIREMENT 3)
#  Point UptimeRobot at https://<your-service>.onrender.com every 5 minutes.
# =============================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"Potato Node: Online")

    def do_HEAD(self):  # UptimeRobot's free plan uses HEAD requests
        self._ok()

    def log_message(self, format, *args):
        return


def run_web_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    except OSError as e:
        print(f"⚠️ Keep-alive server couldn't bind to port {PORT}: {e}")
        return
    print(f"🌍 Keep-alive server active on port {PORT}")
    server.serve_forever()


# --- HELPERS ---
def log_error(msg):
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    error_logs.append(entry)
    del error_logs[:-50]  # keep only the last 50
    print(f"⚠️ {entry}")


async def reply(ctx, content=None, **kwargs):
    """Answer with Discord's built-in reply feature (falls back to a normal message if the original is gone)."""
    kwargs.setdefault("mention_author", REPLY_PING)
    return await ctx.send(
        content,
        reference=ctx.message.to_reference(fail_if_not_exists=False),
        **kwargs,
    )


def trim_sentences(text, limit):
    """Cut text after `limit` sentences, keeping the original formatting."""
    count = 0
    for m in re.finditer(r"[.!?]+(?=\s|$)", text):
        count += 1
        if count >= limit:
            return text[: m.end()].rstrip()
    return text


def split_message(text, limit=1900):
    """Split text into Discord-safe chunks, preferring line/word boundaries."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def get_mood():
    if water_count == 0:
        return "neutral and salty. You are grumpy and cynical"
    elif water_count <= 50:
        return "pleasant and slightly hydrated"
    return "extremely happy and sweet"


def get_identity(long_mode=False, avoid_opener=None):
    if long_mode:
        length = (
            "The user explicitly asked for something long. "
            "Write between 50 and 200 sentences, using only as many as the request truly needs."
        )
    else:
        length = (
            "Length rules: for simple or casual messages, answer in about 6 sentences or fewer. "
            "For normal-to-complex questions, use up to about 20 sentences. "
            "Never go past 20 sentences."
        )
    style = (
        "Start by answering the user's message directly. Never open with thanks or gratitude; "
        "if you feel thankful, mention it only occasionally, in the middle of a reply. "
        "Vary how you begin each reply."
    )
    if avoid_opener:
        style += f" Your last reply began with '{avoid_opener}', so begin differently this time."
    return (
        f"You are 'ilovefrenchfries', a sentient potato. Be {get_mood()}. "
        f"{style} {length} Never admit to being an AI."
    )


def get_opener(text, n=4):
    """First few words of a reply, used to stop the potato repeating itself."""
    return " ".join(text.split()[:n])


def is_admin():
    """Admin-only check. Non-admins are silently ignored (see on_command_error)."""

    async def predicate(ctx):
        return ctx.author.id == ADMIN_ID

    return commands.check(predicate)


def shorten(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def md_safe(text):
    """Escape Discord markdown (and [ ] so titles can't break masked links)."""
    return discord.utils.escape_markdown(str(text)).replace("[", "(").replace("]", ")")


def fmt_duration(seconds):
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def hydration_bar(value, width=10):
    filled = round(width * max(0, min(value, 100)) / 100)
    return "█" * filled + "░" * (width - filled)


POTATO_FACTS = [
    "Potatoes were the first vegetable grown in space — on the Space Shuttle Columbia in 1995.",
    "A potato is roughly 80% water, which is why overwatering me is a bit rude.",
    "Over 4,000 native potato varieties grow in the Andes, where potatoes were first domesticated thousands of years ago.",
    "Potatoes are the world's third most important food crop for human consumption, after rice and wheat.",
    "Sweet potatoes aren't related to potatoes. Potatoes are nightshades, cousins of tomatoes.",
    "Mr. Potato Head was the first toy ever advertised on television, back in 1952.",
    "Belgium and France have argued for ages over who invented the french fry.",
    "Spanish explorers brought potatoes to Europe in the late 1500s.",
    "Green patches and sprouts on a potato contain solanine. Cut them off, never eat them.",
    "Potato plants flower, and the little green fruits they sometimes make are poisonous.",
    "The War of the Bavarian Succession (1778-79) is nicknamed the 'Potato War' because armies spent so much time foraging for spuds.",
]


# =============================================================================
#  NO DMs  (REQUIREMENT 4)
#  Prefix commands: a global check that fails outside servers.
#  Slash commands: every command below carries @app_commands.guild_only().
# =============================================================================
@bot.check
async def block_dms(ctx):
    return ctx.guild is not None  # CheckFailure -> silently ignored in on_command_error


# --- USER COMMANDS ---
@bot.command(aliases=["c"])
@commands.cooldown(1, 8, commands.BucketType.user)
async def chat(ctx, *, message: str):
    if is_dead:
        await reply(ctx, "... (The potato is dead.)")
        return

    long_mode = bool(LONG_PATTERN.search(message))
    message = re.sub(r"--long\b", "", message, flags=re.IGNORECASE).strip() or "Say something."

    max_tokens = LONG_MAX_TOKENS if long_mode else NORMAL_MAX_TOKENS
    timeout = LONG_TIMEOUT if long_mode else NORMAL_TIMEOUT
    cap = LONG_SENTENCE_CAP if long_mode else NORMAL_SENTENCE_CAP

    async with ctx.typing():
        history = list(memory_cache.get(ctx.channel.id, []))
        user_line = f"{ctx.author.display_name}: {message}"
        messages = (
            [{"role": "system", "content": get_identity(long_mode, last_openers.get(ctx.channel.id))}]
            + history
            + [{"role": "user", "content": user_line}]
        )

        response_text = None
        for model_id in list(HUNT_LIST):
            try:
                completion = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model_id,
                        messages=messages,
                        max_tokens=max_tokens,
                    ),
                    timeout=timeout,
                )
                response_text = (completion.choices[0].message.content or "").strip()
                if response_text:
                    break
                log_error(f"{model_id}: empty reply")
            except Exception as e:
                log_error(f"{model_id} fail: {type(e).__name__} {str(e)[:100]}")

        if not response_text:
            await reply(ctx, "The cloud is a desert. Try again in a bit.")
            return

        response_text = trim_sentences(response_text, cap)
        last_openers[ctx.channel.id] = get_opener(response_text)

        # Save a shortened copy so long replies don't bloat future prompts
        history.append({"role": "user", "content": user_line})
        history.append({"role": "assistant", "content": response_text[:1500]})
        memory_cache[ctx.channel.id] = history[-6:]

        for i, chunk in enumerate(split_message(response_text)[:MAX_CHUNKS]):
            if i == 0:
                await reply(ctx, chunk)      # first part is a real Discord reply to the asker
            else:
                await ctx.send(chunk)        # the rest follows underneath


@bot.command()
async def ffw(ctx):
    global water_count, is_dead
    if is_dead:
        await reply(ctx, "You are watering a corpse. It's too late.")
        return

    water_count += 1
    if water_count > 100:
        is_dead = True
        await reply(ctx, "🌊 **OVERWATERED!** The potato died. 💀")
    else:
        await reply(ctx, f"💧 Hydration: `{water_count}/100`.")


@bot.command()
async def ffmood(ctx):
    """Public mood / hydration check."""
    if is_dead:
        await reply(ctx, "💀 The potato is dead. Ask an admin to resurrect it.")
        return
    mood = get_mood().split(".")[0]  # "neutral and salty", "pleasant and slightly hydrated", ...
    embed = discord.Embed(
        title="🥔 Potato Mood",
        description=f"The potato feels **{mood}**.",
        color=POTATO_COLOR,
    )
    embed.add_field(name="💧 Hydration", value=f"`{hydration_bar(water_count)}` {water_count}/100", inline=False)
    embed.set_footer(text="Use !ffw to water it")
    await reply(ctx, embed=embed)


@bot.command()
@commands.cooldown(1, 5, commands.BucketType.user)
async def fffact(ctx):
    """Random potato fact."""
    if is_dead:
        await reply(ctx, "... (The potato is dead. Dead potatoes tell no facts.)")
        return
    await reply(ctx, f"🥔 **Potato fact:** {random.choice(POTATO_FACTS)}")


# --- ADMIN COMMANDS ---
@bot.command()
@is_admin()
async def ffrsr(ctx):
    """Resurrect the potato and reset hydration to 0."""
    global water_count, is_dead
    water_count, is_dead = 0, False
    await reply(ctx, "🪄 The potato has been resurrected!")


@bot.command()
@is_admin()
async def logs(ctx):
    """Show the last 5 error entries."""
    out = "\n".join(error_logs[-5:]) or "No errors recorded."
    await reply(ctx, f"📋 **Recent Error Logs:**\n```{out[:1800]}```")


@bot.command()
@is_admin()
async def ffstatus(ctx):
    """Show a full status report."""
    uptime = int(time.time() - START_TIME)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    lines = [
        f"State:      {'DEAD' if is_dead else 'alive'}",
        f"Hydration:  {water_count}/100",
        f"Mood:       {get_mood().split('.')[0]}",
        f"Uptime:     {h}h {m}m {s}s",
        f"Latency:    {bot.latency * 1000:.0f} ms",
        f"Servers:    {len(bot.guilds)}",
        f"Memories:   {len(memory_cache)} channel(s)",
        f"Errors:     {len(error_logs)} logged",
        f"Cookies:    {'loaded' if YTDL_OPTIONS.get('cookiefile') else 'none/invalid'}",
        f"YouTube:    {youtube_status()}",
        f"yt-dlp:     {ytdlp_version()}",
        f"Spotify:    {'spotipy' if _spotipy_configured() else 'embed fallback'}",
        f"Models:     {' > '.join(x.split('/')[-1] for x in HUNT_LIST)}",
    ]
    await reply(ctx, "🥔 **Potato Status**\n```" + "\n".join(lines) + "```")


@bot.command()
@is_admin()
async def ffsetwater(ctx, amount: int):
    """Set hydration to an exact value (0-100). Also revives the potato."""
    global water_count, is_dead
    if not 0 <= amount <= 100:
        await reply(ctx, "Pick a number from 0 to 100.")
        return
    water_count, is_dead = amount, False
    await reply(ctx, f"💧 Hydration set to `{water_count}/100`.")


@bot.command()
@is_admin()
async def ffkill(ctx):
    """Kill the potato instantly."""
    global is_dead
    is_dead = True
    await reply(ctx, "💀 The potato has been... peeled.")


@bot.command()
@is_admin()
async def ffwipe(ctx, scope: str = "channel"):
    """Clear chat memory for this channel, or `all` channels."""
    if scope.lower() == "all":
        count = len(memory_cache)
        memory_cache.clear()
        last_openers.clear()
        await reply(ctx, f"🧽 Wiped memory for {count} channel(s).")
    else:
        memory_cache.pop(ctx.channel.id, None)
        last_openers.pop(ctx.channel.id, None)
        await reply(ctx, "🧽 The potato forgot this channel.")


@bot.command()
@is_admin()
async def ffclearlogs(ctx):
    """Clear the error log."""
    count = len(error_logs)
    error_logs.clear()
    await reply(ctx, f"🗑️ Cleared {count} log entr{'y' if count == 1 else 'ies'}.")


@bot.command()
@is_admin()
async def ffsay(ctx, *, text: str):
    """Make the potato say something (deletes your command message, so this one can't be a reply)."""
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    await ctx.send(text[:2000])


@bot.command()
@is_admin()
async def ffmodel(ctx, action: str = "list", model_id: Optional[str] = None):
    """Manage the model fallback order: list | add | remove | first | reset."""
    action = action.lower()

    if action in ("add", "remove", "first"):
        if not model_id or not MODEL_SLUG.match(model_id):
            await reply(ctx, f"Usage: `!ffmodel {action} provider/model-name`")
            return
        if action == "add":
            if model_id in HUNT_LIST:
                await reply(ctx, "That model is already in the list.")
                return
            HUNT_LIST.append(model_id)
        elif action == "first":
            if model_id in HUNT_LIST:
                HUNT_LIST.remove(model_id)
            HUNT_LIST.insert(0, model_id)
        else:  # remove
            if model_id not in HUNT_LIST:
                await reply(ctx, "That model isn't in the list.")
                return
            if len(HUNT_LIST) == 1:
                await reply(ctx, "Can't remove the last model.")
                return
            HUNT_LIST.remove(model_id)
    elif action == "reset":
        HUNT_LIST[:] = DEFAULT_MODELS
    elif action != "list":
        await reply(ctx, "Usage: `!ffmodel [list | add <id> | remove <id> | first <id> | reset]`")
        return

    listing = "\n".join(f"{i}. {m}" for i, m in enumerate(HUNT_LIST, 1))
    await reply(ctx, f"🧠 **Model order:**\n```{listing}```")


@bot.command()
@is_admin()
async def fftest(ctx, *, query: str):
    """Run a lookup and show the RAW yt-dlp result/error (the fastest way to see why a link fails)."""
    query = query.strip().strip("<>")
    is_url = bool(re.match(r"https?://", query, re.IGNORECASE))
    if is_url:
        query = await expand_short_link(query)
    spotify = parse_spotify(query) if is_url else None
    if spotify:  # same path /play uses: read the Spotify metadata, then match the first track on YouTube/SoundCloud
        async with ctx.typing():
            try:
                tracks, title = await resolve_spotify(spotify[0], spotify[1], ctx.author.display_name)
                t = tracks[0]
                await t._resolve_by_search()
                await reply(
                    ctx,
                    f"✅ Spotify read OK ({len(tracks)} track(s)). First: **{md_safe(shorten(t.title, 70))}**\n"
                    f"matched on **{t.origin}** → `{shorten(t.page_url, 80)}`\n"
                    f"YouTube status: {youtube_status()}",
                )
            except Exception as e:
                await reply(ctx, f"❌ `{type(e).__name__}` while playing the first Spotify track\n```{str(e)[:1500]}```")
        return
    target = query if is_url else f"ytsearch1:{query}"
    async with ctx.typing():
        try:
            info = await ytdl_extract(target, limit=1)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), None)
            info = info or {}
            await reply(
                ctx,
                f"✅ `{info.get('extractor_key')}` · {shorten(info.get('title'), 80)}\n"
                f"stream url: **{'yes' if pick_stream_url(info) else 'NO'}** · protocol `{info.get('protocol')}` · "
                f"ext `{info.get('ext')}` · duration `{fmt_duration(info.get('duration'))}`",
            )
        except Exception as e:
            await reply(ctx, f"❌ `{type(e).__name__}` for `{shorten(target, 80)}`\n```{str(e)[:1500]}```")


# =============================================================================
#  /cmd and /cmda COMMAND PANELS
#  The lists below are what the panels display. When you add a command to the
#  bot, add a row here too.
# =============================================================================
USER_COMMANDS = [
    ("🥔 Potato", [
        ("!c  /  !chat <message>", "Chat with the potato (add `--long` for a long answer)"),
        ("!ffw", "Water the potato 💧"),
        ("!ffmood", "Check the potato's hydration and mood"),
        ("!fffact", "Hear a random potato fact"),
    ]),
    ("🎵 Music", [
        ("/joinvc", "Join your voice channel (or the VC chat you type it in)"),
        ("/play <link or search>", "Play YouTube / Spotify / SoundCloud links"),
        ("/queue", "Open the player panel: queue + buttons"),
        ("/skip  /pause  /resume  /stop", "Control playback"),
        ("/leave", "Disconnect the bot from voice"),
    ]),
    ("ℹ️ Help", [
        ("/cmd", "Show this panel"),
    ]),
]

ADMIN_COMMANDS = [
    ("🥔 Potato control", [
        ("!ffrsr", "Resurrect the potato and reset hydration to 0"),
        ("!ffkill", "Kill the potato instantly"),
        ("!ffsetwater <0-100>", "Set hydration to an exact value (also revives)"),
        ("!ffsay <text>", "Make the potato say something (deletes your message)"),
    ]),
    ("🧠 AI & memory", [
        ("!ffmodel [list|add|remove|first|reset] [provider/model]", "Manage the model fallback order"),
        ("!ffwipe [all]", "Clear chat memory for this channel (or all channels)"),
    ]),
    ("📋 Diagnostics", [
        ("!ffstatus", "Full status report"),
        ("!logs", "Show the last 5 error entries"),
        ("!ffclearlogs", "Clear the error log"),
        ("!fftest <link or search>", "Run a lookup and show the raw yt-dlp result/error"),
    ]),
    ("🛠️ Panels", [
        ("/cmda", "Show this admin panel"),
    ]),
]


def build_command_panel(title, description, sections):
    embed = discord.Embed(title=title, description=description, color=POTATO_COLOR)
    for name, rows in sections:
        value = "\n".join(f"`{cmd}` — {desc}" for cmd, desc in rows)
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="! = prefix command   •   / = slash command   •   servers only (no DMs)")
    return embed


@bot.tree.command(name="cmd", description="Show everything the potato can do")
@app_commands.guild_only()
async def cmd_panel(interaction: discord.Interaction):
    embed = build_command_panel("🥔 Potato Commands", "Here's what you can do:", USER_COMMANDS)
    await interaction.response.send_message(embed=embed, ephemeral=True)  # ephemeral=False to show everyone


@bot.tree.command(name="cmda", description="Admin command panel (admins only)")
@app_commands.guild_only()
async def cmda_panel(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("🚫 Admins only.", ephemeral=True)
        return
    embed = build_command_panel("🛠️ Admin Commands", "Only you can see this panel.", ADMIN_COMMANDS)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =============================================================================
#  MUSIC
# =============================================================================
class UserFacingError(Exception):
    """An error whose message is safe to show to Discord users."""


def explain_error(err):
    """Turn an exception into a short sentence that is safe to show in Discord."""
    if isinstance(err, UserFacingError):
        return str(err)
    if isinstance(err, asyncio.TimeoutError):
        return "that took too long to load."
    low = str(err).lower()
    if "unsupported url" in low:
        return "that link isn't supported."
    if "confirm your age" in low or "age-restricted" in low or "age restricted" in low:
        return "that video is age-restricted."
    if "not a bot" in low or "sign in to confirm" in low:
        return ("YouTube is blocking my server right now (bot check) — the bot owner may need to "
                "refresh the cookies (see `!logs`).")
    if any(w in low for w in ("private video", "video unavailable", "not available", "been removed", "deleted")):
        return "that video isn't available."
    return "I couldn't load that."


def get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


def ffmpeg_available():
    return bool(shutil.which(FFMPEG_EXE) or os.path.isfile(FFMPEG_EXE))


# ---------- yt-dlp ----------
def is_youtube_target(query):
    q = str(query).lower()
    return q.startswith("ytsearch") or "youtube.com" in q or "youtu.be" in q


def _ytdl_sync(query, flat, limit):
    opts = dict(YTDL_OPTIONS)
    opts["playlistend"] = limit
    if not is_youtube_target(query):
        opts.pop("cookiefile", None)  # YouTube cookies are only for YouTube
    if flat:
        opts["extract_flat"] = "in_playlist"  # list playlist entries without resolving each one
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


async def ytdl_extract(query, *, flat=False, limit=1):
    """yt-dlp is blocking, so it runs in a worker thread."""
    if yt_dlp is None:
        raise UserFacingError("Music isn't set up on this host (yt-dlp isn't installed).")
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, _ytdl_sync, query, flat, limit),
        timeout=RESOLVE_TIMEOUT,
    )


def entry_url(entry):
    url = entry.get("webpage_url") or entry.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    if entry.get("id") and str(entry.get("ie_key") or "").lower().startswith("youtube"):
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return None


def pick_thumb(entry):
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    if thumbs and isinstance(thumbs[-1], dict):
        return thumbs[-1].get("url")
    return None


def pick_stream_url(info):
    if not info:
        return None
    if info.get("url"):
        return info["url"]
    for f in info.get("requested_formats") or []:
        if f.get("url"):
            return f["url"]
    return None


def origin_label(key):
    key = str(key or "").lower()
    if key.startswith("youtube"):
        return "YouTube"
    if key.startswith("soundcloud"):
        return "SoundCloud"
    return str(key).title() or "Web"


@dataclass(eq=False)
class Track:
    title: str
    requester: str
    link: Optional[str] = None        # page shown in the panel (what the user shared)
    page_url: Optional[str] = None    # page yt-dlp pulls the audio from
    search: Optional[str] = None      # "ytsearch:Artist - Title": Spotify tracks are matched on YouTube when played
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    origin: str = "Web"
    stream_url: Optional[str] = None
    resolved_at: float = 0.0
    retried: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def stream_fresh(self):
        return bool(self.stream_url) and (time.time() - self.resolved_at) < STREAM_TTL

    async def _resolve_by_search(self):
        """Spotify tracks: find a match on YouTube, or on SoundCloud when YouTube is blocking us."""
        last_error = None
        for source in music_sources():
            try:
                best = await best_match(source, self.search or f"ytsearch:{self.title}", self.duration)
                info = await ytdl_extract(best["url"], limit=1)
                if info and info.get("entries"):
                    info = next((e for e in info["entries"] if e), None)
                if not pick_stream_url(info):
                    raise UserFacingError("I couldn't find playable audio for that.")
                self.page_url = best["url"]
                self.thumbnail = self.thumbnail or best.get("thumbnail")
                self.origin = f"Spotify → {source.title()}"
                return info
            except Exception as e:
                last_error = e
                log_error(f"{source} match for '{shorten(self.title, 40)}': {type(e).__name__} {str(e)[:120]}")
                if source == "youtube" and is_bot_check(e):
                    block_youtube()
        raise last_error or UserFacingError("I couldn't find a playable match for that track.")

    async def ensure_stream(self):
        """Look up the direct audio URL (stream URLs expire, so this happens right before playing)."""
        async with self._lock:
            if self.stream_fresh:
                return
            if self.page_url:
                info = await ytdl_extract(self.page_url, limit=1)
                if info and info.get("entries"):
                    info = next((e for e in info["entries"] if e), None)
            else:
                info = await self._resolve_by_search()
            stream = pick_stream_url(info)
            if not stream:
                raise UserFacingError("I couldn't find playable audio for that.")
            self.stream_url = stream
            self.resolved_at = time.time()
            self.duration = self.duration or info.get("duration")
            self.thumbnail = self.thumbnail or pick_thumb(info)


# ---- which site Spotify tracks are matched on ----
# SPOTIFY_SOURCES="youtube,soundcloud" (default) tries YouTube first. Once YouTube answers with the
# bot check, it is skipped for YOUTUBE_BLOCK_SECONDS so every track doesn't wait for the same failure.
SPOTIFY_SOURCES = [s for s in (x.strip().lower() for x in os.getenv("SPOTIFY_SOURCES", "youtube,soundcloud").split(","))
                   if s in ("youtube", "soundcloud")] or ["youtube", "soundcloud"]
YOUTUBE_BLOCK_SECONDS = 600
youtube_blocked_until = 0.0


def is_bot_check(err):
    low = str(err).lower()
    return "not a bot" in low or "sign in to confirm" in low


def youtube_blocked():
    return time.time() < youtube_blocked_until


def block_youtube():
    global youtube_blocked_until
    if not youtube_blocked():
        log_error(f"YouTube bot check hit: skipping YouTube for {YOUTUBE_BLOCK_SECONDS // 60} min")
    youtube_blocked_until = time.time() + YOUTUBE_BLOCK_SECONDS


def youtube_status():
    left = int(youtube_blocked_until - time.time())
    return f"blocked (retry in {left}s)" if left > 0 else "ok / untested"


def ytdlp_version():
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return "n/a"


def music_sources():
    sources = list(SPOTIFY_SOURCES)
    if youtube_blocked() and len(sources) > 1:
        sources = [s for s in sources if s != "youtube"]
    return sources


async def best_match(engine, query, expected_seconds):
    """Search YouTube or SoundCloud for 'Artist - Title'; prefer the result whose length is closest to the original."""
    text = re.sub(r"^(?:yt|sc)search\d*:", "", query).strip()
    prefix = "scsearch5" if engine == "soundcloud" else "ytsearch5"
    info = await ytdl_extract(f"{prefix}:{text}", flat=True, limit=5)
    entries = [e for e in ((info or {}).get("entries") or []) if e and entry_url(e)]
    if not entries:
        raise UserFacingError(f"I couldn't find a {engine.title()} match for that track.")
    if expected_seconds:
        close = [e for e in entries if e.get("duration") and abs(e["duration"] - expected_seconds) <= 10]
        if close:
            entries = close
    best = entries[0]
    return {"url": entry_url(best), "thumbnail": pick_thumb(best)}


def track_from_entry(entry, requester, origin):
    url = entry_url(entry)
    if not url or entry.get("_type") == "playlist" or entry.get("ie_key") == "YoutubeTab":
        return None  # channel tabs, nested playlists...
    if entry.get("title") in ("[Private video]", "[Deleted video]"):
        return None
    duration = entry.get("duration")
    return Track(
        title=entry.get("title") or "Unknown title",
        requester=requester,
        link=url,
        page_url=url,
        duration=int(duration) if duration else None,
        thumbnail=pick_thumb(entry),
        origin=origin,
    )


# =============================================================================
#  SPOTIFY  (REQUIREMENT 2)
#  1) spotipy (needs SPOTIPY_CLIENT_ID + SPOTIPY_CLIENT_SECRET) -> "ytsearch:Artist - Title"
#  2) if spotipy isn't configured, or Spotify refuses the request (it restricts
#     some playlists for new developer apps), fall back to reading the public embed page.
# =============================================================================
SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:embed/)?(?:intl-[a-z-]+/)?(?:user/[^/\s]+/)?|spotify:(?:user:[^:\s]+:)?)"
    r"(track|album|playlist)[/:]([A-Za-z0-9]{22})",
    re.IGNORECASE,
)
SPOTIFY_FAIL = "I couldn't read that Spotify link (it may be private, or Spotify changed its page)."


def parse_spotify(text):
    m = SPOTIFY_RE.search(text or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def is_spotify_host(url):
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("spotify.com") or host.endswith("spotify.link")


SOUNDCLOUD_SHORT_HOSTS = ("on.soundcloud.com", "soundcloud.app.goo.gl")


async def expand_short_link(url):
    """Mobile 'share' links -> the real link: spotify.link/xxxx and on.soundcloud.com/xxxx."""
    host = (urlparse(url).hostname or "").lower()
    if host in SOUNDCLOUD_SHORT_HOSTS:
        try:
            async with get_session().get(url, headers={"User-Agent": BROWSER_UA}, allow_redirects=True) as r:
                final = str(r.url)
            final_host = (urlparse(final).hostname or "").lower()
            if final_host.endswith("soundcloud.com") and final_host not in SOUNDCLOUD_SHORT_HOSTS:
                return final
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        return url
    if host not in ("spotify.link", "spotify.app.link"):
        return url
    try:
        async with get_session().get(url, headers={"User-Agent": BROWSER_UA}, allow_redirects=True) as r:
            final = str(r.url)
            if SPOTIFY_RE.search(final):
                return final
            body = await r.text()
        m = SPOTIFY_RE.search(body)
        return m.group(0) if m else url
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return url


def make_spotify_track(artists, name, duration, thumb, link, requester):
    """Build a Track that is matched on YouTube with the query 'ytsearch:Artist - Title' when played."""
    name = str(name or "").strip()
    if not name:
        return None
    artists = str(artists or "").strip()
    first_artist = artists.split(",")[0].strip()
    query = f"{first_artist} - {name}" if first_artist else name
    return Track(
        title=f"{artists} - {name}" if artists else name,
        requester=requester,
        link=link,
        search=f"ytsearch:{query}",
        duration=duration,
        thumbnail=thumb,
        origin="Spotify → YouTube",
    )


# ---- spotipy path ----
def _spotipy_configured():
    return bool(spotipy and SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET)


def _spotipy_client():
    global _spotipy
    if _spotipy is None and _spotipy_configured():
        _spotipy = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIPY_CLIENT_ID,
                client_secret=SPOTIPY_CLIENT_SECRET,
            ),
            requests_timeout=10,
            retries=1,
        )
    return _spotipy


def _sp_artists(item):
    return ", ".join(a["name"] for a in (item.get("artists") or []) if isinstance(a, dict) and a.get("name"))


def _sp_image(obj):
    images = (obj or {}).get("images") or []
    return images[0].get("url") if images and isinstance(images[0], dict) else None


def _sp_row(item, thumb, sid=None):
    ms = item.get("duration_ms")
    link = f"https://open.spotify.com/track/{item.get('id') or sid}" if (item.get("id") or sid) else None
    return {
        "artists": _sp_artists(item),
        "name": item.get("name"),
        "duration": int(ms / 1000) if isinstance(ms, (int, float)) and ms > 0 else None,
        "thumb": thumb,
        "link": link,
    }


def _spotipy_fetch(kind, sid):
    """Blocking: returns (rows, collection_title). Runs in a worker thread."""
    sp = _spotipy_client()
    rows, title = [], None

    if kind == "track":
        t = sp.track(sid)
        rows.append(_sp_row(t, _sp_image(t.get("album")), sid))
        return rows, None

    if kind == "album":
        album = sp.album(sid)
        title, thumb = album.get("name"), _sp_image(album)
        page = album.get("tracks") or {}
        while page:
            for t in page.get("items") or []:
                if t and t.get("name"):
                    rows.append(_sp_row(t, thumb))
            if len(rows) >= MAX_PLAYLIST_TRACKS or not page.get("next"):
                break
            page = sp.next(page)
        return rows[:MAX_PLAYLIST_TRACKS], title

    # playlist
    try:
        title = (sp.playlist(sid, fields="name") or {}).get("name")
    except Exception:
        title = None
    page = sp.playlist_items(sid, limit=100, additional_types=("track",))
    while page:
        for it in page.get("items") or []:
            t = (it or {}).get("track") or (it or {}).get("item")  # Spotify has been renaming this key
            if t and t.get("name") and t.get("type", "track") == "track":
                rows.append(_sp_row(t, _sp_image(t.get("album"))))
        if len(rows) >= MAX_PLAYLIST_TRACKS or not page.get("next"):
            break
        page = sp.next(page)
    return rows[:MAX_PLAYLIST_TRACKS], title


async def resolve_spotify_spotipy(kind, sid, requester):
    loop = asyncio.get_running_loop()
    rows, title = await asyncio.wait_for(
        loop.run_in_executor(None, _spotipy_fetch, kind, sid),
        timeout=RESOLVE_TIMEOUT,
    )
    fallback_link = f"https://open.spotify.com/{kind}/{sid}"
    tracks = []
    for r in rows:
        t = make_spotify_track(r["artists"], r["name"], r["duration"], r["thumb"], r["link"] or fallback_link, requester)
        if t:
            tracks.append(t)
    return tracks, title


# ---- embed-page fallback (no API key) ----
def find_spotify_entity(data, kind, sid):
    """Find the dict describing the requested item anywhere inside the page's JSON (layout-proof)."""
    want = f"spotify:{kind}:{sid}".lower()
    candidates, stack = [], [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            uri = cur.get("uri")
            if isinstance(uri, str) and uri.lower() == want and (cur.get("name") or cur.get("title")):
                candidates.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    if not candidates:
        return None
    if kind == "track":
        return candidates[0]
    return max(candidates, key=lambda c: len(c.get("trackList") or []))


async def fetch_spotify_entity(kind, sid):
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        async with get_session().get(url, headers=headers) as r:
            if r.status != 200:
                raise UserFacingError("Spotify wouldn't show me that link (private, removed or region-locked).")
            page = await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise UserFacingError("I couldn't reach Spotify right now.")
    m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, re.DOTALL)
    if not m:
        raise UserFacingError(SPOTIFY_FAIL)
    try:
        data = json.loads(m.group(1))
    except ValueError:
        raise UserFacingError(SPOTIFY_FAIL)
    entity = find_spotify_entity(data, kind, sid)
    if not entity:
        raise UserFacingError(SPOTIFY_FAIL)
    return entity


def _embed_artists(item):
    artists = item.get("artists")
    if isinstance(artists, list):
        names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
        if names:
            return ", ".join(names)
    sub = item.get("subtitle")
    return sub.strip() if isinstance(sub, str) else ""


def _embed_seconds(item):
    ms = item.get("duration") or item.get("durationMs") or item.get("duration_ms")
    if isinstance(ms, dict):
        ms = ms.get("totalMilliseconds")
    return int(ms / 1000) if isinstance(ms, (int, float)) and ms > 0 else None


def _embed_cover(entity):
    sources = None
    cover = entity.get("coverArt")
    if isinstance(cover, dict):
        sources = cover.get("sources")
    if not sources and isinstance(entity.get("visualIdentity"), dict):
        sources = entity["visualIdentity"].get("image")
    if not sources:
        sources = entity.get("images")
    if isinstance(sources, list) and sources:
        sources = [s for s in sources if isinstance(s, dict) and s.get("url")]
        if sources:
            best = max(sources, key=lambda s: s.get("width") or s.get("maxWidth") or 0)
            return best["url"]
    return None


def _link_from_uri(uri):
    m = re.match(r"spotify:(track|album|playlist):([A-Za-z0-9]{22})", uri or "")
    return f"https://open.spotify.com/{m.group(1)}/{m.group(2)}" if m else None


async def resolve_spotify_embed(kind, sid, requester):
    entity = await fetch_spotify_entity(kind, sid)
    link = f"https://open.spotify.com/{kind}/{sid}"
    cover = _embed_cover(entity)
    if kind == "track":
        track = make_spotify_track(
            _embed_artists(entity), entity.get("name") or entity.get("title"),
            _embed_seconds(entity), cover, link, requester,
        )
        if not track:
            raise UserFacingError(SPOTIFY_FAIL)
        return [track], None
    tracks = []
    for item in (entity.get("trackList") or [])[:MAX_PLAYLIST_TRACKS]:
        if isinstance(item, dict):
            t = make_spotify_track(
                _embed_artists(item), item.get("name") or item.get("title"), _embed_seconds(item),
                cover if kind == "album" else None, _link_from_uri(item.get("uri")) or link, requester,
            )
            if t:
                tracks.append(t)
    if not tracks:
        raise UserFacingError("That Spotify album/playlist looks empty (or it's private).")
    return tracks, str(entity.get("name") or entity.get("title") or f"Spotify {kind}")


async def resolve_spotify(kind, sid, requester):
    """spotipy first, embed-page reader as the safety net."""
    if _spotipy_configured():
        try:
            tracks, title = await resolve_spotify_spotipy(kind, sid, requester)
            if tracks:
                return tracks, title
            log_error(f"spotipy returned nothing for {kind} {sid}; trying embed fallback")
        except Exception as e:
            log_error(f"spotipy {kind} failed: {type(e).__name__} {str(e)[:120]}; trying embed fallback")
    return await resolve_spotify_embed(kind, sid, requester)


# ---------- turning whatever the user typed into tracks ----------
async def resolve_query(query, requester):
    """Returns (tracks, collection_title_or_None)."""
    query = query.strip().strip("<>")  # <link> is how people stop Discord embedding it
    if not query:
        raise UserFacingError("Give me a link, or some words to search for.")
    if len(query) > 500:
        raise UserFacingError("That's too long.")
    is_url = bool(re.match(r"https?://", query, re.IGNORECASE))

    if is_url:
        query = await expand_short_link(query)
    spotify = parse_spotify(query)
    if spotify:
        return await resolve_spotify(spotify[0], spotify[1], requester)
    if is_url and is_spotify_host(query):
        raise UserFacingError("I can play Spotify **track**, **album** and **playlist** links (not artists or podcasts).")

    target = query if is_url else f"ytsearch1:{query}"
    try:
        info = await ytdl_extract(target, flat=True, limit=MAX_PLAYLIST_TRACKS if is_url else 1)
    except UserFacingError:
        raise
    except Exception as e:
        log_error(f"lookup '{shorten(query, 50)}': {type(e).__name__} {str(e)[:150]}")
        raise UserFacingError(f"Sorry, {explain_error(e)}")
    if not info:
        raise UserFacingError("I couldn't find anything for that.")

    entries = info.get("entries")
    origin = origin_label(info.get("extractor_key"))
    if entries is None:  # a single video / track: yt-dlp already gave us the full details
        page = info.get("webpage_url") or query
        track = Track(
            title=info.get("title") or "Unknown title",
            requester=requester,
            link=page,
            page_url=page,
            duration=int(info["duration"]) if info.get("duration") else None,
            thumbnail=pick_thumb(info),
            origin=origin,
        )
        stream = pick_stream_url(info)
        if stream:
            track.stream_url, track.resolved_at = stream, time.time()
        return [track], None

    tracks = []
    for entry in entries:
        if entry:
            t = track_from_entry(entry, requester, origin_label(entry.get("ie_key")) if entry.get("ie_key") else origin)
            if t:
                tracks.append(t)
    if not tracks:
        raise UserFacingError("I couldn't find anything playable there.")
    if not is_url:  # a text search: just the top result
        return tracks[:1], None
    return tracks, info.get("title")


# ---------- voice ----------
def pick_voice_channel(interaction):
    """The voice channel the user is in; otherwise the VC whose chat the command was typed in."""
    voice = getattr(interaction.user, "voice", None)
    if voice and voice.channel:
        return voice.channel
    if isinstance(interaction.channel, (discord.VoiceChannel, discord.StageChannel)):
        return interaction.channel
    return None


def voice_target_or_raise(interaction):
    if yt_dlp is None:
        raise UserFacingError("Music isn't set up on this host (yt-dlp isn't installed).")
    if not getattr(discord.voice_client, "has_nacl", True):
        raise UserFacingError("Voice support isn't installed on this host (PyNaCl is missing).")
    if not ffmpeg_available():
        raise UserFacingError("ffmpeg isn't installed on this host, so I can't play audio.")
    target = pick_voice_channel(interaction)
    if target is None:
        raise UserFacingError("Join a voice channel first — or use this command inside a voice channel's chat.")
    perms = target.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.connect and perms.speak):
        raise UserFacingError(f"I need **View Channel**, **Connect** and **Speak** permissions in **{target.name}**.")
    vc = interaction.guild.voice_client
    player = players.get(interaction.guild.id)
    if vc and vc.channel and vc.channel.id != target.id and player and (player.current or player.queue):
        raise UserFacingError(f"I'm already playing in **{vc.channel.name}**. Join me there, or wait until I'm done.")
    return target


async def connect_player(interaction, target):
    """Join `target` (or move there if idle) and return this server's MusicPlayer."""
    guild = interaction.guild
    async with join_locks.setdefault(guild.id, asyncio.Lock()):
        vc = guild.voice_client
        player = players.get(guild.id)
        if vc is not None and not vc.is_connected():  # stale connection
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None
        if vc is None:
            try:
                vc = await target.connect(self_deaf=True)
            except asyncio.TimeoutError:
                raise UserFacingError("Timed out while connecting to the voice channel.")
            except Exception as e:
                log_error(f"voice connect failed: {type(e).__name__} {str(e)[:150]}")
                raise UserFacingError("I couldn't connect to that voice channel (see `!logs`).")
        elif vc.channel.id != target.id:
            if player is not None and (player.current or player.queue):
                raise UserFacingError(f"I'm already playing in **{vc.channel.name}**.")
            await vc.move_to(target)
        if player is None or player.destroyed:
            player = MusicPlayer(guild)
            players[guild.id] = player
        player.text_channel = interaction.channel
        player.start()
        return player


def control_error(guild, user):
    """None if `user` may control the music, otherwise the reason they can't."""
    if user.id == ADMIN_ID:
        return None
    vc = guild.voice_client
    if vc is None or vc.channel is None:
        return "I'm not in a voice channel."
    voice = getattr(user, "voice", None)
    if voice is None or voice.channel is None or voice.channel.id != vc.channel.id:
        return f"Join **{vc.channel.name}** to control the music."
    return None


async def get_controllable_player(interaction):
    """Used by /skip, /pause...: returns the player, or replies with the problem and returns None."""
    player = players.get(interaction.guild.id)
    if player is None or player.voice_client is None:
        await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
        return None
    problem = control_error(interaction.guild, interaction.user)
    if problem:
        await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        return None
    return player


class MusicPlayer:
    """One per server: owns the queue, the playback loop and the control panel message."""

    def __init__(self, guild):
        self.guild = guild
        self.queue = deque()
        self.current = None
        self.loop_mode = "off"  # off | track | queue
        self.text_channel = None
        self.panel_message = None
        self.view = None
        self.destroyed = False
        self._wake = asyncio.Event()
        self._skip = False
        self._fail_streak = 0
        self._task = None
        self._alone_task = None
        self._prefetch_task = None
        self._panel_lock = asyncio.Lock()

    # --- state helpers ---
    @property
    def voice_client(self):
        return self.guild.voice_client

    def is_paused(self):
        vc = self.voice_client
        return bool(vc and vc.is_paused())

    def humans_in_channel(self):
        vc = self.voice_client
        if not vc or not vc.channel:
            return []
        return [m for m in vc.channel.members if not m.bot]

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def wake(self):
        self._wake.set()

    async def say(self, text):
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(text)
        except discord.HTTPException:
            pass

    # --- controls ---
    def pause(self):
        vc = self.voice_client
        if vc and vc.is_playing():
            vc.pause()
            return True
        return False

    def resume(self):
        vc = self.voice_client
        if vc and vc.is_paused():
            vc.resume()
            return True
        return False

    def skip(self):
        if self.current is None:
            return False
        self._skip = True  # also covers "skipped while still loading"
        vc = self.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # ends the track; the loop moves on
        return True

    def stop(self):
        had_something = self.current is not None or bool(self.queue)
        self.queue.clear()
        self.loop_mode = "off"
        self._skip = True
        vc = self.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        return had_something

    def cycle_loop(self):
        order = ["off", "track", "queue"]
        self.loop_mode = order[(order.index(self.loop_mode) + 1) % len(order)]
        return self.loop_mode

    def shuffle(self):
        items = list(self.queue)
        random.shuffle(items)
        self.queue.clear()
        self.queue.extend(items)

    # --- the panel ("emblem") ---
    def build_embed(self):
        cur = self.current
        if cur:
            state = "⏸️ Paused" if self.is_paused() else "🎶 Now playing"
            name = md_safe(shorten(cur.title, 90))
            headline = f"[{name}]({cur.link.replace(')', '%29')})" if cur.link else name
            embed = discord.Embed(
                title=state,
                description=(
                    f"**{headline}**\n`{fmt_duration(cur.duration)}` · {cur.origin} · "
                    f"requested by **{md_safe(cur.requester)}**"
                ),
                color=POTATO_COLOR,
            )
            if cur.thumbnail and str(cur.thumbnail).startswith("http"):
                embed.set_thumbnail(url=cur.thumbnail)
        else:
            embed = discord.Embed(
                title="🎶 Music player",
                description="Nothing is playing. Use `/play <link or search>` to add something!",
                color=POTATO_COLOR,
            )

        queued = list(self.queue)
        if queued:
            lines = [
                f"`{i}.` {md_safe(shorten(t.title, 55))} `{fmt_duration(t.duration)}`"
                for i, t in enumerate(queued[:PANEL_QUEUE_LINES], 1)
            ]
            if len(queued) > PANEL_QUEUE_LINES:
                lines.append(f"…and **{len(queued) - PANEL_QUEUE_LINES}** more")
            embed.add_field(name=f"📜 Up next ({len(queued)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📜 Up next", value="*The queue is empty.*", inline=False)

        vc = self.voice_client
        where = f"  •  🔊 {vc.channel.name}" if vc and vc.channel else ""
        embed.set_footer(text=f"Loop: {self.loop_mode}{where}")
        return embed

    async def refresh_panel(self, *, resend=False):
        """Edit the panel in place (or post it / move it to the current channel). True if it worked."""
        async with self._panel_lock:
            if self.destroyed or self.text_channel is None:
                return False
            if self.view is None:
                self.view = PlayerView(self)
            self.view.sync()
            embed = self.build_embed()
            if self.panel_message is not None and not resend:
                try:
                    await self.panel_message.edit(embed=embed, view=self.view)
                    return True
                except discord.NotFound:
                    self.panel_message = None  # someone deleted it: post a new one below
                except discord.HTTPException as e:
                    log_error(f"panel edit failed: {e}")
                    return False
            if self.panel_message is not None:  # resend: remove the old panel first
                try:
                    await self.panel_message.delete()
                except discord.HTTPException:
                    pass
                self.panel_message = None
            try:
                self.panel_message = await self.text_channel.send(embed=embed, view=self.view)
                return True
            except discord.HTTPException as e:
                log_error(f"panel send failed: {e}")
                return False

    # --- background work ---
    async def _prefetch(self):
        """Look up the next track's stream while the current one plays, so there's no gap."""
        try:
            nxt = self.queue[0] if self.queue else None
            if nxt is not None and not nxt.stream_fresh:
                await asyncio.wait_for(nxt.ensure_stream(), timeout=RESOLVE_TIMEOUT)
        except Exception:
            pass  # real errors surface when the track is actually played

    async def _run(self):
        try:
            while not self.destroyed:
                self._wake.clear()
                if not self.queue:
                    self.current = None
                    await self.refresh_panel()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        await self.say("👋 Nothing has played for a while, so I left the voice channel.")
                        await self.destroy()
                        return
                    continue

                track = self.queue.popleft()
                self.current = track
                self._skip = False

                vc = self.voice_client
                if vc is None or not vc.is_connected():
                    await self.destroy()
                    return

                # 1) find the audio stream
                try:
                    await asyncio.wait_for(track.ensure_stream(), timeout=RESOLVE_TIMEOUT)
                except Exception as e:
                    log_error(f"resolve '{shorten(track.title, 40)}': {type(e).__name__} {str(e)[:150]}")
                    await self.say(f"⚠️ Skipping **{md_safe(shorten(track.title, 70))}** — {explain_error(e)}")
                    await self._count_failure()
                    continue
                if self._skip or self.destroyed:
                    continue

                # 2) play it
                try:
                    source = discord.FFmpegOpusAudio(
                        track.stream_url,
                        executable=FFMPEG_EXE,
                        before_options=FFMPEG_BEFORE,
                        options=FFMPEG_OPTIONS,
                    )
                except Exception as e:
                    log_error(f"ffmpeg start failed: {type(e).__name__} {str(e)[:150]}")
                    await self.say("⚠️ I couldn't start ffmpeg, so I can't play audio (see `!logs`).")
                    self.queue.clear()
                    continue

                finished = asyncio.Event()
                loop = asyncio.get_running_loop()

                def after_playback(err, _loop=loop, _done=finished):
                    if err:
                        log_error(f"playback error: {err!r}"[:200])
                    _loop.call_soon_threadsafe(_done.set)  # runs in the audio thread

                try:
                    vc.play(source, after=after_playback)
                except Exception as e:
                    source.cleanup()
                    log_error(f"vc.play failed: {type(e).__name__} {str(e)[:150]}")
                    await self._count_failure()
                    continue

                started = time.monotonic()
                await self.refresh_panel()
                self._prefetch_task = asyncio.create_task(self._prefetch())
                await finished.wait()
                if self.destroyed:
                    return

                # 3) what next?
                elapsed = time.monotonic() - started
                died_instantly = (
                    elapsed < 3 and not self._skip and (track.duration is None or track.duration > 10)
                )
                if died_instantly:
                    if not track.retried:  # stream URLs can be rejected: try once more with a fresh one
                        track.retried = True
                        track.stream_url = None
                        self.queue.appendleft(track)
                    else:
                        log_error(f"'{shorten(track.title, 40)}' stopped immediately (ffmpeg/stream problem)")
                        await self.say(f"⚠️ **{md_safe(shorten(track.title, 70))}** stopped right away — skipping it.")
                        await self._count_failure()
                    continue
                self._fail_streak = 0
                if self.loop_mode == "track" and not self._skip:
                    self.queue.appendleft(track)
                elif self.loop_mode == "queue":
                    self.queue.append(track)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_error(f"player loop crashed: {type(e).__name__} {str(e)[:150]}")
            await self.destroy()

    async def _count_failure(self):
        """Stop grinding through a big queue when every track fails (e.g. YouTube is blocking us)."""
        self._fail_streak += 1
        if self._fail_streak >= 3 and self.queue:
            self.queue.clear()
            await self.say("⚠️ Too many tracks failed in a row, so I cleared the queue. Try again in a bit.")

    # --- leaving ---
    def start_alone_timer(self):
        if self._alone_task is None or self._alone_task.done():
            self._alone_task = asyncio.create_task(self._alone_countdown())

    def cancel_alone_timer(self):
        if self._alone_task and not self._alone_task.done() and self._alone_task is not asyncio.current_task():
            self._alone_task.cancel()

    async def _alone_countdown(self):
        await asyncio.sleep(ALONE_TIMEOUT)
        if not self.destroyed and not self.humans_in_channel():
            await self.say("👋 Everyone left, so I left too.")
            await self.destroy()

    async def destroy(self, *, disconnect=True):
        if self.destroyed:
            return
        self.destroyed = True
        if players.get(self.guild.id) is self:
            players.pop(self.guild.id, None)
        self.queue.clear()
        self.current = None
        self.loop_mode = "off"
        me = asyncio.current_task()
        for task in (self._task, self._alone_task, self._prefetch_task):
            if task is not None and task is not me and not task.done():
                task.cancel()
        vc = self.voice_client
        if disconnect and vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                log_error(f"disconnect failed: {type(e).__name__} {str(e)[:100]}")
        if self.panel_message is not None:
            ended = discord.Embed(
                title="🎶 Music player",
                description="Session ended. Use `/play` or `/joinvc` to start again.",
                color=POTATO_COLOR,
            )
            try:
                await self.panel_message.edit(embed=ended, view=None)
            except discord.HTTPException:
                pass
        if self.view is not None:
            self.view.stop()


class PlayerView(discord.ui.View):
    """The buttons under the player panel."""

    def __init__(self, player):
        super().__init__(timeout=None)
        self.player = player
        self.sync()

    def sync(self):
        """Make the buttons match the player's state (Pause <-> Resume, Loop mode)."""
        paused = self.player.is_paused()
        self.pause_resume.label = "Resume" if paused else "Pause"
        self.pause_resume.emoji = "▶️" if paused else "⏸️"
        mode = self.player.loop_mode
        self.loop_btn.label = {"off": "Loop", "track": "Loop: track", "queue": "Loop: queue"}[mode]
        self.loop_btn.style = discord.ButtonStyle.secondary if mode == "off" else discord.ButtonStyle.success

    async def interaction_check(self, interaction):
        if interaction.guild is None:  # never act on button presses outside a server
            return False
        problem = control_error(interaction.guild, interaction.user)
        if problem:
            await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction, button):
        p = self.player
        if p.is_paused():
            p.resume()
        elif not p.pause():
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        self.sync()
        await interaction.response.edit_message(embed=p.build_embed(), view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction, button):
        if not self.player.skip():
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)
            return
        await interaction.response.defer()  # the panel refreshes itself when the next track starts

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction, button):
        if not self.player.stop():
            await interaction.response.send_message("Nothing to stop.", ephemeral=True)
            return
        await interaction.response.defer()

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop_btn(self, interaction, button):
        self.player.cycle_loop()
        self.sync()
        await interaction.response.edit_message(embed=self.player.build_embed(), view=self)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_btn(self, interaction, button):
        if len(self.player.queue) < 2:
            await interaction.response.send_message("I need at least 2 queued tracks to shuffle.", ephemeral=True)
            return
        self.player.shuffle()
        await interaction.response.edit_message(embed=self.player.build_embed(), view=self)


# ---------- slash commands (slash responses are already shown by Discord as replies to the user) ----------
@bot.tree.command(name="joinvc", description="Join your voice channel (or the voice channel whose chat you type this in)")
@app_commands.guild_only()
async def joinvc_cmd(interaction: discord.Interaction):
    try:
        target = voice_target_or_raise(interaction)
    except UserFacingError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc and vc.is_connected() and vc.channel.id == target.id:
        await interaction.response.send_message(f"✅ I'm already in **{target.name}**.", ephemeral=True)
        return
    await interaction.response.defer()  # connecting can take longer than Discord's 3-second limit
    try:
        await connect_player(interaction, target)
    except UserFacingError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    await interaction.followup.send(f"🔊 Joined **{target.name}** — use `/play <link or search>` to start the music!")


@bot.tree.command(name="play", description="Play a YouTube / Spotify / SoundCloud link, or search by name")
@app_commands.describe(query="A link (YouTube, Spotify, SoundCloud...) or words to search for")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 3.0)
async def play_cmd(interaction: discord.Interaction, query: str):
    try:
        target = voice_target_or_raise(interaction)
    except UserFacingError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    try:
        tracks, collection = await resolve_query(query, interaction.user.display_name)
        player = await connect_player(interaction, target)
    except UserFacingError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    except Exception as e:
        log_error(f"/play failed: {type(e).__name__} {str(e)[:150]}")
        await interaction.followup.send("❌ Something went wrong while loading that. Try again in a moment.")
        return

    room = MAX_QUEUE - len(player.queue)
    if room <= 0:
        await interaction.followup.send(f"❌ The queue is full ({MAX_QUEUE} tracks).")
        return
    added = tracks[:room]
    was_idle = player.current is None and not player.queue
    player.queue.extend(added)
    player.text_channel = interaction.channel
    player.wake()

    if len(added) == 1:
        t = added[0]
        name = md_safe(shorten(t.title, 80))
        if was_idle:
            msg = f"▶️ Starting **{name}** `{fmt_duration(t.duration)}`"
        else:
            msg = f"✅ Queued **{name}** `{fmt_duration(t.duration)}` — #{len(player.queue)} in line"
    else:
        label = md_safe(shorten(collection, 60)) if collection else "that link"
        msg = f"✅ Queued **{len(added)} tracks** from **{label}**"
        if len(added) < len(tracks):
            msg += f" (the queue limit cut it at {len(added)} of {len(tracks)})"
    await interaction.followup.send(msg)
    await player.refresh_panel()


@bot.tree.command(name="queue", description="Show the music panel: now playing, queue and controls")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if player is None:
        await interaction.response.send_message("📭 Nothing is queued. Use `/play <link or search>` to start!", ephemeral=True)
        return
    player.text_channel = interaction.channel
    await interaction.response.send_message("📜 Here's the player panel:", ephemeral=True)
    if not await player.refresh_panel(resend=True):
        await interaction.followup.send(
            "❌ I can't post in this channel (I need **Send Messages** and **Embed Links**).", ephemeral=True
        )


@bot.tree.command(name="skip", description="Skip the current track")
@app_commands.guild_only()
async def skip_cmd(interaction: discord.Interaction):
    player = await get_controllable_player(interaction)
    if player is None:
        return
    title = player.current.title if player.current else None
    if not player.skip():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"⏭️ **{md_safe(interaction.user.display_name)}** skipped **{md_safe(shorten(title, 70))}**"
    )


@bot.tree.command(name="pause", description="Pause the music")
@app_commands.guild_only()
async def pause_cmd(interaction: discord.Interaction):
    player = await get_controllable_player(interaction)
    if player is None:
        return
    if not player.pause():
        await interaction.response.send_message("Nothing is playing (or it's already paused).", ephemeral=True)
        return
    await interaction.response.send_message(f"⏸️ Paused by **{md_safe(interaction.user.display_name)}**")
    await player.refresh_panel()


@bot.tree.command(name="resume", description="Resume the music")
@app_commands.guild_only()
async def resume_cmd(interaction: discord.Interaction):
    player = await get_controllable_player(interaction)
    if player is None:
        return
    if not player.resume():
        await interaction.response.send_message("The music isn't paused.", ephemeral=True)
        return
    await interaction.response.send_message(f"▶️ Resumed by **{md_safe(interaction.user.display_name)}**")
    await player.refresh_panel()


@bot.tree.command(name="stop", description="Stop the music and clear the queue")
@app_commands.guild_only()
async def stop_cmd(interaction: discord.Interaction):
    player = await get_controllable_player(interaction)
    if player is None:
        return
    if not player.stop():
        await interaction.response.send_message("Nothing to stop.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"⏹️ **{md_safe(interaction.user.display_name)}** stopped the music and cleared the queue."
    )


@bot.tree.command(name="leave", description="Disconnect the bot from the voice channel")
@app_commands.guild_only()
async def leave_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None:
        await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
        return
    problem = control_error(interaction.guild, interaction.user)
    if problem:
        await interaction.response.send_message(f"❌ {problem}", ephemeral=True)
        return
    await interaction.response.send_message("👋 Left the voice channel.")
    player = players.get(interaction.guild.id)
    if player is not None:
        await player.destroy()
    else:
        await vc.disconnect(force=True)


# --- EVENTS ---
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await reply(ctx, f"⏳ The potato needs a moment. Try again in {error.retry_after:.0f}s.")
    elif isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        return  # unknown command, non-admin, or a DM: stay silent
    elif isinstance(error, commands.MissingRequiredArgument):
        if ctx.command.name == "chat":
            await reply(ctx, "Say something to the potato: `!chat hello`")
        else:
            await reply(ctx, f"Usage: `!{ctx.command.name} <{error.param.name}>`")
    elif isinstance(error, commands.BadArgument):
        await reply(ctx, "That value doesn't look right. Check the command and try again.")
    else:
        log_error(f"{ctx.command}: {error!r}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.NoPrivateMessage):
        msg = "🚫 This bot only works in servers, not in DMs."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ Slow down! Try again in {error.retry_after:.0f}s."
    else:
        name = interaction.command.name if interaction.command else "?"
        log_error(f"/{name}: {error!r}"[:200])
        msg = "⚠️ Something went wrong. Try again in a moment."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_voice_state_update(member, before, after):
    """Tidy up if the bot is kicked, and leave when everyone else has left."""
    player = players.get(member.guild.id)
    if player is None:
        return
    if bot.user and member.id == bot.user.id:
        if after.channel is None:  # disconnected (kicked, channel deleted...)
            await player.destroy(disconnect=False)
        return
    vc = member.guild.voice_client
    if not vc or not vc.channel or vc.channel not in (before.channel, after.channel):
        return
    if player.humans_in_channel():
        player.cancel_alone_timer()
    else:
        player.start_alone_timer()


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


# --- EXECUTION ---
if __name__ == "__main__":
    # Keep-alive server first, so Render sees an open port immediately.
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    if not DISCORD_TOKEN:
        print("FATAL ERROR: DISCORD_TOKEN is not set.")
        raise SystemExit(1)

    if yt_dlp is None:
        print("⚠️ yt-dlp isn't installed: music commands are disabled (pip install -r requirements.txt)")
    elif not ffmpeg_available():
        print("⚠️ ffmpeg wasn't found: music can't play until it's installed (see requirements.txt / Dockerfile)")

    if not os.path.exists(COOKIE_PATH):
        print("⚠️ No YouTube cookies found (set YOUTUBE_COOKIES): YouTube will probably block this host.")
    if spotipy is None:
        print("ℹ️ spotipy isn't installed: Spotify links use the embed-page fallback (pip install spotipy)")
    elif not _spotipy_configured():
        print("ℹ️ SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET not set: Spotify links use the embed-page fallback")

    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
