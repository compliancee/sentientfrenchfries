import os
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import discord
from discord.ext import commands
from openai import AsyncOpenAI
from dotenv import load_dotenv

# --- CONFIG & ENVIRONMENT ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
# Render uses port 10000 by default if PORT is not set
PORT = int(os.getenv("PORT", 10000))

# --- ASYNC OPENROUTER SETUP ---
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

HUNT_LIST = [
    "google/gemini-2.0-flash-001", 
    "meta-llama/llama-3.3-70b-instruct", 
    "deepseek/deepseek-chat"
]

# --- GLOBAL STATE ---
memory_cache = {}
error_logs = []
water_count = 0
is_dead = False

# --- DISCORD SETUP ---
intents = discord.Intents.default()
intents.message_content = True
# Added presence intent to ensure the bot shows as online if enabled in portal
intents.presences = True 
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# --- RENDER HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Potato Node: Online")

    def log_message(self, format, *args):
        return # Silence logs to keep Render console clean

def run_web_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    print(f"🌍 Render Health Check active on port {PORT}")
    server.serve_forever()

# --- POTATO LOGIC ---
def get_identity():
    if water_count == 0:
        mood = "neutral and salty. You are grumpy and cynical."
    elif water_count <= 50:
        mood = "pleasant and slightly hydrated."
    else:
        mood = "extremely happy, sweet, and overflowing with gratitude."

    return (
        f"You are 'ilovefrenchfries', a sentient potato. Be {mood}. "
        f"Response length: 5-400 words. Never admit to being an AI."
    )

# --- COMMANDS ---

@bot.command(aliases=['c'])
async def chat(ctx, *, message: str):
    global is_dead
    if is_dead:
        await ctx.send("... (The potato is dead.)")
        return

    async with ctx.typing():
        history = memory_cache.get(ctx.channel.id, [])
        response_text = None
        
        # Fallback loop through HUNT_LIST
        for model_id in HUNT_LIST:
            try:
                system_msg = {"role": "system", "content": get_identity()}
                messages = [system_msg] + history + [{"role": "user", "content": message}]
                
                # Using AsyncOpenAI with a timeout
                completion = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model_id, 
                        messages=messages
                    ),
                    timeout=15.0
                )
                
                response_text = completion.choices[0].message.content
                if response_text:
                    break
            except Exception as e:
                error_logs.append(f"{model_id} fail: {str(e)[:50]}")
                continue

        if response_text:
            # Update local memory
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": response_text})
            memory_cache[ctx.channel.id] = history[-6:] # Keep last 6 messages
            await ctx.send(response_text)
        else:
            await ctx.send("The cloud is a desert. Use `!logs` to see what happened.")

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

# --- FLAT ADMIN COMMANDS ---

@bot.command()
async def ffrsr(ctx):
    """Resurrect the potato (Admin Only)"""
    global water_count, is_dead
    if ctx.author.id != ADMIN_ID:
        return
    water_count, is_dead = 0, False
    await ctx.send("🪄 The potato has been resurrected!")

@bot.command()
async def logs(ctx):
    """Check recent errors (Admin Only)"""
    if ctx.author.id != ADMIN_ID:
        return
    out = "\n".join(error_logs[-5:]) or "No errors recorded."
    await ctx.send(f"📋 **Recent Error Logs:**\n```{out}```")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

# --- EXECUTION ---
if __name__ == "__main__":
    # Start web server in background thread for Render's health checks
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    # Start the Discord bot
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
