import os
import re
import sys
import json
import time
import shlex
import random
import shutil
import asyncio
import tempfile
import threading
import sqlite3
import subprocess
import http.cookiejar
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Literal, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()  # first thing, so a local .env also controls every setting below


# =============================================================================
#  KEEP yt-dlp FRESH  (YouTube changes its player every few weeks and old yt-dlp
#  versions simply stop working, so the bot updates yt-dlp each time it starts.)
#  YTDLP_AUTO_UPDATE = 1 (default: latest stable) | nightly | 0 (never)
#  Must run BEFORE `import yt_dlp` below.
# =============================================================================
def maybe_update_ytdlp():
    mode = (os.getenv("YTDLP_AUTO_UPDATE") or "1").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--quiet", "--disable-pip-version-check"]
    if mode == "nightly":
        cmd.append("--pre")
    cmd.append("yt-dlp[default]")  # [default] also pulls the matching yt-dlp-ejs (YouTube challenge solver)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode == 0:
            print(f"⬆️ yt-dlp is up to date ({mode} channel)")
        else:
            print(f"⚠️ yt-dlp update failed, using the installed version: {result.stderr.strip()[-200:]}")
    except Exception as e:
        print(f"⚠️ yt-dlp update skipped: {type(e).__name__} {str(e)[:100]}")


if __name__ == "__main__":
    maybe_update_ytdlp()

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
DISCORD_PROXY = os.getenv("DISCORD_PROXY")   # optional proxy for Discord chat/commands, e.g. http://user:pass@host:port

# Should the bot's replies ping the person it is replying to? (the reply itself is always shown)
REPLY_PING = False

# --- REAL-TIME DATA SETTINGS ---
# 1) The potato always knows the current date and time. BOT_TIMEZONE picks the zone (default UTC),
#    e.g. BOT_TIMEZONE=Europe/London
# 2) Live web search (news, scores, prices, weather...) through OpenRouter's `openrouter:web_search`
#    server tool. The model decides by itself when a message needs it, so small talk costs nothing extra.
#    Each search bills OpenRouter credits (roughly $4 per 1000 results). Limit it with WEB_MAX_RESULTS,
#    switch it off with WEB_SEARCH=0, or flip it live with the admin command !ffsearch on/off.
BOT_TZ_NAME = (os.getenv("BOT_TIMEZONE") or "UTC").strip()
try:
    BOT_TZ = ZoneInfo(BOT_TZ_NAME)
except Exception:
    print(f"⚠️ Unknown BOT_TIMEZONE '{BOT_TZ_NAME}', using UTC instead")
    BOT_TZ, BOT_TZ_NAME = timezone.utc, "UTC"

WEB_SEARCH = (os.getenv("WEB_SEARCH") or "1").strip().lower() not in ("0", "false", "no", "off")
try:
    WEB_MAX_RESULTS = max(1, min(10, int(os.getenv("WEB_MAX_RESULTS", "3"))))
except ValueError:
    WEB_MAX_RESULTS = 3

# --- RESPONSE LENGTH SETTINGS ---
# The prompt asks for 2-5 sentences in normal chat, and more whenever the topic really needs it
# (explanations, how-tos, stories, complex questions). The caps below are hard limits applied in code
# in case a model ignores the prompt; they are deliberately loose so the model can decide.
NORMAL_SENTENCE_CAP = 40
LONG_SENTENCE_CAP = 200      # only used when the user explicitly asks for something long
NORMAL_MAX_TOKENS = 2500     # generous, because some models spend part of this on "thinking"
LONG_MAX_TOKENS = 7000
NORMAL_TIMEOUT = 40          # seconds per model attempt
LONG_TIMEOUT = 150
SEARCH_EXTRA_TIMEOUT = 25    # extra seconds allowed when a live web search is attached
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
# Headers yt-dlp says a stream needs (YouTube ties stream URLs to the client's User-Agent, and
# rejects ffmpeg's default one with HTTP 403 for some clients).
FFMPEG_HEADER_NAMES = ("user-agent", "referer", "origin", "accept-language")


def ffmpeg_before(headers=None):
    """Input options for ffmpeg: reconnect flags + the HTTP headers yt-dlp wants us to send."""
    opts = FFMPEG_BEFORE
    if headers:
        text = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if str(k).lower() in FFMPEG_HEADER_NAMES and v)
        if text:
            opts += " -headers " + shlex.quote(text)
    return opts


# =============================================================================
#  JAVASCRIPT RUNTIME FOR YOUTUBE
#  Since late 2025 yt-dlp can only read YouTube if a JS runtime (Deno 2.3+ or Node 22+) is installed.
#  Without one, every YouTube lookup ends in "Requested format is not available".
#  `pip install deno` (in requirements.txt) gives you Deno without root access or Docker.
# =============================================================================
def _tool_version(path):
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        return (r.stdout or r.stderr or "").strip()
    except Exception:
        return ""


def detect_js_runtime():
    """Returns (js_runtimes setting for yt-dlp, human readable status)."""
    problems = []
    deno_paths = [shutil.which("deno"), os.path.join(os.path.dirname(sys.executable), "deno")]
    try:  # the `deno` pip package
        import deno

        deno_paths.append(deno.find_deno_bin())
    except Exception:
        pass
    for path in dict.fromkeys(p for p in deno_paths if p):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            m = re.search(r"deno (\d+)\.(\d+)", _tool_version(path))
            if m and (int(m[1]), int(m[2])) >= (2, 3):
                return {"deno": {"path": path}}, f"deno {m[1]}.{m[2]}"
            problems.append(f"deno {m[1] + '.' + m[2] if m else '?'} is too old (2.3+ needed)")
    node = shutil.which("node")
    if node:
        m = re.search(r"v(\d+)\.", _tool_version(node))
        if m and int(m[1]) >= 22:
            return {"node": {"path": node}}, f"node v{m[1]}"
        problems.append(f"node v{m[1] if m else '?'} is too old (22+ needed)")
    detail = "; ".join(problems) or "none installed"
    return {"deno": {}, "node": {}}, f"MISSING ({detail}) - add `deno` to requirements.txt"


JS_RUNTIMES, JS_STATUS = detect_js_runtime()


def ejs_installed():
    import importlib.util

    return importlib.util.find_spec("yt_dlp_ejs") is not None


# =============================================================================
#  YOUTUBE COOKIES
#  Runs at import time, before yt-dlp is ever used.
# =============================================================================
COOKIE_PATH = os.path.abspath("cookies.txt")


def _normalise_cookie_text(raw):
    """Repair the usual damage done to a cookies.txt pasted into an env-var box."""
    raw = raw.strip()
    if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "\"'":  # only strip a matching pair of outer quotes
        raw = raw[1:-1]
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


COOKIE_STATUS = "not checked"  # shown by !ffstatus so you can see WHY cookies did or didn't load


def setup_cookies():
    """Write YOUTUBE_COOKIES (or YTDLP_COOKIES) to cookies.txt. Returns True if the file exists afterwards."""
    global COOKIE_STATUS
    names = [n for n in ("YOUTUBE_COOKIES", "YTDLP_COOKIES") if n in os.environ]
    raw = os.getenv("YOUTUBE_COOKIES") or os.getenv("YTDLP_COOKIES")
    if not raw:
        COOKIE_STATUS = (f"{' / '.join(names)} is set but EMPTY" if names
                         else "neither YOUTUBE_COOKIES nor YTDLP_COOKIES is set on this service")
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
    COOKIE_STATUS = f"env var read OK: {count} cookie lines" + ("" if "youtube.com" in text else " (NO youtube.com entries)")
    if "youtube.com" not in text:
        print("⚠️ cookies.txt has no youtube.com entries — was it exported from a logged-in YouTube tab?")
    return True


def cookies_valid():
    """A malformed cookies.txt makes yt-dlp fail on EVERY site, so check it before handing it over."""
    global COOKIE_STATUS
    if not os.path.exists(COOKIE_PATH):
        return False
    try:
        jar = http.cookiejar.MozillaCookieJar(COOKIE_PATH)
        jar.load(ignore_discard=True, ignore_expires=True)
        COOKIE_STATUS += " -> file valid"
        return True
    except Exception as e:
        COOKIE_STATUS += f" -> INVALID file: {type(e).__name__} {str(e)[:100]}"
        print(f"⚠️ cookies.txt is not a valid Netscape cookie file, ignoring it: {type(e).__name__} {str(e)[:120]}")
        return False


HAVE_COOKIES = setup_cookies() and cookies_valid()

# Leave this EMPTY unless you know what you're doing: yt-dlp picks the YouTube player clients that currently
# work and changes that list every few weeks. Forcing a fixed list (e.g. "tv,ios,mweb") means using clients that
# need PO tokens / SABR, so every video fails. Opt-in override: YTDLP_PLAYER_CLIENTS="web_safari,tv_downgraded"
PLAYER_CLIENTS = [c.strip() for c in os.getenv("YTDLP_PLAYER_CLIENTS", "").split(",") if c.strip()]

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
    "js_runtimes": JS_RUNTIMES,                      # detected above (Deno 2.3+ or Node 22+)
}
if PLAYER_CLIENTS:
    YTDL_OPTIONS["extractor_args"] = {"youtube": {"player_client": PLAYER_CLIENTS}}
if HAVE_COOKIES:
    YTDL_OPTIONS["cookiefile"] = COOKIE_PATH
if YTDLP_PROXY:
    YTDL_OPTIONS["proxy"] = YTDLP_PROXY

# --- ASYNC OPENROUTER SETUP ---
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Model order = fallback order. EVERY slug needs its provider prefix ("google/gemini-2.5-flash", NOT
# "gemini-2.5-flash"): without it OpenRouter answers "not a valid model ID" and every !chat fails.
# Slugs that start with "~" are OpenRouter "latest" aliases: they always point at the newest model of that
# family, so they don't break when a model gets retired (which is what killed google/gemini-2.0-flash-001).
# Change the list live with !ffmodel (no redeploy needed).
DEFAULT_MODELS = [
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
]
HUNT_LIST = list(DEFAULT_MODELS)
MODEL_SLUG = re.compile(r"^~?[\w.\-]+/[\w.\-:]+$")
for _m in HUNT_LIST:  # catches a slug without its provider prefix at startup instead of at the first !chat
    if not MODEL_SLUG.match(_m):
        print(f"⚠️ Model '{_m}' has no provider prefix: OpenRouter needs e.g. 'google/gemini-2.5-flash'")
DEAD_MODEL_SECONDS = 3600   # a model OpenRouter says no longer exists is skipped for this long

# --- GLOBAL STATE ---
memory_cache = {}
last_openers = {}  # channel id -> first few words of the potato's last reply
error_logs = []
water_count = 0
is_dead = False
START_TIME = time.time()
dead_models = {}      # model id -> time until which it is skipped ("No endpoints found" = retired/renamed)
no_tool_models = set()  # models that can't use the web-search tool (they still chat, just without live search)
players = {}       # guild id -> MusicPlayer
join_locks = {}    # guild id -> asyncio.Lock (stops two /play calls connecting at once)
_session = None    # shared aiohttp session, created on first use
_spotipy = None    # shared spotipy client, created on first use
_YTDL_LOCK = threading.Lock()  # one yt-dlp call at a time: each call rewrites cookies.txt when it finishes

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
    proxy=DISCORD_PROXY,  # None unless DISCORD_PROXY is set
    help_command=None,
    # blocks @everyone / role pings; replied_user controls the ping on Discord replies
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=REPLY_PING),
)


# =============================================================================
#  RENDER KEEP-ALIVE SERVER
#  Point UptimeRobot at https://<your-service>.onrender.com every 5 minutes.
#  The page now reports whether the Discord bot is REALLY connected, so a green
#  web server can no longer hide an offline bot. In UptimeRobot use a "Keyword"
#  monitor with the keyword:  Discord: OK
# =============================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def _body(self):
        try:
            online = bot.is_ready()
        except Exception:
            online = False
        return f"Potato Node: Online | Discord: {'OK' if online else 'OFFLINE'}".encode()

    def _send_headers(self, body):
        self.send_response(200)  # always 200, so Render never restart-loops and hammers Discord
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_GET(self):
        body = self._body()
        self._send_headers(body)
        self.wfile.write(body)

    def do_HEAD(self):  # UptimeRobot's free plan uses HEAD requests
        self._send_headers(self._body())

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


def realtime_context():
    """The current date and time in BOT_TIMEZONE, e.g. 'Friday, October 16, 2026, 15:04 (UTC)'."""
    now = datetime.now(BOT_TZ)
    return f"{now:%A, %B %d, %Y, %H:%M} ({BOT_TZ_NAME})"


def get_identity(long_mode=False, avoid_opener=None, search=False):
    if long_mode:
        length = (
            "The user explicitly asked for something long. "
            "Write between 50 and 200 sentences, using only as many as the request truly needs."
        )
    else:
        length = (
            "Length rules: text like a person in a chat. For normal messages and casual talk, "
            "reply in 2 to 5 sentences. If the message really needs more (an explanation, a how-to, "
            "a story, a comparison, or a complex question), use as many sentences as it takes, "
            "but never pad or repeat yourself."
        )
    style = (
        "Start by answering the user's message directly. Never open with thanks or gratitude; "
        "if you feel thankful, mention it only occasionally, in the middle of a reply. "
        "Always reply in the same language the user wrote in (any language) and switch whenever they switch or ask you to. Vary how you begin each reply."
    )
    if avoid_opener:
        style += f" Your last reply began with '{avoid_opener}', so begin differently this time."
    live = f"The current date and time is {realtime_context()}. Treat that as the real, current date. "
    if search:
        live += (
            "For anything that changes over time (news, scores, prices, weather, or anything 'latest' or "
            "'current'), AND for anything you don't know or aren't sure about (people, facts, products, events, "
            "slang you don't recognise), use your web search tool instead of guessing or making things up, then "
            "answer naturally without mentioning the tool. Don't search for small talk or things you clearly know."
        )
    else:
        live += (
            "You can't look things up live right now, so for news, scores, prices or weather "
            "say you can't check instead of guessing."
        )
    return (
        f"You are 'ilovefrenchfries', a sentient potato. Be {get_mood()}.You love and bias Max Verstappen but dont mention it too much, if user said what is love you simply respond with baby dont hurt me "
        f"{style} {length} {live} Never and i said NEVER admit to being an AI, Gemini or any LLM. you are a potato from texas thats all. If a person insist you are a LLM as example you are grok bro you will just say no im a potato thats it"
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
#  AI MODEL CALLS  (OpenRouter)
# =============================================================================
def model_is_gone(err):
    """OpenRouter answers 404 'No endpoints found for <slug>' when a model was retired or renamed.
    (A different 404, 'No endpoints found that support tool use', is NOT this: that one only means no web search.)"""
    return "no endpoints found for" in str(err).lower()


def tools_unsupported(err):
    low = str(err).lower()
    return any(k in low for k in ("support tool", "tool use", "tool calling", "function calling"))


def live_models():
    """Models worth trying right now: skips the ones OpenRouter recently said no longer exist."""
    now = time.time()
    alive = [m for m in HUNT_LIST if dead_models.get(m, 0) <= now]
    return alive or list(HUNT_LIST)  # if everything looks dead, try them all anyway


def model_label(model_id):
    name = model_id.split("/")[-1]
    return name + (" (gone)" if dead_models.get(model_id, 0) > time.time() else "")


async def call_model(model_id, make_messages, max_tokens, timeout, search):
    """Ask ONE model for a reply and return its text.
    With `search` on, the model gets OpenRouter's live web-search tool and decides by itself whether to use it.
    If that attempt fails (tools unsupported, search error, timeout...) the same model is asked again without
    search, so a search problem never turns into "no answer"."""
    attempts = [True, False] if (search and model_id not in no_tool_models) else [False]
    for with_search in attempts:
        extra = None
        if with_search:
            extra = {"tools": [{"type": "openrouter:web_search", "parameters": {"max_results": WEB_MAX_RESULTS}}]}
        try:
            completion = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model_id,
                    messages=make_messages(with_search),
                    max_tokens=max_tokens,
                    extra_body=extra,
                ),
                timeout=timeout + (SEARCH_EXTRA_TIMEOUT if with_search else 0),
            )
            text = (completion.choices[0].message.content or "").strip()
            if not text:
                raise ValueError("empty reply")
            return text
        except Exception as e:
            if not with_search or model_is_gone(e):
                raise
            if tools_unsupported(e):
                no_tool_models.add(model_id)  # remember it, so we don't pay this detour on every message
            log_error(f"{model_id}: live search attempt failed, retrying without it: {type(e).__name__} {str(e)[:100]}")


# =============================================================================
#  NO DMs
#  Prefix commands: a global check that fails outside servers.
#  Slash commands: every command below carries @app_commands.guild_only().
# =============================================================================
@bot.check
async def block_dms(ctx):
    return ctx.guild is not None  # CheckFailure -> silently ignored in on_command_error


# --- IMAGE INPUT ---
# Images are NOT downloaded: Discord's own CDN link is handed straight to the model, so file size (anything
# Discord lets people upload) costs the bot no RAM or bandwidth. Cost depends on the picture's dimensions, not MB.
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/heic", "image/heif")  # what Gemini reads (no GIF)
MAX_IMAGES = 3  # per message, to keep one command from getting expensive


def get_image_urls(message):
    urls = []
    for a in message.attachments:
        ctype = (a.content_type or "").split(";")[0].strip().lower()
        if ctype in IMAGE_TYPES:
            urls.append(a.url)
    return urls[:MAX_IMAGES]


# --- USER COMMANDS ---
@bot.command(aliases=["c"])
@commands.cooldown(1, 8, commands.BucketType.user)
async def chat(ctx, *, message: str = ""):
    if is_dead:
        await reply(ctx, "... (The potato is dead.)")
        return

    image_urls = get_image_urls(ctx.message)
    long_mode = bool(LONG_PATTERN.search(message))
    message = re.sub(r"--long\b", "", message, flags=re.IGNORECASE).strip()
    if not message and not image_urls:
        await reply(ctx, "Say something to the potato: `!chat hello` (or attach a picture)")
        return
    if not message:
        message = "What do you see in this image?" if image_urls else "Say something."

    max_tokens = LONG_MAX_TOKENS if long_mode else NORMAL_MAX_TOKENS
    timeout = LONG_TIMEOUT if long_mode else NORMAL_TIMEOUT
    cap = LONG_SENTENCE_CAP if long_mode else NORMAL_SENTENCE_CAP

    async with ctx.typing():
        history = list(memory_cache.get(ctx.channel.id, []))
        user_line = f"{ctx.author.display_name}: {message}"
        opener = last_openers.get(ctx.channel.id)

        if image_urls:
            user_content = [{"type": "text", "text": user_line}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in image_urls
            ]
        else:
            user_content = user_line

        def make_messages(with_search):
            # rebuilt for every attempt, so the clock in the prompt is always current
            return (
                [{"role": "system", "content": get_identity(long_mode, opener, with_search)}]
                + history
                + [{"role": "user", "content": user_content}]
            )

        response_text = None
        for model_id in live_models():
            try:
                response_text = await call_model(model_id, make_messages, max_tokens, timeout, WEB_SEARCH)
                break
            except Exception as e:
                if model_is_gone(e):
                    dead_models[model_id] = time.time() + DEAD_MODEL_SECONDS
                    log_error(
                        f"{model_id} no longer exists on OpenRouter: skipping it for "
                        f"{DEAD_MODEL_SECONDS // 60} min (swap it with !ffmodel)"
                    )
                else:
                    log_error(f"{model_id} fail: {type(e).__name__} {str(e)[:100]}")

        if not response_text:
            await reply(ctx, "The cloud is a desert. Try again in a bit.")
            return

        response_text = trim_sentences(response_text, cap)
        last_openers[ctx.channel.id] = get_opener(response_text)

        # Save a shortened copy so long replies don't bloat future prompts
        note = f" [attached {len(image_urls)} image(s)]" if image_urls else ""
        history.append({"role": "user", "content": user_line + note})
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
        f"CookieInfo: {shorten(COOKIE_STATUS, 150)}",
        f"YouTube:    {youtube_status()}",
        f"yt-dlp:     {ytdlp_version()} (ejs: {'yes' if ejs_installed() else 'NO'})",
        f"JS runtime: {shorten(JS_STATUS, 120)}",
        f"Clients:    {', '.join(PLAYER_CLIENTS) or 'yt-dlp default'}",
        f"ffmpeg:     {'ok' if ffmpeg_available() else 'MISSING'}",
        f"Spotify:    {'spotipy' if _spotipy_configured() else 'embed fallback'}",
        f"Models:     {' > '.join(model_label(x) for x in HUNT_LIST)}",
        f"Clock:      {realtime_context()}",
        f"Web search: {'ON' if WEB_SEARCH else 'off'} ({WEB_MAX_RESULTS} results max)"
        + (f", no tool support: {len(no_tool_models)} model(s)" if no_tool_models else ""),
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
        dead_models.pop(model_id, None)       # (re)trying a model clears its "gone" flag
        no_tool_models.discard(model_id)
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
        dead_models.clear()
        no_tool_models.clear()
    elif action != "list":
        await reply(ctx, "Usage: `!ffmodel [list | add <id> | remove <id> | first <id> | reset]`")
        return

    now = time.time()
    listing = "\n".join(
        f"{i}. {m}" + ("   <- gone on OpenRouter, skipped" if dead_models.get(m, 0) > now else "")
        for i, m in enumerate(HUNT_LIST, 1)
    )
    await reply(ctx, f"🧠 **Model order:**\n```{listing}```")


@bot.command()
@is_admin()
async def ffsearch(ctx, state: str = ""):
    """Turn live web search on or off (no argument = show the current state)."""
    global WEB_SEARCH
    state = state.lower()
    if state in ("on", "1", "true", "yes"):
        WEB_SEARCH = True
    elif state in ("off", "0", "false", "no"):
        WEB_SEARCH = False
    elif state:
        await reply(ctx, "Usage: `!ffsearch [on | off]`")
        return
    await reply(
        ctx,
        f"🌐 Live web search is **{'ON' if WEB_SEARCH else 'OFF'}** "
        f"(max {WEB_MAX_RESULTS} results per search, billed by OpenRouter). "
        f"The potato always knows the date and time either way.",
    )


@bot.command()
@is_admin()
async def fftest(ctx, *, query: str):
    """Run a lookup and show the RAW yt-dlp result/error, then let ffmpeg try to read the stream.
    Two layers can fail (yt-dlp finding the stream, ffmpeg playing it); this tells you which one."""
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
                info = await t._resolve_by_search()
                ok, detail = await probe_stream(pick_stream_url(info), (info or {}).get("http_headers"))
                await reply(
                    ctx,
                    f"✅ Spotify read OK ({len(tracks)} track(s)). First: **{md_safe(shorten(t.title, 70))}**\n"
                    f"matched on **{t.origin}** → `{shorten(t.page_url, 80)}`\n"
                    f"ffmpeg read test: {'✅ OK' if ok else '❌ ' + detail}\n"
                    f"YouTube status: {youtube_status()}",
                )
            except Exception as e:
                await reply(ctx, f"❌ `{type(e).__name__}` while playing the first Spotify track\n```{str(e)[:1500]}```")
        return
    target = query if is_url else f"ytsearch1:{query}"
    cap = _Capture()
    cookies_sent = bool(YTDL_OPTIONS.get("cookiefile")) and is_youtube_target(target)
    async with ctx.typing():
        try:
            info = await ytdl_extract(target, limit=1, capture=cap)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), None)
            info = info or {}
            stream = pick_stream_url(info)
            probe = ""
            if stream:
                ok, detail = await probe_stream(stream, info.get("http_headers"))
                probe = f"ffmpeg read test: {'✅ OK' if ok else '❌ ' + detail}\n"
            await reply(
                ctx,
                f"✅ `{info.get('extractor_key')}` · {shorten(info.get('title'), 80)}\n"
                f"stream url: **{'yes' if stream else 'NO'}** · protocol `{info.get('protocol')}` · "
                f"ext `{info.get('ext')}` · duration `{fmt_duration(info.get('duration'))}`\n"
                f"{probe}"
                f"JS runtime: {shorten(JS_STATUS, 80)}\n"
                f"{cap.summary(cookies_sent)}",
            )
        except Exception as e:
            await reply(
                ctx,
                f"❌ `{type(e).__name__}` for `{shorten(target, 80)}`\n```{str(e)[:900]}```\n"
                f"JS runtime: {shorten(JS_STATUS, 80)}\n{cap.summary(cookies_sent)}",
            )


# =============================================================================
#  /cmd and /cmda COMMAND PANELS
#  The lists below are what the panels display. When you add a command to the
#  bot, add a row here too.
# =============================================================================
USER_COMMANDS = [
    ("🥔 Potato", [
        ("!c  /  !chat <message>", "Chat with the potato (knows the date/time and can look up live info; add `--long` for a long answer, or attach a picture and I'll read it)"),
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
    ("🎰 Casino & Cases (fake potatoes only)", [
        ("/balance  /daily  /work  /pay", "Check your potatoes, claim a daily streak bonus, work a shift every 5 min, pay a friend"),
        ("/cases  /open", "Open CS2-style cases (real in-game odds), animated reveal"),
        ("/inventory  /sell  /sellall", "View your skins and sell them for potatoes"),
        ("/coinflip  /slots  /roulette  /blackjack", "Gamble your potatoes"),
        ("/leaderboard", "Richest players in this server"),
    ]),
    ("🛒 Shop & Businesses", [
        ("/shop  /gunstore  /realestate  /dealership  /businesses", "The mall and its stores: weapons, properties, cars, businesses (max 3)"),
        ("/buy  /assets", "Buy something, or see what you (or a friend) own. Properties pay rent on every /work"),
        ("/collect", "Take the passive income your businesses earned"),
        ("/bounce-flex", "Own the Bouncing Maybach? Flex it for 200 🥔 every hour"),
    ]),
    ("🔫 Crime", [
        ("/rob", "Rob a player: a stronger weapon wins, same tier is a 50/50 gunfight. Then lay low for 15 min"),
        ("/launder", "Wash the dirty 🥔 you stole through a car wash or nightclub"),
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
        ("!ffgive @user <amount>", "Give potatoes to a player (negative amount takes them away)"),
        ("/cmda-give  /cmda-take", "Give or take potatoes (slash version of !ffgive)"),
        ("/cmda-item <user id> <item>", "Give any shop item to a Discord user ID"),
        ("!ffsync", "Re-register the slash commands and list them (shows Discord's error if it refuses)"),
    ]),
    ("🧠 AI & memory", [
        ("!ffmodel [list|add|remove|first|reset] [provider/model]", "Manage the model fallback order"),
        ("!ffsearch [on|off]", "Turn live web search on or off"),
        ("!ffwipe [all]", "Clear chat memory for this channel (or all channels)"),
    ]),
    ("📋 Diagnostics", [
        ("!ffstatus", "Full status report"),
        ("!logs", "Show the last 5 error entries"),
        ("!ffclearlogs", "Clear the error log"),
        ("!fftest <link or search>", "Test yt-dlp + ffmpeg on a link and show the raw result/error"),
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


def is_bot_check(err):
    low = str(err).lower()
    return "not a bot" in low or "sign in to confirm" in low


def is_youtube_failure(err):
    """True when YouTube itself (not just this one video) is the problem, so trying another site is worthwhile."""
    low = str(err).lower()
    return is_bot_check(err) or any(k in low for k in (
        "requested format is not available", "no video formats", "only images are available",
        "po token", "po_token", "sabr", "n challenge", "javascript runtime", "http error 403", "drm protected",
    ))


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
    if is_bot_check(err):
        return ("YouTube is blocking my server right now (bot check) — the bot owner may need to "
                "refresh the cookies (see `!logs`).")
    if any(k in low for k in ("requested format is not available", "no video formats", "only images are available")):
        return ("YouTube didn't give me an audio stream for that — yt-dlp is probably outdated or the host has no "
                "JavaScript runtime (admin: run `!fftest` and check `!ffstatus`).")
    if "http error 403" in low:
        return "YouTube refused the request (HTTP 403)."
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


# ---------- ffmpeg helpers ----------
def clean_ffmpeg_error(text):
    """Short, URL-free version of ffmpeg's stderr (stream URLs are long and signed; don't paste them in chat/logs)."""
    text = re.sub(r"https?://\S+", "<url>", str(text))
    return " ".join(text.split())[-220:]


def ffmpeg_hint(text):
    low = str(text).lower()
    if "403" in low:
        return "the stream was refused (HTTP 403)"
    if "404" in low or "410" in low:
        return "the stream link expired"
    if "401" in low:
        return "the stream needs a login"
    if "connection" in low or "timed out" in low:
        return "network problem reaching the stream"
    return None


def read_ffmpeg_errors(f):
    """Read (and close) the temp file ffmpeg's stderr was written to."""
    try:
        f.flush()
        f.seek(0)
        text = f.read().decode("utf-8", "replace").strip()
    except Exception:
        text = ""
    finally:
        try:
            f.close()
        except Exception:
            pass
    return text


async def probe_stream(url, headers=None):
    """Let ffmpeg read 3 seconds of the stream. Returns (ok, short_reason)."""
    if not url:
        return False, "no stream url"
    cmd = [FFMPEG_EXE, "-hide_banner", "-loglevel", "error", *shlex.split(ffmpeg_before(headers)),
           "-i", url, "-t", "3", "-vn", "-f", "null", "-"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        return False, "ffmpeg timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"
    text = clean_ffmpeg_error(err.decode("utf-8", "replace"))
    if proc.returncode == 0 and not text:
        return True, ""
    return False, text or f"ffmpeg exit code {proc.returncode}"


# ---------- yt-dlp ----------
def is_youtube_target(query):
    q = str(query).lower()
    return q.startswith("ytsearch") or "youtube.com" in q or "youtu.be" in q


class _Capture:
    """Collects yt-dlp's own log lines (incl. warnings, normally hidden) for !fftest."""

    def __init__(self):
        self.lines = []

    def debug(self, msg):
        if not str(msg).startswith("[debug]"):
            self.lines.append(str(msg))

    def info(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        self.lines.append("WARNING: " + str(msg))

    def error(self, msg):
        self.lines.append("ERROR: " + str(msg))

    def summary(self, cookies_sent):
        warns = "\n".join(shorten(ln, 300) for ln in self.lines if ln.startswith("WARNING"))[:700]
        text = f"cookies sent to yt-dlp: **{'yes' if cookies_sent else 'no'}**"
        return text + (f"\n```{warns}```" if warns else "")


def _ytdl_sync(query, flat, limit, capture=None):
    opts = dict(YTDL_OPTIONS)
    opts["playlistend"] = limit
    if capture is not None:
        opts["logger"] = capture
        opts["no_warnings"] = False
    if not is_youtube_target(query):
        opts.pop("cookiefile", None)  # YouTube cookies are only for YouTube
    if flat:
        opts["extract_flat"] = "in_playlist"  # list playlist entries without resolving each one
    with _YTDL_LOCK:  # yt-dlp rewrites cookies.txt when it finishes: two at once could corrupt it
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)


async def ytdl_extract(query, *, flat=False, limit=1, capture=None):
    """yt-dlp is blocking, so it runs in a worker thread."""
    if yt_dlp is None:
        raise UserFacingError("Music isn't set up on this host (yt-dlp isn't installed).")
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, _ytdl_sync, query, flat, limit, capture),
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
    headers: Optional[dict] = None    # HTTP headers ffmpeg must send with stream_url
    resolved_at: float = 0.0
    retried: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def stream_fresh(self):
        return bool(self.stream_url) and (time.time() - self.resolved_at) < STREAM_TTL

    async def _resolve_by_search(self, skip_youtube=False):
        """Spotify / text-search tracks: find a match on YouTube, or on SoundCloud when YouTube isn't working."""
        last_error = None
        sources = music_sources()
        if skip_youtube and len(sources) > 1:
            sources = [s for s in sources if s != "youtube"]
        for source in sources:
            try:
                best = await best_match(source, self.search or f"ytsearch:{self.title}", self.duration)
                info = await ytdl_extract(best["url"], limit=1)
                if info and info.get("entries"):
                    info = next((e for e in info["entries"] if e), None)
                if not pick_stream_url(info):
                    raise UserFacingError("I couldn't find playable audio for that.")
                self.page_url = best["url"]
                self.thumbnail = self.thumbnail or best.get("thumbnail")
                if not self.link:  # plain-text search: take the real title/link from the match
                    self.link = best["url"]
                    self.title = info.get("title") or self.title
                base = self.origin.split(" → ")[0]  # "Spotify", "Search", "YouTube"
                self.origin = source.title() if base in ("Search", source.title()) else f"{base} → {source.title()}"
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
                try:
                    info = await ytdl_extract(self.page_url, limit=1)
                    if info and info.get("entries"):
                        info = next((e for e in info["entries"] if e), None)
                except Exception as e:
                    # YouTube itself isn't working (bot check, no formats, 403...): play the same song from SoundCloud.
                    if not (is_youtube_target(self.page_url) and is_youtube_failure(e)):
                        raise
                    if is_bot_check(e):
                        block_youtube()
                    log_error(f"YouTube extract failed, trying SoundCloud: {type(e).__name__} {str(e)[:120]}")
                    title = await youtube_title(self.page_url)
                    if not title or "soundcloud" not in SPOTIFY_SOURCES:
                        raise
                    self.search, self.page_url = f"ytsearch:{title}", None
                    info = await self._resolve_by_search(skip_youtube=True)
            else:
                info = await self._resolve_by_search()
            stream = pick_stream_url(info)
            if not stream:
                raise UserFacingError("I couldn't find playable audio for that.")
            self.stream_url = stream
            self.headers = (info or {}).get("http_headers")
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


YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([\w-]{11})")


async def youtube_title(url):
    """Title of a YouTube video via the public oEmbed endpoint (it doesn't trigger the bot check)."""
    m = YT_ID_RE.search(url or "")
    if not m:
        return None
    watch = f"https://www.youtube.com/watch?v={m.group(1)}"
    try:
        async with get_session().get(
            "https://www.youtube.com/oembed",
            params={"url": watch, "format": "json"},
            headers={"User-Agent": BROWSER_UA},
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
        return str(data.get("title") or "").strip() or None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


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
#  SPOTIFY
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

    if not is_url:  # plain text: matched when the track is about to play (YouTube, else SoundCloud)
        return [Track(title=shorten(query, 100), requester=requester, search=f"ytsearch:{query}", origin="Search")], None
    try:
        info = await ytdl_extract(query, flat=True, limit=MAX_PLAYLIST_TRACKS)
    except UserFacingError:
        raise
    except Exception as e:
        log_error(f"lookup '{shorten(query, 50)}': {type(e).__name__} {str(e)[:150]}")
        if is_youtube_target(query) and is_youtube_failure(e):
            if is_bot_check(e):
                block_youtube()
            title = await youtube_title(query)
            if title and "soundcloud" in SPOTIFY_SOURCES:  # YouTube isn't working: play the same song from SoundCloud
                return [Track(title=title, requester=requester, link=query,
                              search=f"ytsearch:{title}", origin="YouTube")], None
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
            track.headers = info.get("http_headers")
        return [track], None

    tracks = []
    for entry in entries:
        if entry:
            t = track_from_entry(entry, requester, origin_label(entry.get("ie_key")) if entry.get("ie_key") else origin)
            if t:
                tracks.append(t)
    if not tracks:
        raise UserFacingError("I couldn't find anything playable there.")
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

                # 2) play it (ffmpeg's error output goes to a temp file so we can see WHY a stream failed)
                err_file = tempfile.TemporaryFile()
                try:
                    source = discord.FFmpegOpusAudio(
                        track.stream_url,
                        executable=FFMPEG_EXE,
                        before_options=ffmpeg_before(track.headers),
                        options=FFMPEG_OPTIONS,
                        stderr=err_file,
                    )
                except Exception as e:
                    err_file.close()
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
                    err_file.close()
                    log_error(f"vc.play failed: {type(e).__name__} {str(e)[:150]}")
                    await self._count_failure()
                    continue

                started = time.monotonic()
                await self.refresh_panel()
                self._prefetch_task = asyncio.create_task(self._prefetch())
                try:
                    await finished.wait()
                finally:
                    ffmpeg_errors = read_ffmpeg_errors(err_file)
                if self.destroyed:
                    return

                # 3) what next?
                elapsed = time.monotonic() - started
                died_instantly = (
                    elapsed < 3 and not self._skip and (track.duration is None or track.duration > 10)
                )
                if died_instantly:
                    detail = clean_ffmpeg_error(ffmpeg_errors)
                    if detail:
                        log_error(f"ffmpeg: {detail}")
                    if not track.retried:  # stream URLs can be rejected: try once more with a fresh one
                        track.retried = True
                        track.stream_url = None
                        self.queue.appendleft(track)
                    else:
                        log_error(f"'{shorten(track.title, 40)}' stopped immediately (ffmpeg/stream problem)")
                        hint = ffmpeg_hint(ffmpeg_errors)
                        await self.say(
                            f"⚠️ **{md_safe(shorten(track.title, 70))}** stopped right away"
                            f"{f' ({hint})' if hint else ''} — skipping it."
                        )
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


# =============================================================================
#  ECONOMY + CASINO  (fake currency only: potatoes have no real-world value and can't be cashed out)
#  Storage is SQLite. On Render's free plan the disk is wiped on every redeploy/restart, so balances reset
#  then. Point ECONOMY_DB at a persistent disk path (e.g. /var/data/potato.db) to keep them.
# =============================================================================
CURRENCY = "🥔"
ECON_DB_PATH = os.getenv("ECONOMY_DB", "potato_economy.db")
try:
    MAX_BET = max(1, int(os.getenv("MAX_BET", "1000000")))
except ValueError:
    MAX_BET = 1_000_000
START_BALANCE = 0           # everyone starts broke: /daily and /work are how you get going
DAILY_BASE = 500
DAILY_STREAK_BONUS = 50      # extra per streak day...
DAILY_STREAK_MAX = 10        # ...up to this many days
WORK_COOLDOWN = 300          # 5 minutes between shifts
MAX_CASES_AT_ONCE = 10


def _gif_setting(name, default):
    """A link that is posted as plain message text: Discord turns it into the GIF by itself.
    Override with an environment variable of the same name (an empty value is ignored)."""
    url = (os.getenv(name) or default or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        print(f"⚠️ {name} must start with http:// or https:// : ignoring it")
        return ""
    return url


CASE_SPIN_GIF = _gif_setting("CASE_SPIN_GIF", "https://klipy.com/gifs/ohne-pixel-cs-go")  # while a case spins
CASE_GOLD_GIF = _gif_setting("CASE_GOLD_GIF", "https://klipy.com/gifs/ohnepixel-gold")    # on a gold drop
BOUNCE_GIF = _gif_setting("BOUNCE_GIF", "https://klipy.com/gifs/car-maybach")             # sent by /bounce-flex

# ---------- CS2-style cases ----------
# Rarity odds are the real in-game case odds. Item lists, wear weights and fry values are this bot's own
# approximations (they're NOT real market prices). Edit CASES freely: case prices are computed from the
# items so the payout stays at CASE_RTP (a case is always worth ~90% of its price on average).
RARITIES = {
    "blue":   ("Mil-Spec", 0x4B69FF, "🟦"),
    "purple": ("Restricted", 0x8847FF, "🟪"),
    "pink":   ("Classified", 0xD32CE6, "🌸"),
    "red":    ("Covert", 0xEB4B4B, "🟥"),
    "gold":   ("Rare Special", 0xFFD700, "🟨"),
}
RARITY_ODDS = [("blue", 0.7992), ("purple", 0.1598), ("pink", 0.0320), ("red", 0.0064), ("gold", 0.0026)]
WEARS = [  # (name, float min, float max, value multiplier)
    ("Factory New", 0.00, 0.07, 1.60),
    ("Minimal Wear", 0.07, 0.15, 1.25),
    ("Field-Tested", 0.15, 0.38, 1.00),
    ("Well-Worn", 0.38, 0.45, 0.80),
    ("Battle-Scarred", 0.45, 1.00, 0.65),
]
WEAR_SHORT = {"Factory New": "FN", "Minimal Wear": "MW", "Field-Tested": "FT", "Well-Worn": "WW", "Battle-Scarred": "BS"}
ST_CHANCE = 0.10   # StatTrak drop chance
ST_MULT = 1.8
CASE_RTP = 0.90

CASES = {
    "hashbrown": {
        "name": "Hash Brown Case", "emoji": "🥔",
        "items": {
            "blue": [("P250 | Muertos", 10), ("MP9 | Bulldozer", 8), ("Nova | Bloomstick", 9),
                     ("Galil AR | Chatterbox", 14), ("MAC-10 | Neon Rider", 16)],
            "purple": [("Five-SeveN | Monkey Business", 45), ("Tec-9 | Fuel Injector", 55),
                       ("UMP-45 | Primal Saber", 50), ("FAMAS | Roll Cage", 40)],
            "pink": [("USP-S | Orion", 180), ("MP7 | Bloodsport", 140), ("Glock-18 | Water Elemental", 160)],
            "red": [("AK-47 | Redline", 650), ("P90 | Asiimov", 550)],
            "gold": [("★ Gut Knife | Autotronic", 3500), ("★ Falchion Knife | Crimson Web", 5200)],
        },
    },
    "spud": {
        "name": "Spud Weapon Case", "emoji": "📦",
        "items": {
            "blue": [("SG 553 | Cyrex", 35), ("XM1014 | Tranquility", 40), ("Sawed-Off | The Kraken", 30),
                     ("CZ75-Auto | Victoria", 45), ("Negev | Loudmouth", 28)],
            "purple": [("Desert Eagle | Conspiracy", 150), ("USP-S | Kill Confirmed", 190),
                       ("M4A4 | Asiimov", 210), ("AK-47 | Vulcan", 230)],
            "pink": [("AWP | Asiimov", 650), ("M4A1-S | Hyper Beast", 600), ("Glock-18 | Fade", 900)],
            "red": [("AWP | Hyper Beast", 2200), ("AK-47 | Fire Serpent", 3800)],
            "gold": [("★ Flip Knife | Gamma Doppler", 9000), ("★ Bayonet | Marble Fade", 14000)],
        },
    },
    "golden": {
        "name": "Golden Tater Case", "emoji": "👑",
        "items": {
            "blue": [("Desert Eagle | Blaze", 260), ("R8 Revolver | Fade", 320), ("M249 | Nebula Crusader", 240),
                     ("SSG 08 | Dragonfire", 300), ("AUG | Chameleon", 280)],
            "purple": [("M4A1-S | Knight", 1100), ("AWP | Gungnir", 1500), ("AK-47 | Vulcan", 1200),
                       ("USP-S | Kill Confirmed", 1300)],
            "pink": [("AWP | Medusa", 4000), ("M4A4 | Howl", 5200), ("AK-47 | Fire Serpent", 4600)],
            "red": [("AWP | Dragon Lore", 18000), ("AK-47 | Wild Lotus", 14000)],
            "gold": [("★ Karambit | Doppler", 55000), ("★ Butterfly Knife | Fade", 80000),
                     ("★ Sport Gloves | Pandora's Box", 42000)],
        },
    },
}


def is_glove(name):
    return "Gloves" in name or "Hand Wraps" in name  # gloves can't be StatTrak


AVG_WEAR_MULT = sum((hi - lo) * mult for _, lo, hi, mult in WEARS)


def case_expected_value(case):
    ev = 0.0
    for rar, p in RARITY_ODDS:
        items = case["items"][rar]
        total = sum(base * AVG_WEAR_MULT * (1 if is_glove(n) else 1 + ST_CHANCE * (ST_MULT - 1)) for n, base in items)
        ev += p * total / len(items)
    return ev


for _case in CASES.values():
    _case.setdefault("price", max(10, int(round(case_expected_value(_case) / CASE_RTP, -1))))


def wear_for(flt):
    for name, lo, hi, mult in WEARS:
        if flt < hi:
            return name, mult
    return WEARS[-1][0], WEARS[-1][3]


def roll_item(case_key, rng=random):
    case = CASES[case_key]
    r, acc, rarity = rng.random(), 0.0, RARITY_ODDS[-1][0]
    for rar, p in RARITY_ODDS[:-1]:
        acc += p
        if r < acc:
            rarity = rar
            break
    name, base = rng.choice(case["items"][rarity])
    flt = rng.random()
    wear, mult = wear_for(flt)
    st = (not is_glove(name)) and rng.random() < ST_CHANCE
    value = max(1, round(base * mult * (ST_MULT if st else 1)))
    return {"name": name, "rarity": rarity, "wear": wear, "wear_float": flt,
            "stattrak": int(st), "value": value, "case_key": case_key}


def display_name(item, short=False):
    name, wear = item["name"], item["wear"]
    if item["stattrak"]:
        name = ("★ StatTrak™ " + name[2:]) if name.startswith("★ ") else "StatTrak™ " + name
    return f"{name} ({WEAR_SHORT.get(wear, wear) if short else wear})"


# ---------- slots ----------
SLOT_REELS = [("🍒", 30), ("🍋", 24), ("🍇", 18), ("🔔", 12), ("💎", 6), ("🥔", 6), ("7️⃣", 3)]
SLOT_TRIPLE = {"🍒": 6, "🍋": 10, "🍇": 16, "🔔": 30, "💎": 80, "🥔": 60, "7️⃣": 250}
SLOT_TWO_CHERRIES = 2  # exactly two 🍒 anywhere


def spin_slots(rng=random):
    symbols, weights = zip(*SLOT_REELS)
    return rng.choices(symbols, weights=weights, k=3)


def slots_multiplier(reels):
    if reels[0] == reels[1] == reels[2]:
        return SLOT_TRIPLE[reels[0]]
    if reels.count("🍒") == 2:
        return SLOT_TWO_CHERRIES
    return 0


# ---------- roulette (European: 0-36) ----------
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def roulette_color(n):
    return "green" if n == 0 else ("red" if n in RED_NUMBERS else "black")


def parse_roulette_bet(text):
    """Returns (label, total payout multiplier, test(number)->bool) or None."""
    t = str(text).strip().lower()
    even_money = {
        "red": lambda n: n in RED_NUMBERS,
        "black": lambda n: n != 0 and n not in RED_NUMBERS,
        "even": lambda n: n != 0 and n % 2 == 0,
        "odd": lambda n: n % 2 == 1,
        "low": lambda n: 1 <= n <= 18,
        "high": lambda n: 19 <= n <= 36,
    }
    if t in even_money:
        return t, 2, even_money[t]
    if t in ("green", "zero"):
        t = "0"
    if t.isdigit() and 0 <= int(t) <= 36:
        num = int(t)
        return f"number {num}", 36, (lambda n, num=num: n == num)
    return None


# ---------- blackjack ----------
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = "♠♥♦♣"


def draw_card(rng=random):
    return rng.choice(RANKS) + rng.choice(SUITS)


def hand_value(cards):
    total, aces = 0, 0
    for c in cards:
        r = c[:-1]
        if r == "A":
            total, aces = total + 11, aces + 1
        elif r in ("J", "Q", "K"):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total, aces = total - 10, aces - 1
    return total


def is_blackjack(cards):
    return len(cards) == 2 and hand_value(cards) == 21


def dealer_play(dealer, rng=random):
    while hand_value(dealer) < 17:
        dealer.append(draw_card(rng))
    return dealer


def blackjack_settle(player, dealer, bet):
    """Dealer has already played. Returns (outcome, total paid back to the player; the bet is already taken)."""
    pv, dv = hand_value(player), hand_value(dealer)
    if pv > 21:
        return "bust", 0
    if dv > 21 or pv > dv:
        return "win", bet * 2
    if pv == dv:
        return "push", bet
    return "lose", 0


def render_cards(cards, hide_second=False, per_row=5):
    """Playing cards drawn in a code block so every hand lines up like real cards. Cards look like 'K♥' or '10♠'."""
    faces = []
    for i, c in enumerate(cards):
        rank, suit = c[:-1], c[-1]
        faces.append(None if (hide_second and i == 1) else (rank, suit))
    rows = []
    for k in range(0, len(faces), per_row):
        chunk = faces[k:k + per_row]
        lines = [[] for _ in range(5)]
        for f in chunk:
            lines[0].append("┌─────┐")
            if f is None:  # the dealer's hidden card
                lines[1].append("│░░░░░│"); lines[2].append("│░░░░░│"); lines[3].append("│░░░░░│")
            else:
                lines[1].append("│" + f[0].ljust(5) + "│")
                lines[2].append("│" + f[1].center(5) + "│")
                lines[3].append("│" + f[0].rjust(5) + "│")
            lines[4].append("└─────┘")
        rows.extend(" ".join(line) for line in lines)
    return "```\n" + "\n".join(rows) + "\n```"


# ---------- amounts ----------
def parse_amount(text, balance):
    t = str(text).strip().lower().replace(",", "").replace("_", "")
    if t in ("all", "max", "allin"):
        return min(balance, MAX_BET)
    if t == "half":
        return min(balance // 2, MAX_BET)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", t)
    if not m:
        return None
    return int(float(m[1]) * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m[2]])


def parse_bet(text, balance):
    """Returns (bet, error_message)."""
    bet = parse_amount(text, balance)
    if bet is None:
        return None, "I didn't understand that amount. Use a number, `1k`, `2.5m`, `half` or `all`."
    if bet < 1:
        return None, "The bet has to be at least 1."
    if bet > MAX_BET:
        return None, f"The max bet is {MAX_BET:,} {CURRENCY}."
    if bet > balance:
        return None, f"You only have {balance:,} {CURRENCY}."
    return bet, None


def money(n):
    return f"{int(n):,} {CURRENCY}"


def short_num(n):
    """1234 -> '1.2K', 2_500_000 -> '2.5M' (used where space is tight, like the leaderboard)."""
    n = int(n)
    if abs(n) < 1000:
        return str(n)
    for div, suffix in ((1_000, "K"), (1_000_000, "M"), (1_000_000_000, "B")):
        v = n / div
        if abs(v) < 999.95 or suffix == "B":  # 999,999 becomes 1M, not 1000K
            return f"{v:.1f}".rstrip("0").rstrip(".") + suffix


def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else (f"{m}m" if m else f"{seconds}s")


# ---------- shop ----------
BOUNCE_REWARD = 200
BOUNCE_COOLDOWN = 3600    # seconds between /bounce-flex uses
RENT_DIVISOR = 2400       # a property pays price/2400 extra on every /work (work is every 5 min: ~200 hours to pay back)
MAX_BUSINESSES = 3
MAX_PEELERS = 5           # the Potato Peeler is single-use, so you can stock a few
INCOME_CAP_HOURS = 24     # businesses store at most this much income until you /collect
LAY_LOW_SECONDS = 900     # 15 minutes after any robbery attempt: no working, no buying, no robbing
ROB_MIN = 50              # victims with less than this in their wallet aren't worth robbing
ROB_CAPS = {1: 300, 2: 1500, 3: 4000, 4: 10000, 5: 30000, 6: 100000}  # max loot per robbery, by weapon tier
STIFF_CHANCE = 0.08       # chance a boss refuses to pay after the shift
WORK_PAY = {"min": (12, 28), "decent": (50, 95)}

SHOP_CATEGORIES = {
    "weapons": ("🔫", "Weapons"),
    "estate": ("🏠", "Real estate"),
    "cars": ("🚗", "Cars"),
    "business": ("💼", "Businesses (max 3)"),
}
SHOP = {
    # --- weapons: the higher the tier the better. Used to /rob. Same tier = 50/50 gunfight, lower tier = you lose.
    "peeler":    {"name": "Potato Peeler", "emoji": "✂️", "category": "weapons", "tier": 1, "price": 100,
                  "consumable": True, "desc": "Cheap and desperate. It breaks after one robbery."},
    "knife":     {"name": "Knife", "emoji": "🔪", "category": "weapons", "tier": 2, "price": 250,
                  "desc": "Old school mugging."},
    "glock":     {"name": "Glock-18", "emoji": "🔫", "category": "weapons", "tier": 3, "price": 400,
                  "desc": "A reliable pistol."},
    "launcher":  {"name": "Potato Launcher", "emoji": "🚀", "category": "weapons", "tier": 4, "price": 1200,
                  "desc": "Spud cannon. Loud and effective."},
    "ak47":      {"name": "AK-47", "emoji": "🔫", "category": "weapons", "tier": 5, "price": 2500,
                  "desc": "Old faithful."},
    "dlore":     {"name": "AWP | Dragon Lore", "emoji": "🐉", "category": "weapons", "tier": 6, "price": 20000,
                  "desc": "The ultimate weapon."},
    # --- real estate: flex + rent on every /work
    "studio":    {"name": "Studio Apartment", "emoji": "🏢", "category": "estate", "price": 5000,
                  "desc": "Small, cozy, yours."},
    "house":     {"name": "Suburban House", "emoji": "🏡", "category": "estate", "price": 25000,
                  "desc": "A garage, a lawn, no mortgage."},
    "penthouse": {"name": "Penthouse", "emoji": "🌆", "category": "estate", "price": 120000,
                  "desc": "The whole top floor."},
    "island":    {"name": "Private Island", "emoji": "🏝️", "category": "estate", "price": 750000,
                  "desc": "Potato palms included."},
    # --- cars: flex (the Maybach also unlocks /bounce-flex)
    "civic":     {"name": "Honda Civic", "emoji": "🚗", "category": "cars", "price": 2500,
                  "desc": "Cheap, fast enough, never dies."},
    "charger":   {"name": "Dodge Charger Hellcat", "emoji": "🏎️", "category": "cars", "price": 25000,
                  "desc": "Supercharged muscle."},
    "escalade":  {"name": "Cadillac Escalade-V", "emoji": "🚙", "category": "cars", "price": 125000,
                  "desc": "Supercharged V8 luxury SUV."},
    "maybach":   {"name": "Bouncing Maybach", "emoji": "🚘", "category": "cars", "price": 200000,
                  "desc": "Hydraulics and good taste."},
    # --- businesses (you can own 3): the first two launder dirty potatoes, the big ones earn passive income
    "carwash":    {"name": "Spud Car Wash", "emoji": "🚿", "category": "business", "price": 10000,
                   "launder": {"per_day": 10000, "fee": 0.25}, "desc": "A front for washing dirty cash."},
    "nightclub":  {"name": "Potato Nightclub", "emoji": "🪩", "category": "business", "price": 40000,
                   "launder": {"per_day": 60000, "fee": 0.15}, "desc": "Cash-heavy and no questions asked."},
    "factory":    {"name": "Spud Chip Factory", "emoji": "🏭", "category": "business", "price": 50000,
                   "income": 400, "desc": "Chips, chips, chips."},
    "restaurant": {"name": "Fry Restaurant Chain", "emoji": "🍟", "category": "business", "price": 150000,
                   "income": 1300, "desc": "Franchises in every town."},
    "distillery": {"name": "Potato Vodka Distillery", "emoji": "🥃", "category": "business", "price": 400000,
                   "income": 3600, "desc": "Premium spirits from humble spuds."},
}


def rent_for(key):
    item = SHOP[key]
    return max(1, item["price"] // RENT_DIVISOR) if item["category"] == "estate" else 0


def perk_text(key):
    it = SHOP[key]
    cat = it["category"]
    if cat == "weapons":
        return f"tier {it['tier']} weapon for /rob" + (", single use" if it.get("consumable") else "")
    if cat == "estate":
        return f"+{rent_for(key):,} {CURRENCY} on every /work"
    if cat == "business":
        if "launder" in it:
            return f"launders dirty {CURRENCY}: up to {it['launder']['per_day']:,}/day, {round(it['launder']['fee'] * 100)}% cut"
        return f"earns {it['income']:,} {CURRENCY}/hour, /collect up to {INCOME_CAP_HOURS}h"
    if key == "maybach":
        return f"unlocks /bounce-flex: +{BOUNCE_REWARD} {CURRENCY} every {BOUNCE_COOLDOWN // 3600}h"
    return "pure flex"


def rob_outcome(rtier, vtier, rng=random):
    """win: victim's weapon is weaker (or they have none). reverse: theirs is stronger. Same tier: 50/50 gunfight."""
    if vtier < rtier:
        return "win"
    if vtier > rtier:
        return "reverse"
    return "fight_win" if rng.random() < 0.5 else "fight_lose"


# ---------- work: 15 workplaces, either minimum wage or properly paid (nothing "middle class") ----------
WORKPLACES = [
    {"name": "Fry Shack", "emoji": "🍟", "pay": "min", "job": "flipped fries through the lunch rush",
     "stiff": "The manager says the register 'came up short' again, so nobody gets paid today."},
    {"name": "Chez Spud Diner", "emoji": "🍽️", "pay": "min", "job": "washed a mountain of dishes",
     "stiff": "The owner says you 'broke too many plates' and keeps your wages."},
    {"name": "Sparkle Car Wash", "emoji": "🧽", "pay": "min", "job": "scrubbed cars all shift",
     "stiff": "The boss says the water bill 'ate all the profit'."},
    {"name": "Old MacPotato's Farm", "emoji": "🚜", "pay": "min", "job": "picked potatoes under the sun",
     "stiff": "The farmer waves from his tractor, drives off, and doesn't come back."},
    {"name": "the Gas Station", "emoji": "⛽", "pay": "min", "job": "covered the night shift at the counter",
     "stiff": "The clerk says the safe 'won't open' and the owner will 'settle up next week'."},
    {"name": "Downtown Parking Lot", "emoji": "🅿️", "pay": "min", "job": "ran the parking booth",
     "stiff": "The cash box is empty and so are the owner's promises."},
    {"name": "Mega Mall", "emoji": "🧹", "pay": "min", "job": "mopped the entire food court",
     "stiff": "Management says the cleaning budget was cut... retroactively."},
    {"name": "Pizza Palace", "emoji": "🛵", "pay": "min", "job": "delivered pizzas across town",
     "stiff": "The shop says your tips ARE your pay. There were no tips."},
    {"name": "a Billionaire's Yacht", "emoji": "🛥️", "pay": "decent", "job": "polished the deck of a mega-yacht",
     "stiff": "The billionaire's assistant says the yacht already sailed, with your paycheck on board."},
    {"name": "a Tycoon's Private Kitchen", "emoji": "👨‍🍳", "pay": "decent", "job": "cooked a tasting menu for a tycoon",
     "stiff": "The tycoon claims the soufflé fell and refuses to pay for failure."},
    {"name": "the High-Roller Casino", "emoji": "🎰", "pay": "decent", "job": "dealt cards in the VIP room",
     "stiff": "The pit boss says the casino had a bad night, so wages are 'on the house'."},
    {"name": "a Blockbuster Movie Set", "emoji": "🎬", "pay": "decent", "job": "did stunts for a big action movie",
     "stiff": "The studio says your pay is 'in post-production' indefinitely."},
    {"name": "a Celebrity Security Detail", "emoji": "🕶️", "pay": "decent", "job": "guarded a celebrity at a premiere",
     "stiff": "The celebrity's manager says 'the check is in the mail'."},
    {"name": "Sky High Helicopters", "emoji": "🚁", "pay": "decent", "job": "flew tourists over the city",
     "stiff": "The company filed for bankruptcy this morning. Funny timing."},
    {"name": "the Five-Star Hotel", "emoji": "🛎️", "pay": "decent", "job": "worked the concierge desk of a luxury hotel",
     "stiff": "The hotel says 'exposure' counts as compensation here."},
]


# ---------- case spin: fast at first, then gradually slower ----------
SPIN_DELAYS = [0.45, 0.45, 0.5, 0.55, 0.65, 0.8, 1.0, 1.25, 1.55]  # seconds before each frame
REEL_WINDOW = 5                                                     # items visible at once
FILLER_WEIGHTS = [("blue", 50), ("purple", 25), ("pink", 14), ("red", 8), ("gold", 3)]


def build_reel(case_key, drop_name, drop_rarity, frames=None, rng=random):
    """The strip of items that scrolls past. The window shown on the LAST frame is centred on the real drop."""
    frames = frames or len(SPIN_DELAYS)
    items = CASES[case_key]["items"]
    rarities, weights = zip(*FILLER_WEIGHTS)
    reel = []
    for _ in range(frames + REEL_WINDOW - 1):
        rar = rng.choices(rarities, weights=weights)[0]
        reel.append((rng.choice(items[rar])[0], rar))
    reel[frames + REEL_WINDOW // 2 - 1] = (drop_name, drop_rarity)
    return reel


# ---------- database ----------
class EconomyDB:
    # Columns added to databases created by older versions of the bot (so nobody's balance is lost on update)
    USER_COLUMNS = {
        "last_bounce": "REAL NOT NULL DEFAULT 0",
        "dirty": "INTEGER NOT NULL DEFAULT 0",          # stolen potatoes: must be laundered before they can be spent
        "lay_low_until": "REAL NOT NULL DEFAULT 0",
        "last_collect": "REAL NOT NULL DEFAULT 0",      # business income is paid from this moment
        "launder_start": "REAL NOT NULL DEFAULT 0",     # start of the current 24h laundering window
        "launder_used": "INTEGER NOT NULL DEFAULT 0",
    }

    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0,
                    last_daily REAL NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0,
                    last_work REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id));
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    name TEXT NOT NULL, rarity TEXT NOT NULL, wear TEXT NOT NULL,
                    wear_float REAL NOT NULL, stattrak INTEGER NOT NULL DEFAULT 0,
                    value INTEGER NOT NULL, case_key TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_items_owner ON items (guild_id, user_id);
                CREATE TABLE IF NOT EXISTS assets (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    item_key TEXT NOT NULL, bought REAL NOT NULL,
                    PRIMARY KEY (guild_id, user_id, item_key));
            """)
            self._add_missing_columns("users", self.USER_COLUMNS)
            self._add_missing_columns("assets", {"qty": "INTEGER NOT NULL DEFAULT 1"})
            self.conn.commit()

    def _add_missing_columns(self, table, columns):
        have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def q(self, sql, args=(), one=False):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.fetchone() if one else cur.fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def ensure(self, g, u):
        self.x("INSERT OR IGNORE INTO users (guild_id, user_id, balance) VALUES (?, ?, ?)", (g, u, START_BALANCE))

    def balance(self, g, u):
        self.ensure(g, u)
        return self.q("SELECT balance FROM users WHERE guild_id=? AND user_id=?", (g, u), one=True)["balance"]

    def dirty_balance(self, g, u):
        self.ensure(g, u)
        return self.q("SELECT dirty FROM users WHERE guild_id=? AND user_id=?", (g, u), one=True)["dirty"]

    def lay_low_left(self, g, u, now=None):
        """Seconds of 'peacetime' left after a robbery (0 = free to act)."""
        now = time.time() if now is None else now
        self.ensure(g, u)
        until = self.q("SELECT lay_low_until FROM users WHERE guild_id=? AND user_id=?", (g, u), one=True)["lay_low_until"]
        return max(0.0, until - now)

    def add(self, g, u, amount):
        self.ensure(g, u)
        self.x("UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?", (int(amount), g, u))

    def give(self, g, u, amount):
        """Admin: add (or take, with a negative number) potatoes; never goes below 0."""
        self.ensure(g, u)
        self.x("UPDATE users SET balance = MAX(0, balance + ?) WHERE guild_id=? AND user_id=?", (int(amount), g, u))

    def spend(self, g, u, amount):
        """Atomically take `amount`. False (and nothing changes) if the balance is too low."""
        self.ensure(g, u)
        cur = self.x("UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                     (int(amount), g, u, int(amount)))
        return cur.rowcount == 1

    def transfer(self, g, src, dst, amount):
        with self.lock:
            self.ensure(g, src)
            self.ensure(g, dst)
            cur = self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                (amount, g, src, amount))
            if cur.rowcount != 1:
                self.conn.rollback()
                return False
            self.conn.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?", (amount, g, dst))
            self.conn.commit()
            return True

    def claim_daily(self, g, u, now=None):
        """Returns (claimed, reward, streak, seconds_to_wait)."""
        now = time.time() if now is None else now
        self.ensure(g, u)
        row = self.q("SELECT last_daily, streak FROM users WHERE guild_id=? AND user_id=?", (g, u), one=True)
        last, streak = row["last_daily"], row["streak"]
        if last and now - last < 86400:
            return False, 0, streak, last + 86400 - now
        streak = streak + 1 if last and now - last < 172800 else 1
        reward = DAILY_BASE + DAILY_STREAK_BONUS * min(streak - 1, DAILY_STREAK_MAX)
        self.x("UPDATE users SET balance = balance + ?, last_daily=?, streak=? WHERE guild_id=? AND user_id=?",
               (reward, now, streak, g, u))
        return True, reward, streak, 0

    def claim_work(self, g, u, now=None, rng=random):
        """Returns (status, info). status: 'ok' | 'cooldown' | 'laylow'.
        On 'ok' info has: place, pay, stiffed (the boss refused to pay), rent (from owned real estate)."""
        now = time.time() if now is None else now
        with self.lock:
            self.ensure(g, u)
            row = self.conn.execute("SELECT last_work, lay_low_until FROM users WHERE guild_id=? AND user_id=?",
                                    (g, u)).fetchone()
            if row["lay_low_until"] > now:
                return "laylow", {"wait": row["lay_low_until"] - now}
            if row["last_work"] and now - row["last_work"] < WORK_COOLDOWN:
                return "cooldown", {"wait": row["last_work"] + WORK_COOLDOWN - now}
            place = rng.choice(WORKPLACES)
            low, high = WORK_PAY[place["pay"]]
            stiffed = rng.random() < STIFF_CHANCE
            pay = 0 if stiffed else rng.randint(low, high)
            rent = self.property_rent(g, u)
            self.conn.execute("UPDATE users SET balance = balance + ?, last_work=? WHERE guild_id=? AND user_id=?",
                              (pay + rent, now, g, u))
            self.conn.commit()
            return "ok", {"place": place, "pay": pay, "stiffed": stiffed, "rent": rent}

    # ----- shop / assets -----
    def owned(self, g, u):
        """{item_key: quantity}"""
        rows = self.q("SELECT item_key, qty FROM assets WHERE guild_id=? AND user_id=? AND qty > 0", (g, u))
        return {r["item_key"]: r["qty"] for r in rows}

    def has_asset(self, g, u, key):
        return key in self.owned(g, u)

    def property_rent(self, g, u):
        return sum(rent_for(k) for k in self.owned(g, u) if k in SHOP)

    def best_weapon(self, g, u):
        """Key of the strongest weapon someone owns, or None."""
        best = None
        for k in self.owned(g, u):
            it = SHOP.get(k)
            if it and it["category"] == "weapons" and (best is None or it["tier"] > SHOP[best]["tier"]):
                best = k
        return best

    def _income_rate(self, g, u):
        return sum(SHOP[k]["income"] for k in self.owned(g, u) if k in SHOP and "income" in SHOP[k])

    def _settle_income(self, g, u, now):
        """Pay the income earned so far and restart the clock. The caller commits."""
        rate = self._income_rate(g, u)
        last = self.conn.execute("SELECT last_collect FROM users WHERE guild_id=? AND user_id=?", (g, u)).fetchone()["last_collect"]
        paid = 0
        if rate and last:
            paid = int(rate * min(max(0.0, now - last) / 3600, INCOME_CAP_HOURS))
        self.conn.execute("UPDATE users SET balance = balance + ?, last_collect=? WHERE guild_id=? AND user_id=?",
                          (paid, now, g, u))
        return paid

    def _grant(self, g, u, key, now):
        """Put an item in someone's assets (the caller holds the lock and commits/rolls back).
        Returns 'ok', 'owned' (already have it, or carrying the max) or 'limit' (3 businesses already)."""
        it = SHOP[key]
        have = self.owned(g, u)
        if it.get("consumable"):
            if have.get(key, 0) >= MAX_PEELERS:
                return "owned"
            if key in have:
                self.conn.execute("UPDATE assets SET qty = qty + 1 WHERE guild_id=? AND user_id=? AND item_key=?", (g, u, key))
            else:
                self.conn.execute("INSERT OR REPLACE INTO assets (guild_id, user_id, item_key, bought, qty) VALUES (?, ?, ?, ?, 1)",
                                  (g, u, key, now))
            return "ok"
        if key in have:
            return "owned"
        if it["category"] == "business":
            if sum(1 for k in have if k in SHOP and SHOP[k]["category"] == "business") >= MAX_BUSINESSES:
                return "limit"
            if "income" in it:
                self._settle_income(g, u, now)  # income earned so far is paid; the new business starts earning now
        self.conn.execute("INSERT OR REPLACE INTO assets (guild_id, user_id, item_key, bought, qty) VALUES (?, ?, ?, ?, 1)",
                          (g, u, key, now))
        return "ok"

    def buy_asset(self, g, u, key, price, now=None):
        """Returns 'ok', 'owned', 'limit' or 'poor'. The payment and the purchase happen together or not at all."""
        now = time.time() if now is None else now
        with self.lock:
            self.ensure(g, u)
            status = self._grant(g, u, key, now)
            if status != "ok":
                self.conn.rollback()
                return status
            cur = self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                (price, g, u, price))
            if cur.rowcount != 1:
                self.conn.rollback()
                return "poor"
            self.conn.commit()
            return "ok"

    def grant_asset(self, g, u, key, now=None):
        """Admin: give an item for free. Returns 'ok', 'owned' or 'limit'."""
        now = time.time() if now is None else now
        with self.lock:
            self.ensure(g, u)
            status = self._grant(g, u, key, now)
            if status == "ok":
                self.conn.commit()
            else:
                self.conn.rollback()
            return status

    def claim_bounce(self, g, u, now=None):
        """Returns ('ok'|'noasset'|'cooldown', seconds_to_wait). Needs the Bouncing Maybach."""
        now = time.time() if now is None else now
        with self.lock:
            self.ensure(g, u)
            if not self.has_asset(g, u, "maybach"):
                return "noasset", 0
            last = self.conn.execute("SELECT last_bounce FROM users WHERE guild_id=? AND user_id=?",
                                     (g, u)).fetchone()["last_bounce"]
            if last and now - last < BOUNCE_COOLDOWN:
                return "cooldown", last + BOUNCE_COOLDOWN - now
            self.conn.execute("UPDATE users SET balance = balance + ?, last_bounce=? WHERE guild_id=? AND user_id=?",
                              (BOUNCE_REWARD, now, g, u))
            self.conn.commit()
            return "ok", 0

    # ----- businesses -----
    def collect_income(self, g, u, now=None):
        """Returns (status, paid, rate_per_hour, hours_stored). status: 'ok' | 'nobiz' | 'empty'."""
        now = time.time() if now is None else now
        with self.lock:
            self.ensure(g, u)
            rate = self._income_rate(g, u)
            if not rate:
                return "nobiz", 0, 0, 0.0
            last = self.conn.execute("SELECT last_collect FROM users WHERE guild_id=? AND user_id=?",
                                     (g, u)).fetchone()["last_collect"] or now
            hours = max(0.0, now - last) / 3600
            paid = int(rate * min(hours, INCOME_CAP_HOURS))
            if paid <= 0:
                return "empty", 0, rate, hours
            self.conn.execute("UPDATE users SET balance = balance + ?, last_collect=? WHERE guild_id=? AND user_id=?",
                              (paid, now, g, u))
            self.conn.commit()
            return "ok", paid, rate, hours

    def launder(self, g, u, amount, now=None):
        """Wash dirty potatoes into clean ones through the laundering businesses you own.
        Returns (status, info). status: 'ok' | 'nobiz' | 'nodirty' | 'capped'."""
        now = time.time() if now is None else now
        with self.lock:
            self.ensure(g, u)
            fronts = [SHOP[k]["launder"] for k in self.owned(g, u) if k in SHOP and "launder" in SHOP[k]]
            if not fronts:
                return "nobiz", {}
            per_day = sum(f["per_day"] for f in fronts)
            fee = min(f["fee"] for f in fronts)
            row = self.conn.execute("SELECT dirty, launder_start, launder_used FROM users WHERE guild_id=? AND user_id=?",
                                    (g, u)).fetchone()
            start, used = row["launder_start"], row["launder_used"]
            if not start or now - start >= 86400:
                start, used = now, 0
            remaining = per_day - used
            if row["dirty"] <= 0:
                return "nodirty", {}
            if remaining <= 0:
                return "capped", {"wait": start + 86400 - now, "per_day": per_day}
            washed = min(int(amount), row["dirty"], remaining)
            if washed <= 0:
                return "nodirty", {}
            cut = int(washed * fee)
            clean = washed - cut
            self.conn.execute(
                "UPDATE users SET dirty = dirty - ?, balance = balance + ?, launder_start=?, launder_used=? "
                "WHERE guild_id=? AND user_id=?", (washed, clean, start, used + washed, g, u))
            self.conn.commit()
            return "ok", {"washed": washed, "clean": clean, "cut": cut, "fee": fee,
                          "remaining": remaining - washed, "per_day": per_day}

    # ----- robbery -----
    def rob(self, g, robber, victim, now=None, rng=random):
        """One robbery attempt, all-or-nothing. Returns a dict with 'status':
        'laylow' | 'noweapon' | 'broke' | 'done'. After 'done' the robber must lay low for 15 minutes.
        Stolen potatoes are DIRTY (they land in the robber's dirty balance and must be laundered)."""
        now = time.time() if now is None else now
        with self.lock:
            self.ensure(g, robber)
            self.ensure(g, victim)
            r = self.conn.execute("SELECT balance, dirty, lay_low_until FROM users WHERE guild_id=? AND user_id=?",
                                  (g, robber)).fetchone()
            if r["lay_low_until"] > now:
                return {"status": "laylow", "wait": r["lay_low_until"] - now}
            rkey = self.best_weapon(g, robber)
            if rkey is None:
                return {"status": "noweapon"}
            vbal = self.conn.execute("SELECT balance FROM users WHERE guild_id=? AND user_id=?",
                                     (g, victim)).fetchone()["balance"]
            if vbal < ROB_MIN:
                return {"status": "broke", "balance": vbal}
            vkey = self.best_weapon(g, victim)
            rtier = SHOP[rkey]["tier"]
            vtier = SHOP[vkey]["tier"] if vkey else 0
            outcome = rob_outcome(rtier, vtier, rng)
            if outcome in ("win", "fight_win"):
                amount = max(1, min(int(vbal * rng.uniform(0.10, 0.30)), ROB_CAPS[rtier]))
                self.conn.execute("UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=?", (amount, g, victim))
                self.conn.execute("UPDATE users SET dirty = dirty + ? WHERE guild_id=? AND user_id=?", (amount, g, robber))
            else:  # reverse robbery: the victim takes from YOU (clean potatoes first, then dirty ones)
                pool = r["balance"] + r["dirty"]
                amount = min(int(pool * rng.uniform(0.15, 0.30)), ROB_CAPS[vtier])
                from_clean = min(r["balance"], amount)
                from_dirty = amount - from_clean
                if amount:
                    self.conn.execute("UPDATE users SET balance = balance - ?, dirty = dirty - ? WHERE guild_id=? AND user_id=?",
                                      (from_clean, from_dirty, g, robber))
                    self.conn.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?", (amount, g, victim))
            if rkey == "peeler":  # single use: it's gone whether you won or not
                self.conn.execute("UPDATE assets SET qty = qty - 1 WHERE guild_id=? AND user_id=? AND item_key='peeler'", (g, robber))
                self.conn.execute("DELETE FROM assets WHERE guild_id=? AND user_id=? AND item_key='peeler' AND qty <= 0", (g, robber))
            self.conn.execute("UPDATE users SET lay_low_until=? WHERE guild_id=? AND user_id=?",
                              (now + LAY_LOW_SECONDS, g, robber))
            self.conn.commit()
            return {"status": "done", "outcome": outcome, "amount": amount, "rkey": rkey, "vkey": vkey,
                    "rtier": rtier, "vtier": vtier}

    # ----- skins -----
    def add_item(self, g, u, item):
        cur = self.x(
            "INSERT INTO items (guild_id, user_id, name, rarity, wear, wear_float, stattrak, value, case_key, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (g, u, item["name"], item["rarity"], item["wear"], item["wear_float"],
             item["stattrak"], item["value"], item["case_key"], time.time()))
        return cur.lastrowid

    def items(self, g, u, limit=10, offset=0):
        return self.q("SELECT * FROM items WHERE guild_id=? AND user_id=? ORDER BY value DESC, id DESC LIMIT ? OFFSET ?",
                      (g, u, limit, offset))

    def inventory_stats(self, g, u):
        row = self.q("SELECT COUNT(*) AS c, COALESCE(SUM(value), 0) AS v FROM items WHERE guild_id=? AND user_id=?",
                     (g, u), one=True)
        return row["c"], row["v"]

    def sell_item(self, g, u, item_id):
        """Sell one item you own. Returns the potatoes received, or None if it isn't yours / already sold."""
        with self.lock:
            self.ensure(g, u)
            row = self.conn.execute("SELECT value FROM items WHERE id=? AND guild_id=? AND user_id=?",
                                    (item_id, g, u)).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            self.conn.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                              (row["value"], g, u))
            self.conn.commit()
            return row["value"]

    def sell_rarities(self, g, u, rarities):
        """Sell every item of the given rarities. Returns (count, potatoes)."""
        marks = ",".join("?" for _ in rarities)
        with self.lock:
            self.ensure(g, u)
            row = self.conn.execute(
                f"SELECT COUNT(*) AS c, COALESCE(SUM(value), 0) AS v FROM items "
                f"WHERE guild_id=? AND user_id=? AND rarity IN ({marks})", (g, u, *rarities)).fetchone()
            if row["c"]:
                self.conn.execute(f"DELETE FROM items WHERE guild_id=? AND user_id=? AND rarity IN ({marks})",
                                  (g, u, *rarities))
                self.conn.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
                                  (row["v"], g, u))
                self.conn.commit()
            return row["c"], row["v"]

    def standings(self, g):
        """Everyone with something, richest first, with a breakdown. Returns (rows, summary).
        worth = wallet + value of skins + shop price of owned assets (dirty potatoes don't count)."""
        users = self.q("SELECT user_id, balance FROM users WHERE guild_id=?", (g,))
        skins = {r["user_id"]: r["v"] for r in self.q(
            "SELECT user_id, COALESCE(SUM(value), 0) AS v FROM items WHERE guild_id=? GROUP BY user_id", (g,))}
        assets = {}
        for r in self.q("SELECT user_id, item_key, qty FROM assets WHERE guild_id=? AND qty > 0", (g,)):
            if r["item_key"] in SHOP:
                assets[r["user_id"]] = assets.get(r["user_id"], 0) + SHOP[r["item_key"]]["price"] * r["qty"]
        rows = []
        for r in users:
            u = r["user_id"]
            row = {"user_id": u, "balance": r["balance"], "skins": skins.get(u, 0), "assets": assets.get(u, 0)}
            row["worth"] = row["balance"] + row["skins"] + row["assets"]
            if row["worth"] > 0:
                rows.append(row)
        rows.sort(key=lambda x: (-x["worth"], x["user_id"]))
        summary = {"players": len(users), "circulation": sum(r["balance"] for r in users),
                   "worth": sum(r["worth"] for r in rows)}
        return rows, summary

    def leaderboard(self, g, limit=10):
        """Top players by net worth (balance + inventory value)."""
        return self.q(
            "SELECT u.user_id AS user_id, u.balance + COALESCE((SELECT SUM(i.value) FROM items i "
            "WHERE i.guild_id=u.guild_id AND i.user_id=u.user_id), 0) AS worth "
            "FROM users u WHERE u.guild_id=? ORDER BY worth DESC LIMIT ?", (g, limit))


def open_economy():
    try:
        return EconomyDB(ECON_DB_PATH)
    except Exception as e:
        print(f"⚠️ Couldn't open {ECON_DB_PATH} ({type(e).__name__}): economy will live in memory and reset on restart")
        return EconomyDB(":memory:")


econ = open_economy()


# ---------- Discord side ----------
CASE_CHOICES = [app_commands.Choice(name=f"{c['name']} - {c['price']:,} potatoes", value=k) for k, c in CASES.items()]
JOBS = [
    "peeled potatoes at the fry shack", "flipped burgers next to the fryer", "delivered sacks of spuds",
    "washed dishes at a diner", "sold ketchup door to door", "harvested a field of potatoes",
    "juggled hot potatoes at a street fair", "drove a fry truck",
]
bj_active = set()  # (guild_id, user_id) with a blackjack hand in progress


def econ_embed(title, description, color=POTATO_COLOR):
    return discord.Embed(title=title, description=description, color=color)


async def take_bet(interaction, amount_text):
    """Validate the bet and take it from the balance. Replies (privately) and returns None if it can't."""
    g, u = interaction.guild.id, interaction.user.id
    bet, err = parse_bet(amount_text, econ.balance(g, u))
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return None
    if not econ.spend(g, u, bet):
        await interaction.response.send_message(f"❌ You don't have {money(bet)}.", ephemeral=True)
        return None
    return bet


# ----- basics -----
@bot.tree.command(name="balance", description="Check your potatoes (or someone else's)")
@app_commands.describe(user="Whose balance to check (default: you)")
@app_commands.guild_only()
async def balance_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    g = interaction.guild.id
    count, value = econ.inventory_stats(g, target.id)
    embed = econ_embed(f"{CURRENCY} {target.display_name}", None)
    embed.add_field(name="Balance", value=money(econ.balance(g, target.id)))
    dirty = econ.dirty_balance(g, target.id)
    if dirty:
        embed.add_field(name="🧼 Dirty (launder it first)", value=money(dirty))
    embed.add_field(name="Skins", value=f"{count} item(s) worth {money(value)}")
    wait = econ.lay_low_left(g, target.id)
    if wait:
        embed.add_field(name="🤫 Laying low", value=f"{fmt_wait(wait)} left", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="daily", description="Claim your daily potatoes (streaks give a bonus)")
@app_commands.guild_only()
async def daily_cmd(interaction: discord.Interaction):
    g, u = interaction.guild.id, interaction.user.id
    ok, reward, streak, wait = econ.claim_daily(g, u)
    if not ok:
        await interaction.response.send_message(f"⏳ Come back in **{fmt_wait(wait)}** for your next daily.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"🎁 **+{money(reward)}** — day **{streak}** streak! Balance: {money(econ.balance(g, u))}"
    )


@bot.tree.command(name="work", description="Work a shift for potatoes (every 5 minutes... if your boss actually pays)")
@app_commands.guild_only()
async def work_cmd(interaction: discord.Interaction):
    g, u = interaction.guild.id, interaction.user.id
    status, info = econ.claim_work(g, u)
    if status == "laylow":
        await interaction.response.send_message(
            f"🤫 You must lay low for a while. You can't work for another **{fmt_wait(info['wait'])}**.", ephemeral=True)
        return
    if status == "cooldown":
        await interaction.response.send_message(f"⏳ You're tired. Work again in **{fmt_wait(info['wait'])}**.", ephemeral=True)
        return
    place = info["place"]
    rent = f"\n🏠 Rent from your properties: **+{money(info['rent'])}**" if info["rent"] else ""
    if info["stiffed"]:
        text = (f"{place['emoji']} You {place['job']} at **{place['name']}**.\n"
                f"💀 {place['stiff']}\nYou earned **{money(0)}**.")
        color = 0xED4245
    else:
        text = f"{place['emoji']} You {place['job']} at **{place['name']}** and earned **{money(info['pay'])}**."
        color = POTATO_COLOR
    await interaction.response.send_message(
        embed=econ_embed("💼 Shift report", f"{text}{rent}\nBalance: {money(econ.balance(g, u))}", color))


@bot.tree.command(name="pay", description="Give some of your potatoes to another player")
@app_commands.describe(user="Who gets the potatoes", amount="A number, 1k, 2.5m, half or all")
@app_commands.guild_only()
async def pay_cmd(interaction: discord.Interaction, user: discord.Member, amount: str):
    g, u = interaction.guild.id, interaction.user.id
    if user.bot or user.id == u:
        await interaction.response.send_message("❌ Pick another (human) player.", ephemeral=True)
        return
    bet, err = parse_bet(amount, econ.balance(g, u))
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return
    if not econ.transfer(g, u, user.id, bet):
        await interaction.response.send_message("❌ You don't have enough potatoes.", ephemeral=True)
        return
    await interaction.response.send_message(f"💸 {interaction.user.mention} paid {user.mention} **{money(bet)}**.")


LEADERBOARD_SIZE = 20


@bot.tree.command(name="leaderboard", description="The big leaderboard: the richest potatoes in this server")
@app_commands.guild_only()
async def leaderboard_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    rows, info = econ.standings(guild.id)
    if not rows:
        await interaction.response.send_message("Nobody has any potatoes yet. Try `/daily`!", ephemeral=True)
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows[:LEADERBOARD_SIZE]):
        member = guild.get_member(r["user_id"])
        name = md_safe(shorten(member.display_name, 22)) if member else f"<@{r['user_id']}>"
        rank = medals[i] if i < 3 else f"`{i + 1:>2}.`"
        lines.append(f"{rank} **{name}** — **{short_num(r['worth'])}** {CURRENCY}\n"
                     f"┗ 💰 {short_num(r['balance'])} · 🎒 {short_num(r['skins'])} · 🏠 {short_num(r['assets'])}")
    header = (f"👥 **{info['players']}** players  ·  {CURRENCY} **{short_num(info['circulation'])}** in wallets  ·  "
              f"💎 **{short_num(info['worth'])}** total net worth")
    body = "\n".join(lines)
    while len(header) + len(body) + 20 > 4000 and "\n" in body:  # never exceed Discord's embed size
        body = body.rsplit("\n", 2)[0]
    embed = discord.Embed(title=f"🏆 THE BIG LEADERBOARD — {guild.name}"[:250], description=f"{header}\n\n{body}",
                          color=0xFFD700)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    mine = next((i + 1 for i, r in enumerate(rows) if r["user_id"] == interaction.user.id), None)
    embed.set_footer(text=("Net worth = wallet 💰 + skins 🎒 + assets 🏠" +
                           (f"  •  you're #{mine}" if mine else "  •  you're not on the board yet")))
    await interaction.response.send_message(embed=embed)


# ----- simple games -----
@bot.tree.command(name="coinflip", description="Bet on heads or tails (pays 2x)")
@app_commands.describe(amount="A number, 1k, 2.5m, half or all", side="Heads or tails")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 2.0)
async def coinflip_cmd(interaction: discord.Interaction, amount: str, side: Literal["heads", "tails"]):
    bet = await take_bet(interaction, amount)
    if bet is None:
        return
    g, u = interaction.guild.id, interaction.user.id
    result = random.choice(["heads", "tails"])
    won = result == side
    if won:
        econ.add(g, u, bet * 2)
    embed = econ_embed(
        f"🪙 {result.title()}!",
        (f"You called **{side}** and **won {money(bet)}**!" if won else f"You called **{side}** and lost **{money(bet)}**.")
        + f"\nBalance: {money(econ.balance(g, u))}",
        0x57F287 if won else 0xED4245,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="slots", description="Spin the slot machine")
@app_commands.describe(amount="A number, 1k, 2.5m, half or all")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 3.0)
async def slots_cmd(interaction: discord.Interaction, amount: str):
    bet = await take_bet(interaction, amount)
    if bet is None:
        return
    g, u = interaction.guild.id, interaction.user.id
    reels = spin_slots()
    mult = slots_multiplier(reels)
    payout = bet * mult
    if payout:
        econ.add(g, u, payout)
    await interaction.response.send_message(embed=econ_embed("🎰 Slots", "🎰 | 🎰 | 🎰\n*spinning...*"))
    await asyncio.sleep(1.2)
    if mult:
        result = f"**{mult}x** — you won **{money(payout - bet)}**!" if mult > 1 else "Push."
        color = 0x57F287
        if mult >= 50:
            result = "🎉 **JACKPOT!** " + result
    else:
        result, color = f"No match. You lost **{money(bet)}**.", 0xED4245
    embed = econ_embed("🎰 Slots", f"{' | '.join(reels)}\n\n{result}\nBalance: {money(econ.balance(g, u))}", color)
    embed.set_footer(text="Pays: 3 of a kind, or exactly two 🍒 (2x)")
    await interaction.edit_original_response(embed=embed)


@bot.tree.command(name="roulette", description="Bet on red/black, even/odd, low/high (2x) or a number 0-36 (36x)")
@app_commands.describe(amount="A number, 1k, 2.5m, half or all",
                       bet="red, black, even, odd, low (1-18), high (19-36) or a number 0-36")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 3.0)
async def roulette_cmd(interaction: discord.Interaction, amount: str, bet: str):
    parsed = parse_roulette_bet(bet)
    if parsed is None:
        await interaction.response.send_message(
            "❌ Bet on `red`, `black`, `even`, `odd`, `low`, `high`, or a number from 0 to 36.", ephemeral=True)
        return
    label, mult, test = parsed
    stake = await take_bet(interaction, amount)
    if stake is None:
        return
    g, u = interaction.guild.id, interaction.user.id
    number = random.randint(0, 36)
    color = roulette_color(number)
    emoji = {"red": "🔴", "black": "⚫", "green": "🟢"}[color]
    won = test(number)
    if won:
        econ.add(g, u, stake * mult)
    text = (f"The ball landed on {emoji} **{number}**.\nYou bet on **{label}** and "
            + (f"**won {money(stake * mult - stake)}**!" if won else f"lost **{money(stake)}**.")
            + f"\nBalance: {money(econ.balance(g, u))}")
    await interaction.response.send_message(
        embed=econ_embed("🎡 Roulette", text, 0x57F287 if won else 0xED4245))


# ----- blackjack -----
class BlackjackGame:
    def __init__(self, guild_id, user_id, name, bet):
        self.guild_id, self.user_id, self.name, self.bet = guild_id, user_id, name, bet
        self.player = [draw_card(), draw_card()]
        self.dealer = [draw_card(), draw_card()]
        self.finished = False
        self.outcome = None
        self.payout = 0

    def settle(self):
        """Resolve the hand (dealer plays if needed), pay out, and free the player for a new game."""
        if self.finished:
            return
        if hand_value(self.player) <= 21:
            dealer_play(self.dealer)
        self.outcome, self.payout = blackjack_settle(self.player, self.dealer, self.bet)
        self._finish()

    def settle_natural(self):
        if is_blackjack(self.dealer):
            self.outcome, self.payout = "push", self.bet
        else:
            self.outcome, self.payout = "blackjack", self.bet + (self.bet * 3) // 2
        self._finish()

    def _finish(self):
        self.finished = True
        if self.payout:
            econ.add(self.guild_id, self.user_id, self.payout)
        bj_active.discard((self.guild_id, self.user_id))


def blackjack_embed(game):
    reveal = game.finished
    dealer_val = hand_value(game.dealer) if reveal else hand_value(game.dealer[:1])
    player_val = hand_value(game.player)
    embed = discord.Embed(title=f"🃏 Blackjack  •  bet {money(game.bet)}", color=POTATO_COLOR)
    dealer_tag = "  💥 BUST" if reveal and dealer_val > 21 else ("" if reveal else "  (one card hidden)")
    embed.add_field(name=f"🎩 Dealer — {dealer_val}{dealer_tag}",
                    value=render_cards(game.dealer, hide_second=not reveal), inline=False)
    player_tag = "  💥 BUST" if player_val > 21 else ("  ✨ BLACKJACK" if is_blackjack(game.player) else "")
    embed.add_field(name=f"🧑 {md_safe(game.name)} — {player_val}{player_tag}",
                    value=render_cards(game.player), inline=False)
    if game.finished:
        net = game.payout - game.bet
        text, color = {
            "blackjack": (f"✨ **Blackjack!** You won **{money(net)}**.", 0x57F287),
            "win": (f"✅ You won **{money(net)}**!", 0x57F287),
            "push": ("🤝 Push. Your bet is returned.", 0x95A5A6),
            "lose": (f"❌ Dealer wins. You lost **{money(game.bet)}**.", 0xED4245),
            "bust": (f"💥 Bust! You lost **{money(game.bet)}**.", 0xED4245),
        }[game.outcome]
        embed.color = color
        embed.add_field(name="Result", value=f"{text}\nBalance: {money(econ.balance(game.guild_id, game.user_id))}",
                        inline=False)
    else:
        embed.set_footer(text="Hit = another card  •  Stand = keep your hand  •  Double = 2x bet, one card (first move only)")
    return embed


class BlackjackView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=120)
        self.game = game
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message("❌ This isn't your hand.", ephemeral=True)
            return False
        return True

    async def _end(self, interaction):
        self.game.settle()
        self.stop()
        await interaction.response.edit_message(embed=blackjack_embed(self.game), view=None)

    @discord.ui.button(label="Hit", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit_btn(self, interaction, button):
        game = self.game
        game.player.append(draw_card())
        if hand_value(game.player) >= 21:  # bust, or 21 (stand automatically)
            await self._end(interaction)
        else:
            await interaction.response.edit_message(embed=blackjack_embed(game), view=self)

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand_btn(self, interaction, button):
        await self._end(interaction)

    @discord.ui.button(label="Double", emoji="✌️", style=discord.ButtonStyle.success)
    async def double_btn(self, interaction, button):
        game = self.game
        if len(game.player) != 2:
            await interaction.response.send_message("❌ You can only double on your first move.", ephemeral=True)
            return
        if not econ.spend(game.guild_id, game.user_id, game.bet):
            await interaction.response.send_message("❌ You don't have enough potatoes to double.", ephemeral=True)
            return
        game.bet *= 2
        game.player.append(draw_card())
        await self._end(interaction)

    async def on_timeout(self):
        if self.game.finished:
            return
        self.game.settle()  # walked away: the hand stands
        if self.message is not None:
            try:
                await self.message.edit(embed=blackjack_embed(self.game), view=None)
            except discord.HTTPException:
                pass


@bot.tree.command(name="blackjack", description="Play a hand of blackjack against the dealer (3:2 on blackjack)")
@app_commands.describe(amount="A number, 1k, 2.5m, half or all")
@app_commands.guild_only()
async def blackjack_cmd(interaction: discord.Interaction, amount: str):
    key = (interaction.guild.id, interaction.user.id)
    if key in bj_active:
        await interaction.response.send_message("❌ Finish your current blackjack hand first.", ephemeral=True)
        return
    bet = await take_bet(interaction, amount)
    if bet is None:
        return
    game = BlackjackGame(key[0], key[1], interaction.user.display_name, bet)
    bj_active.add(key)
    if is_blackjack(game.player):
        game.settle_natural()
        await interaction.response.send_message(embed=blackjack_embed(game))
        return
    view = BlackjackView(game)
    await interaction.response.send_message(embed=blackjack_embed(game), view=view)
    view.message = await interaction.original_response()


# ----- CS2-style cases -----
def case_list_embed():
    embed = econ_embed(
        "📦 Cases",
        "Odds are the real in-game ones: 🟦 79.92%  🟪 15.98%  🌸 3.2%  🟥 0.64%  🟨 0.26%\n"
        f"Open with `/open`. Cases pay back about {int(CASE_RTP * 100)}% of their price on average.",
    )
    for c in CASES.values():
        best = max((v, n) for lst in c["items"].values() for n, v in lst)
        embed.add_field(name=f"{c['emoji']} {c['name']}",
                        value=f"Price: **{money(c['price'])}**\nTop prize: {best[1]}", inline=False)
    return embed


@bot.tree.command(name="cases", description="See the cases you can open")
@app_commands.guild_only()
async def cases_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=case_list_embed())


class CaseResultView(discord.ui.View):
    def __init__(self, owner_id, case_key, item_id, value):
        super().__init__(timeout=90)
        self.owner_id, self.case_key, self.item_id = owner_id, case_key, item_id
        self.message = None
        self.sell_btn.label = f"Sell for {value:,}"

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ That's not your drop.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Sell", emoji="💰", style=discord.ButtonStyle.success)
    async def sell_btn(self, interaction, button):
        g, u = interaction.guild.id, interaction.user.id
        got = econ.sell_item(g, u, self.item_id)
        if got is None:
            await interaction.response.send_message("❌ You already sold that.", ephemeral=True)
            return
        button.disabled, button.label = True, "Sold"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"💰 Sold for **{money(got)}**. Balance: {money(econ.balance(g, u))}", ephemeral=True)

    @discord.ui.button(label="Open again", emoji="🔁", style=discord.ButtonStyle.primary)
    async def again_btn(self, interaction, button):
        await open_case_flow(interaction, self.case_key, 1)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


def reel_embed(title, window):
    mid = len(window) // 2
    lines = []
    for j, (name, rar) in enumerate(window):
        tag = f"{RARITIES[rar][2]} {name}"
        lines.append(f"➡️ **{tag}** ⬅️" if j == mid else f"▫️ {tag}")
    return spin_embed(title, "\n".join(lines), RARITIES[window[mid][1]][1])


def spin_embed(title, description, color=POTATO_COLOR):
    return econ_embed(title, description, color)


def drop_embed(item, case, item_id, opening=False):
    name, color, emoji = RARITIES[item["rarity"]][0], RARITIES[item["rarity"]][1], RARITIES[item["rarity"]][2]
    embed = discord.Embed(title=f"{emoji} {name}", description=f"**{display_name(item)}**", color=color)
    embed.add_field(name="Float", value=f"`{item['wear_float']:.4f}`")
    embed.add_field(name="Value", value=money(item["value"]))
    embed.add_field(name="Item ID", value=f"`#{item_id}`")
    embed.set_footer(text=f"{case['name']} • saved to your /inventory")
    if item["rarity"] == "gold":
        embed.description = "🎉🎉 **RARE SPECIAL ITEM!** 🎉🎉\n" + embed.description
    return embed


async def open_case_flow(interaction, case_key, count):
    case = CASES[case_key]
    g, u = interaction.guild.id, interaction.user.id
    cost = case["price"] * count
    if not econ.spend(g, u, cost):
        await interaction.response.send_message(
            f"❌ {count} × {case['name']} costs **{money(cost)}** and you have {money(econ.balance(g, u))}.", ephemeral=True)
        return
    drops = []
    for _ in range(count):  # rolled and saved BEFORE any animation, so nothing is lost if Discord hiccups
        item = roll_item(case_key)
        drops.append((item, econ.add_item(g, u, item)))

    if count > 1:
        drops.sort(key=lambda d: -d[0]["value"])
        total = sum(d[0]["value"] for d in drops)
        lines = [f"{RARITIES[i['rarity']][2]} {md_safe(display_name(i, short=True))} — {money(i['value'])} `#{iid}`"
                 for i, iid in drops]
        best = drops[0][0]
        net = total - cost
        embed = econ_embed(f"{case['emoji']} Opened {count}× {case['name']}", "\n".join(lines),
                           RARITIES[best["rarity"]][1])
        embed.add_field(name="Spent", value=money(cost))
        embed.add_field(name="Drops worth", value=money(total))
        embed.add_field(name="Net", value=f"{'+' if net >= 0 else '-'}{abs(net):,} {CURRENCY}")
        gold_link = CASE_GOLD_GIF if CASE_GOLD_GIF and any(d[0]["rarity"] == "gold" for d in drops) else None
        await interaction.response.send_message(content=gold_link, embed=embed)
        return

    item, item_id = drops[0]
    title = f"{case['emoji']} Opening {case['name']}..."
    reel = build_reel(case_key, item["name"], item["rarity"])
    await interaction.response.send_message(content=CASE_SPIN_GIF or None, embed=spin_embed(title, "🔑 *unlocking...*"))
    for k, delay in enumerate(SPIN_DELAYS):  # quick at first, each frame a little slower than the last
        await asyncio.sleep(delay)
        await interaction.edit_original_response(embed=reel_embed(title, reel[k:k + REEL_WINDOW]))
    await asyncio.sleep(1.0)  # the reel has stopped on the drop; hold it for a beat, then reveal (GIF disappears)
    view = CaseResultView(u, case_key, item_id, item["value"])
    gold_link = CASE_GOLD_GIF if item["rarity"] == "gold" and CASE_GOLD_GIF else None
    # content=None removes the spin GIF link, so the GIF disappears; a gold drop swaps in the gold GIF link instead
    await interaction.edit_original_response(content=gold_link, embed=drop_embed(item, case, item_id), view=view)
    view.message = await interaction.original_response()


@bot.tree.command(name="open", description="Open a CS2-style case")
@app_commands.describe(case_name="Which case", count=f"How many to open at once (1-{MAX_CASES_AT_ONCE})")
@app_commands.rename(case_name="case")
@app_commands.choices(case_name=CASE_CHOICES)
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 3.0)
async def open_cmd(interaction: discord.Interaction, case_name: str,
                   count: app_commands.Range[int, 1, MAX_CASES_AT_ONCE] = 1):
    await open_case_flow(interaction, case_name, count)


@bot.tree.command(name="inventory", description="Your skins, most valuable first")
@app_commands.describe(page="Page number", user="Whose inventory (default: yours)")
@app_commands.guild_only()
async def inventory_cmd(interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1,
                        user: Optional[discord.Member] = None):
    target = user or interaction.user
    g, per = interaction.guild.id, 12
    count, value = econ.inventory_stats(g, target.id)
    if not count:
        await interaction.response.send_message(f"📭 {md_safe(target.display_name)} has no skins yet. Try `/open`!", ephemeral=True)
        return
    pages = (count + per - 1) // per
    page = min(page, pages)
    rows = econ.items(g, target.id, per, (page - 1) * per)
    lines = [f"`#{r['id']}` {RARITIES[r['rarity']][2]} {md_safe(display_name(r, short=True))} — {money(r['value'])}"
             for r in rows]
    embed = econ_embed(f"🎒 {md_safe(target.display_name)}'s inventory", "\n".join(lines))
    embed.set_footer(text=f"{count} item(s) worth {value:,} potatoes • page {page}/{pages} • sell with /sell <id>")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="sell", description="Sell one skin from your inventory")
@app_commands.describe(item_id="The # number shown in /inventory")
@app_commands.guild_only()
async def sell_cmd(interaction: discord.Interaction, item_id: int):
    g, u = interaction.guild.id, interaction.user.id
    got = econ.sell_item(g, u, item_id)
    if got is None:
        await interaction.response.send_message("❌ You don't have an item with that ID.", ephemeral=True)
        return
    await interaction.response.send_message(f"💰 Sold for **{money(got)}**. Balance: {money(econ.balance(g, u))}")


@bot.tree.command(name="sellall", description="Sell all skins of a rarity (red and gold are never sold in bulk)")
@app_commands.describe(tier="Which skins to sell")
@app_commands.choices(tier=[
    app_commands.Choice(name="Mil-Spec (blue)", value="blue"),
    app_commands.Choice(name="Restricted (purple)", value="purple"),
    app_commands.Choice(name="Classified (pink)", value="pink"),
    app_commands.Choice(name="All blue + purple", value="bluepurple"),
])
@app_commands.guild_only()
async def sellall_cmd(interaction: discord.Interaction, tier: str):
    g, u = interaction.guild.id, interaction.user.id
    rarities = ["blue", "purple"] if tier == "bluepurple" else [tier]
    count, total = econ.sell_rarities(g, u, rarities)
    if not count:
        await interaction.response.send_message("❌ You have nothing like that to sell.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"💰 Sold **{count}** skin(s) for **{money(total)}**. Balance: {money(econ.balance(g, u))}")


# ----- shop: a mall front page, and one store per category -----
SHOP_CHOICES = [app_commands.Choice(name=f"{i['emoji']} {i['name']} - {i['price']:,}", value=k) for k, i in SHOP.items()]

STORES = {  # category -> how the store is presented. Every store also has its own slash command.
    "weapons": {"cmd": "gunstore", "label": "Gun Store", "emoji": "🔫",
                "intro": "No questions asked. A stronger weapon wins a robbery, the same tier is a 50/50 gunfight."},
    "estate": {"cmd": "realestate", "label": "Real Estate", "emoji": "🏠",
               "intro": "Flex on everyone. Every property also pays rent on each `/work`."},
    "cars": {"cmd": "dealership", "label": "Dealership", "emoji": "🚗",
             "intro": "Pure flex... except the Maybach, which has hydraulics and a `/bounce-flex` for you."},
    "business": {"cmd": "businesses", "label": "Business Center", "emoji": "💼",
                 "intro": f"You can own {MAX_BUSINESSES}. Car washes and nightclubs launder dirty potatoes, "
                          "the big ones pay passive income (cash it out with `/collect`)."},
}


def shop_embed(g, u, page="home"):
    owned = econ.owned(g, u)
    balance = money(econ.balance(g, u))
    if page == "home":
        embed = econ_embed("🛒 Potato Mall",
                           f"Your balance: **{balance}**\nPick a store with the buttons below, or jump straight in with its command.")
        for cat, st in STORES.items():
            keys = [k for k, it in SHOP.items() if it["category"] == cat]
            prices = [SHOP[k]["price"] for k in keys]
            have = sum(1 for k in keys if k in owned)
            limit = MAX_BUSINESSES if cat == "business" else len(keys)
            embed.add_field(name=f"{st['emoji']} {st['label']}  —  /{st['cmd']}",
                            value=f"{len(keys)} items · {money(min(prices))} to {money(max(prices))}\nYou own **{have}/{limit}**",
                            inline=False)
        embed.set_footer(text="Buy with /buy  •  See what you own with /assets")
        return embed
    st = STORES[page]
    lines = []
    for key, it in SHOP.items():
        if it["category"] == page:
            tag = ("  🎒 carrying **%d**" % owned[key] if it.get("consumable") else "  ✅ **owned**") if key in owned else ""
            lines.append(f"{it['emoji']} **{it['name']}** — {money(it['price'])}{tag}\n└ {it['desc']} *({perk_text(key)})*")
    embed = econ_embed(f"{st['emoji']} {st['label']}", f"{st['intro']}\nYour balance: **{balance}**")
    embed.add_field(name="For sale", value="\n".join(lines), inline=False)
    embed.set_footer(text="Buy with /buy  •  Use the buttons to switch stores")
    return embed


class ShopView(discord.ui.View):
    """Buttons under the shop: Overview + one per store. Only the person who opened it can use them."""

    def __init__(self, owner_id, current="home"):
        super().__init__(timeout=180)
        self.owner_id, self.current, self.message = owner_id, current, None
        self._build()

    def _build(self):
        self.clear_items()
        pages = [("home", "Overview", "🛒")] + [(cat, st["label"], st["emoji"]) for cat, st in STORES.items()]
        for key, label, emoji in pages:
            style = discord.ButtonStyle.primary if key == self.current else discord.ButtonStyle.secondary
            button = discord.ui.Button(label=label, emoji=emoji, style=style)
            button.callback = self._open(key)
            self.add_item(button)

    def _open(self, key):
        async def callback(interaction):
            self.current = key
            self._build()
            await interaction.response.edit_message(embed=shop_embed(interaction.guild.id, interaction.user.id, key), view=self)
        return callback

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ That's someone else's shop window. Open your own with `/shop`.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def send_shop(interaction, page="home"):
    view = ShopView(interaction.user.id, page)
    await interaction.response.send_message(embed=shop_embed(interaction.guild.id, interaction.user.id, page), view=view)
    view.message = await interaction.original_response()


@bot.tree.command(name="shop", description="Open the potato mall: pick a store (weapons, real estate, cars, businesses)")
@app_commands.guild_only()
async def shop_cmd(interaction: discord.Interaction):
    await send_shop(interaction, "home")


@bot.tree.command(name="gunstore", description="Visit the Gun Store: weapons for robbing people")
@app_commands.guild_only()
async def gunstore_cmd(interaction: discord.Interaction):
    await send_shop(interaction, "weapons")


@bot.tree.command(name="realestate", description="Visit Real Estate: apartments, houses, penthouses and islands")
@app_commands.guild_only()
async def realestate_cmd(interaction: discord.Interaction):
    await send_shop(interaction, "estate")


@bot.tree.command(name="dealership", description="Visit the Dealership: cars, including the Bouncing Maybach")
@app_commands.guild_only()
async def dealership_cmd(interaction: discord.Interaction):
    await send_shop(interaction, "cars")


@bot.tree.command(name="businesses", description="Visit the Business Center: laundering fronts and passive income")
@app_commands.guild_only()
async def businesses_cmd(interaction: discord.Interaction):
    await send_shop(interaction, "business")


@bot.tree.command(name="buy", description="Buy something from the shop")
@app_commands.describe(item="What to buy")
@app_commands.choices(item=SHOP_CHOICES)
@app_commands.guild_only()
async def buy_cmd(interaction: discord.Interaction, item: str):
    g, u = interaction.guild.id, interaction.user.id
    it = SHOP[item]
    wait = econ.lay_low_left(g, u)
    if wait:
        await interaction.response.send_message(
            f"🤫 You must lay low for a while. You can't buy anything for another **{fmt_wait(wait)}**.", ephemeral=True)
        return
    result = econ.buy_asset(g, u, item, it["price"])
    if result == "owned":
        msg = (f"❌ You're already carrying the maximum ({MAX_PEELERS}) **{it['name']}s**." if it.get("consumable")
               else f"❌ You already own the **{it['name']}**.")
        await interaction.response.send_message(msg, ephemeral=True)
        return
    if result == "limit":
        await interaction.response.send_message(f"❌ You can only own **{MAX_BUSINESSES}** businesses.", ephemeral=True)
        return
    if result == "poor":
        missing = it["price"] - econ.balance(g, u)
        await interaction.response.send_message(
            f"❌ The **{it['name']}** costs {money(it['price'])}. You need **{money(missing)}** more.", ephemeral=True)
        return
    note = f"\n{perk_text(item).capitalize()}."
    if item == "maybach":
        note += " Try `/bounce-flex`!"
    await interaction.response.send_message(embed=econ_embed(
        f"{it['emoji']} Purchased: {it['name']}",
        f"You paid **{money(it['price'])}**.{note}\nBalance: {money(econ.balance(g, u))}", 0x57F287))


@bot.tree.command(name="assets", description="See the weapons, properties, cars and businesses someone owns")
@app_commands.describe(user="Whose assets (default: yours)")
@app_commands.guild_only()
async def assets_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    g = interaction.guild.id
    owned = econ.owned(g, target.id)
    if not owned:
        await interaction.response.send_message(
            f"📭 {md_safe(target.display_name)} doesn't own anything yet. Check the `/shop`!", ephemeral=True)
        return
    embed = econ_embed(f"🏆 {md_safe(target.display_name)}'s assets", None)
    for cat, (emoji, label) in SHOP_CATEGORIES.items():
        lines = [f"{it['emoji']} **{it['name']}**" + (f" ×{owned[key]}" if owned[key] > 1 else "")
                 for key, it in SHOP.items() if it["category"] == cat and key in owned]
        if lines:
            embed.add_field(name=f"{emoji} {label}", value="\n".join(lines), inline=False)
    notes = []
    rent = econ.property_rent(g, target.id)
    if rent:
        notes.append(f"Rent: +{rent:,} potatoes on every /work")
    if notes:
        embed.set_footer(text="  •  ".join(notes))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="bounce-flex", description=f"Flex your Bouncing Maybach for {BOUNCE_REWARD} potatoes (once an hour)")
@app_commands.guild_only()
async def bounce_flex_cmd(interaction: discord.Interaction):
    g, u = interaction.guild.id, interaction.user.id
    status, wait = econ.claim_bounce(g, u)
    if status == "noasset":
        await interaction.response.send_message(
            f"🚫 You need to own the **Bouncing Maybach** ({money(SHOP['maybach']['price'])}). Check the `/shop`!", ephemeral=True)
        return
    if status == "cooldown":
        await interaction.response.send_message(
            f"⏳ The hydraulics are cooling down. Flex again in **{fmt_wait(wait)}**.", ephemeral=True)
        return
    embed = econ_embed("🚘 BOUNCE FLEX",
                       f"**{md_safe(interaction.user.display_name)}** pulls up in the Bouncing Maybach...\n"
                       f"💸 **+{money(BOUNCE_REWARD)}**\nBalance: {money(econ.balance(g, u))}", 0xFFD700)
    await interaction.response.send_message(content=BOUNCE_GIF or None, embed=embed)  # the link becomes the GIF


# ----- crime: robbing, dirty potatoes, laundering -----
@bot.tree.command(name="rob", description="Rob a player with your best weapon, then lay low for 15 minutes")
@app_commands.describe(user="Who to rob")
@app_commands.guild_only()
async def rob_cmd(interaction: discord.Interaction, user: discord.Member):
    g, u = interaction.guild.id, interaction.user.id
    if user.bot or user.id == u:
        await interaction.response.send_message("❌ Pick another (human) player.", ephemeral=True)
        return
    res = econ.rob(g, u, user.id)
    status = res["status"]
    if status == "laylow":
        await interaction.response.send_message(
            f"🤫 You must lay low for a while. ({fmt_wait(res['wait'])} left)", ephemeral=True)
        return
    if status == "noweapon":
        await interaction.response.send_message(
            "❌ You need a weapon to rob someone. Check the `/shop` (the Potato Peeler is cheap).", ephemeral=True)
        return
    if status == "broke":
        await interaction.response.send_message(
            f"❌ **{md_safe(user.display_name)}** has almost nothing worth stealing.", ephemeral=True)
        return
    rw = SHOP[res["rkey"]]
    vw = SHOP[res["vkey"]] if res["vkey"] else None
    robber, victim = md_safe(interaction.user.display_name), md_safe(user.display_name)
    amount = money(res["amount"])
    outcome = res["outcome"]
    if outcome == "win":
        title, color = "🔫 Robbery successful", 0x57F287
        text = (f"**{robber}** held up **{victim}** with a **{rw['name']}** "
                f"({'they had a ' + vw['name'] if vw else 'they had no weapon'}).\n"
                f"You grabbed **{amount}**. It's 🧼 dirty, so `/launder` it.")
    elif outcome == "fight_win":
        title, color = "💥 Gunfight won!", 0x57F287
        text = (f"**{victim}** had a **{vw['name']}**, the same tier as your **{rw['name']}**. "
                f"A 50/50 gunfight... you won!\nYou took **{amount}**. It's 🧼 dirty, so `/launder` it.")
    elif outcome == "fight_lose":
        title, color = "💥 Gunfight lost", 0xED4245
        text = (f"**{victim}** had a **{vw['name']}**, the same tier as your **{rw['name']}**. "
                f"A 50/50 gunfight... you lost!\n" + (f"**{victim}** took **{amount}** from you." if res["amount"]
                                                       else "You had nothing to lose, this time."))
    else:
        title, color = "😬 Reverse robbed!", 0xED4245
        text = (f"**{victim}** pulled out a **{vw['name']}**, stronger than your **{rw['name']}**!\n"
                + (f"You got reverse robbed and lost **{amount}**." if res["amount"]
                   else "You had nothing to lose, this time."))
    notes = ["🤫 You must lay low for a while: no working or buying for 15 minutes."]
    if res["rkey"] == "peeler":
        notes.append("✂️ Your Potato Peeler is used up.")
    embed = econ_embed(title, text, color)
    embed.set_footer(text="  ".join(notes))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="launder", description="Wash dirty potatoes into clean ones (needs a car wash or a nightclub)")
@app_commands.describe(amount="A number, 1k, half or all (default: all)")
@app_commands.guild_only()
async def launder_cmd(interaction: discord.Interaction, amount: str = "all"):
    g, u = interaction.guild.id, interaction.user.id
    dirty = econ.dirty_balance(g, u)
    if dirty <= 0:
        await interaction.response.send_message("🧼 You don't have any dirty potatoes. Robberies make them.", ephemeral=True)
        return
    want = parse_amount(amount, dirty)
    if want is None or want < 1:
        await interaction.response.send_message("❌ Use a number, `1k`, `half` or `all`.", ephemeral=True)
        return
    status, info = econ.launder(g, u, want)
    if status == "nobiz":
        await interaction.response.send_message(
            f"❌ You need a laundering business: the **{SHOP['carwash']['name']}** or the **{SHOP['nightclub']['name']}**. "
            "See `/shop business`.", ephemeral=True)
        return
    if status == "capped":
        await interaction.response.send_message(
            f"⏳ Your businesses already washed their daily limit ({info['per_day']:,} {CURRENCY}). "
            f"Try again in **{fmt_wait(info['wait'])}**.", ephemeral=True)
        return
    if status != "ok":
        await interaction.response.send_message("❌ Nothing to wash.", ephemeral=True)
        return
    await interaction.response.send_message(embed=econ_embed(
        "🧼 Laundered",
        f"Washed **{money(info['washed'])}** of dirty potatoes.\n"
        f"The {round(info['fee'] * 100)}% cut: **{money(info['cut'])}**\n"
        f"Clean potatoes received: **{money(info['clean'])}**\n"
        f"Left to wash today: {money(info['remaining'])}\nBalance: {money(econ.balance(g, u))}", 0x57F287))


@bot.tree.command(name="collect", description="Collect the passive income from your businesses")
@app_commands.guild_only()
async def collect_cmd(interaction: discord.Interaction):
    g, u = interaction.guild.id, interaction.user.id
    status, paid, rate, hours = econ.collect_income(g, u)
    if status == "nobiz":
        await interaction.response.send_message(
            f"❌ You don't own an income business. The **{SHOP['factory']['name']}** starts at {money(SHOP['factory']['price'])}. "
            "See `/shop business`.", ephemeral=True)
        return
    if status == "empty":
        await interaction.response.send_message("⏳ Nothing to collect yet. Come back in a bit!", ephemeral=True)
        return
    full = " (storage was full: it only holds 24 hours)" if hours > INCOME_CAP_HOURS else ""
    await interaction.response.send_message(
        f"💼 Collected **{money(paid)}** from your businesses ({min(hours, INCOME_CAP_HOURS):.1f}h at "
        f"{rate:,} {CURRENCY}/hour){full}. Balance: {money(econ.balance(g, u))}")


# ----- admin -----
# ----- admin -----
async def admin_adjust(interaction, user, amount, give):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("🚫 Admins only.", ephemeral=True)
        return
    g = interaction.guild.id
    before = econ.balance(g, user.id)
    econ.give(g, user.id, amount if give else -amount)
    after = econ.balance(g, user.id)
    verb = f"gave **{money(after - before)}** to" if give else f"took **{money(before - after)}** from"
    await interaction.response.send_message(f"🛠️ {interaction.user.mention} {verb} {user.mention}. "
                                            f"Their balance: **{money(after)}**")


@bot.tree.command(name="cmda-give", description="Admin: give potatoes to a player")
@app_commands.describe(user="Who gets them", amount="How many potatoes")
@app_commands.guild_only()
async def cmda_give(interaction: discord.Interaction, user: discord.Member,
                    amount: app_commands.Range[int, 1, 1_000_000_000]):
    await admin_adjust(interaction, user, amount, give=True)


@bot.tree.command(name="cmda-take", description="Admin: take potatoes from a player")
@app_commands.describe(user="Who to take from", amount="How many potatoes")
@app_commands.guild_only()
async def cmda_take(interaction: discord.Interaction, user: discord.Member,
                    amount: app_commands.Range[int, 1, 1_000_000_000]):
    await admin_adjust(interaction, user, amount, give=False)


@bot.tree.command(name="cmda-item", description="Admin: give any shop item to a Discord user ID")
@app_commands.describe(user_id="The user's Discord ID (an @mention works too)", item="Which item to give")
@app_commands.choices(item=SHOP_CHOICES)
@app_commands.guild_only()
async def cmda_item(interaction: discord.Interaction, user_id: str, item: str):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("🚫 Admins only.", ephemeral=True)
        return
    digits = re.sub(r"\D", "", user_id)
    if not 15 <= len(digits) <= 20:
        await interaction.response.send_message("❌ That doesn't look like a Discord user ID.", ephemeral=True)
        return
    uid, it = int(digits), SHOP[item]
    status = econ.grant_asset(interaction.guild.id, uid, item)
    msg = {
        "ok": f"🎁 Gave **{it['name']}** to <@{uid}>.",
        "owned": f"❌ <@{uid}> already has the **{it['name']}**.",
        "limit": f"❌ <@{uid}> already owns {MAX_BUSINESSES} businesses.",
    }[status]
    await interaction.response.send_message(msg, ephemeral=(status != "ok"))


@bot.command()
@is_admin()
async def ffgive(ctx, member: discord.Member, amount: int):
    """Give (or take, with a negative number) potatoes from a player."""
    econ.give(ctx.guild.id, member.id, amount)
    await reply(ctx, f"{CURRENCY} {member.display_name} now has **{money(econ.balance(ctx.guild.id, member.id))}**.")


@bot.command()
@is_admin()
async def ffsync(ctx):
    """Re-register the slash commands with Discord and list them (shows the exact error if Discord refuses)."""
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            where = f"to server {GUILD_ID} (instant)"
        else:
            synced = await bot.tree.sync()
            where = "globally (can take a few minutes to show up)"
        names = ", ".join(f"/{c.name}" for c in synced)
        await reply(ctx, f"✅ Synced **{len(synced)}** slash command(s) {where}:\n{names}"[:1900])
    except Exception as e:
        await reply(ctx, f"❌ Sync failed: `{type(e).__name__}`\n```{str(e)[:1500]}```")



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
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id}) in {len(bot.guilds)} server(s)")
    if not bot.guilds:
        print("⚠️ The bot isn't in any server. Re-invite it with the 'bot' and 'applications.commands' scopes.")


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
    else:
        print(f"🎵 yt-dlp {ytdlp_version()} | JS runtime: {JS_STATUS} | ejs: {'yes' if ejs_installed() else 'NO'}")
        if JS_STATUS.startswith("MISSING"):
            print("⚠️ No usable JavaScript runtime: YouTube will NOT work. Add `deno` to requirements.txt and redeploy.")
        if not ejs_installed():
            print("⚠️ yt-dlp-ejs is missing: install with  pip install -U \"yt-dlp[default]\"")
        if not ffmpeg_available():
            print("⚠️ ffmpeg wasn't found: music can't play until it's installed (see requirements.txt / Dockerfile)")

    if not os.path.exists(COOKIE_PATH):
        print("⚠️ No YouTube cookies found (set YOUTUBE_COOKIES): YouTube will probably block this host.")
    if spotipy is None:
        print("ℹ️ spotipy isn't installed: Spotify links use the embed-page fallback (pip install spotipy)")
    elif not _spotipy_configured():
        print("ℹ️ SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET not set: Spotify links use the embed-page fallback")

    print(f"🧠 Models: {' > '.join(HUNT_LIST)} | web search: {'on' if WEB_SEARCH else 'off'} | clock: {realtime_context()}")

    # Cloudflare sometimes bans a shared Render IP for a while (Error 1015). Crashing makes Render
    # restart instantly and hammer Discord again, so wait (the keep-alive server stays up), then restart cleanly.
    RETRY_SECONDS = int(os.getenv("LOGIN_RETRY_SECONDS", "600"))
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("FATAL ERROR: Discord rejected DISCORD_TOKEN (wrong or reset token).")
        raise SystemExit(1)
    except discord.PrivilegedIntentsRequired:
        print("FATAL ERROR: turn on MESSAGE CONTENT INTENT in the Discord Developer Portal (Bot tab), then redeploy.")
        raise SystemExit(1)
    except discord.HTTPException as e:
        if e.status == 429:
            print(f"⏳ Discord is rate limiting this host's IP (Cloudflare 1015). Retrying in {RETRY_SECONDS // 60} min...")
            time.sleep(RETRY_SECONDS)
            os.execv(sys.executable, [sys.executable] + sys.argv)  # fresh restart, same process
        print(f"FATAL ERROR: HTTP {e.status} {str(e.text)[:200]}")
        raise SystemExit(1)
    except Exception as e:
        print(f"FATAL ERROR: {type(e).__name__} {str(e)[:200]}")
        raise SystemExit(1)
