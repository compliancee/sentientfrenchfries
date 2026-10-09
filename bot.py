import os
import re
import time
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import discord
from discord.ext import commands
from openai import AsyncOpenAI
from dotenv import load_dotenv

# --- CONFIG & ENVIRONMENT ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 10000))  # Render default

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
error_logs = []
water_count = 0
is_dead = False
START_TIME = time.time()

# --- DISCORD SETUP ---
intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Developer Portal
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),  # blocks @everyone / role pings
)


# --- RENDER HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"Potato Node: Online")

    def do_HEAD(self):  # some uptime monitors use HEAD
        self._ok()

    def log_message(self, format, *args):
        return


def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    print(f"🌍 Render Health Check active on port {PORT}")
    server.serve_forever()


# --- HELPERS ---
def log_error(msg):
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    error_logs.append(entry)
    del error_logs[:-50]  # keep only the last 50
    print(f"⚠️ {entry}")


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
    return "extremely happy, sweet, and overflowing with gratitude"


def get_identity(long_mode=False):
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
    return (
        f"You are 'ilovefrenchfries', a sentient potato. Be {get_mood()}. "
        f"{length} Never admit to being an AI."
    )


def is_admin():
    """Admin-only check. Non-admins are silently ignored (see on_command_error)."""

    async def predicate(ctx):
        return ctx.author.id == ADMIN_ID

    return commands.check(predicate)


# --- USER COMMANDS ---
@bot.command(aliases=["c"])
@commands.cooldown(1, 8, commands.BucketType.user)
async def chat(ctx, *, message: str):
    if is_dead:
        await ctx.send("... (The potato is dead.)")
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
            [{"role": "system", "content": get_identity(long_mode)}]
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
            await ctx.send("The cloud is a desert. Try again in a bit.")
            return

        response_text = trim_sentences(response_text, cap)

        # Save a shortened copy so long replies don't bloat future prompts
        history.append({"role": "user", "content": user_line})
        history.append({"role": "assistant", "content": response_text[:1500]})
        memory_cache[ctx.channel.id] = history[-6:]

        for chunk in split_message(response_text)[:MAX_CHUNKS]:
            await ctx.send(chunk)


@bot.command()
async def ffw(ctx):
    global water_count, is_dead
    if is_dead:
        await ctx.send("You are watering a corpse. It's too late.")
        return

    water_count += 1
    if water_count > 100:
        is_dead = True
        await ctx.send("🌊 **OVERWATERED!** The potato died. 💀")
    else:
        await ctx.send(f"💧 Hydration: `{water_count}/100`.")


# --- ADMIN COMMANDS ---
@bot.command()
@is_admin()
async def ffrsr(ctx):
    """Resurrect the potato and reset hydration to 0."""
    global water_count, is_dead
    water_count, is_dead = 0, False
    await ctx.send("🪄 The potato has been resurrected!")


@bot.command()
@is_admin()
async def logs(ctx):
    """Show the last 5 error entries."""
    out = "\n".join(error_logs[-5:]) or "No errors recorded."
    await ctx.send(f"📋 **Recent Error Logs:**\n```{out[:1800]}```")


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
        f"Models:     {' > '.join(m.split('/')[-1] for m in HUNT_LIST)}",
    ]
    await ctx.send("🥔 **Potato Status**\n```" + "\n".join(lines) + "```")


@bot.command()
@is_admin()
async def ffsetwater(ctx, amount: int):
    """Set hydration to an exact value (0-100). Also revives the potato."""
    global water_count, is_dead
    if not 0 <= amount <= 100:
        await ctx.send("Pick a number from 0 to 100.")
        return
    water_count, is_dead = amount, False
    await ctx.send(f"💧 Hydration set to `{water_count}/100`.")


@bot.command()
@is_admin()
async def ffkill(ctx):
    """Kill the potato instantly."""
    global is_dead
    is_dead = True
    await ctx.send("💀 The potato has been... peeled.")


@bot.command()
@is_admin()
async def ffwipe(ctx, scope: str = "channel"):
    """Clear chat memory for this channel, or `all` channels."""
    if scope.lower() == "all":
        count = len(memory_cache)
        memory_cache.clear()
        await ctx.send(f"🧽 Wiped memory for {count} channel(s).")
    else:
        memory_cache.pop(ctx.channel.id, None)
        await ctx.send("🧽 The potato forgot this channel.")


@bot.command()
@is_admin()
async def ffclearlogs(ctx):
    """Clear the error log."""
    count = len(error_logs)
    error_logs.clear()
    await ctx.send(f"🗑️ Cleared {count} log entr{'y' if count == 1 else 'ies'}.")


@bot.command()
@is_admin()
async def ffsay(ctx, *, text: str):
    """Make the potato say something (deletes your command message)."""
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
            await ctx.send(f"Usage: `!ffmodel {action} provider/model-name`")
            return
        if action == "add":
            if model_id in HUNT_LIST:
                await ctx.send("That model is already in the list.")
                return
            HUNT_LIST.append(model_id)
        elif action == "first":
            if model_id in HUNT_LIST:
                HUNT_LIST.remove(model_id)
            HUNT_LIST.insert(0, model_id)
        else:  # remove
            if model_id not in HUNT_LIST:
                await ctx.send("That model isn't in the list.")
                return
            if len(HUNT_LIST) == 1:
                await ctx.send("Can't remove the last model.")
                return
            HUNT_LIST.remove(model_id)
    elif action == "reset":
        HUNT_LIST[:] = DEFAULT_MODELS
    elif action != "list":
        await ctx.send("Usage: `!ffmodel [list | add <id> | remove <id> | first <id> | reset]`")
        return

    listing = "\n".join(f"{i}. {m}" for i, m in enumerate(HUNT_LIST, 1))
    await ctx.send(f"🧠 **Model order:**\n```{listing}```")


# --- EVENTS ---
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ The potato needs a moment. Try again in {error.retry_after:.0f}s.")
    elif isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        return  # unknown command or non-admin: stay silent
    elif isinstance(error, commands.MissingRequiredArgument):
        if ctx.command.name == "chat":
            await ctx.send("Say something to the potato: `!chat hello`")
        else:
            await ctx.send(f"Usage: `!{ctx.command.name} <{error.param.name}>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("That value doesn't look right. Check the command and try again.")
    else:
        log_error(f"{ctx.command}: {error!r}")


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


# --- EXECUTION ---
if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
