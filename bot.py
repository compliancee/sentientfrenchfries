import os
import discord
import psutil
import time
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from discord.ext import commands
from openai import OpenAI
from dotenv import load_dotenv

# --- CONFIG ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 8000)) # Koyeb sends a PORT variable

# --- OPENROUTER SETUP ---
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Any model you want from OpenRouter
HUNT_LIST = [
    "google/gemini-2.0-flash-001", 
    "meta-llama/llama-3.3-70b-instruct", 
    "deepseek/deepseek-chat"
]

# --- STATE ---
memory_cache = {}
error_logs = []
start_time = time.time()
water_count = 0
is_dead = False

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# --- FAKE WEB SERVER (For Koyeb Free Tier) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Potato Node: Online")

    def log_message(self, format, *args):
        return # Silence server logs to keep console clean

def run_web_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    print(f"🌍 Koyeb Port {PORT} active.")
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

@bot.command(aliases=['c'])
async def chat(ctx, *, message):
    if is_dead:
        await ctx.send("... (The potato is dead.)")
        return

    async with ctx.typing():
        history = memory_cache.get(ctx.channel.id, [])
        res = None
        for model_id in HUNT_LIST:
            try:
                msgs = [{"role": "system", "content": get_identity()}] + history + [{"role": "user", "content": message}]
                completion = client.chat.completions.create(model=model_id, messages=msgs, timeout=15)
                res = completion.choices[0].message.content
                if res: break
            except Exception as e:
                error_logs.append(f"{model_id} fail: {str(e)[:30]}")

        if res:
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": res})
            memory_cache[ctx.channel.id] = history[-6:]
            await ctx.send(res)
        else:
            await ctx.send("The cloud is a desert. Use `!a logs`.")

@bot.command()
async def ffw(ctx):
    global water_count, is_dead
    if is_dead: return
    water_count += 1
    if water_count > 100:
        is_dead = True
        await ctx.send("🌊 **OVERWATERED!** The potato died. 💀")
    else:
        await ctx.send(f"💧 Hydration: `{water_count}/100`.")

@bot.group(aliases=['a'])
async def admin(ctx):
    if ctx.author.id != ADMIN_ID: return

@admin.command()
async def ffrsr(ctx):
    global water_count, is_dead
    water_count, is_dead = 0, False
    await ctx.send("🪄 Resurrected!")

@admin.command()
async def logs(ctx):
    out = "\n".join(error_logs[-5:]) or "No errors."
    await ctx.send(f"📋 **Logs:**\n```{out}```")

# --- LAUNCH ---
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)