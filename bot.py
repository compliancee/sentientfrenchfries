import os, discord, psutil, time, sys, threading, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from discord.ext import commands
from openai import OpenAI
from dotenv import load_dotenv

# --- CONFIG ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 8000))

# --- AI SETUP ---
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
HUNT_LIST = ["google/gemini-2.0-flash-001", "meta-llama/llama-3.3-70b-instruct"]

# --- GLOBAL STATE ---
memory_cache = {}
blacklist = []
error_logs = []
start_time = time.time()
water_count = 0
is_dead = False
ai_temp = 0.7  # AI Randomness

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# --- FAKE WEB SERVER ---
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Potato Node Active")
    def log_message(self, format, *args): return

def run_web_server():
    HTTPServer(('0.0.0.0', PORT), HealthCheck).serve_forever()

# --- AI IDENTITY ---
def get_identity():
    if water_count == 0: mood = "salty, grumpy, and cynical."
    elif water_count <= 50: mood = "neutral and slightly hydrated."
    else: mood = "extremely sweet, happy, and loving."
    
    return (
        f"You are 'ilovefrenchfries', a sentient potato. Be {mood}. "
        f"Keep responses between 15 and 150 words. NEVER mention being an AI."
    )

# --- PUBLIC COMMANDS ---
@bot.command(aliases=['c'])
async def chat(ctx, *, message):
    if is_dead or ctx.author.id in blacklist: return
    async with ctx.typing():
        history = memory_cache.get(ctx.channel.id, [])
        res = None
        for model in HUNT_LIST:
            try:
                msgs = [{"role": "system", "content": get_identity()}] + history + [{"role": "user", "content": message}]
                comp = client.chat.completions.create(model=model, messages=msgs, temperature=ai_temp)
                res = comp.choices[0].message.content
                if res: break
            except Exception as e: error_logs.append(f"{model}: {str(e)[:30]}")
        if res:
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": res})
            memory_cache[ctx.channel.id] = history[-6:]
            await ctx.send(res)

@bot.command()
async def ffw(ctx):
    global water_count, is_dead
    if is_dead: return await ctx.send("💀 I'm a dead starch-mush. Use `!a ffrsr`.")
    water_count += 1
    if water_count > 100:
        is_dead = True
        await ctx.send("🌊 **OVERWATERED!** I died.")
    else:
        await ctx.send(f"💧 Hydration: `{water_count}/100`.")

@bot.command()
async def mood(ctx):
    m = "Salty" if water_count == 0 else "Hydrated" if water_count <= 50 else "Blooming"
    await ctx.send(f"🥔 **Current Mood:** {m} ({water_count}/100 water)")

@bot.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

@bot.command()
async def cmdzz(ctx):
    embed = discord.Embed(title="🍟 Potato Help Menu", color=0xffcc00)
    embed.add_field(name="Commands", value="`!c [msg]` - Talk\n`!ffw` - Water Me\n`!mood` - Check Mood\n`!ping` - Latency\n`!clear` - Wipe Chat", inline=False)
    if ctx.author.id == ADMIN_ID:
        embed.set_footer(text="Admin Detected: Use !cmdadm for God Mode")
    await ctx.send(embed=embed)

# --- 25 FEATURE ADMIN PANEL ---
@bot.command()
async def cmdadm(ctx):
    if ctx.author.id != ADMIN_ID: return
    e = discord.Embed(title="🔐 Potato God Mode (25 Features)", color=0xff0000)
    e.add_field(name="Systems", value="1. `!a st` - Stats\n2. `!a logs` - API Logs\n3. `!a k` - Kill Bot\n4. `!a rb` - Reboot\n5. `!a cll` - Clear Logs", inline=True)
    e.add_field(name="Potato State", value="6. `!a ffrsr` - Revive\n7. `!a sw [num]` - Set Water\n8. `!a die` - Force Death\n9. `!a live` - Force Live\n10. `!a dry` - 0 Water", inline=True)
    e.add_field(name="AI Tuning", value="11. `!a temp [num]` - AI Random\n12. `!a hunt` - Show Models\n13. `!clear` - Purge Context\n14. `!a brain` - Cache Info\n15. `!a mfull` - Reset All Memory", inline=True)
    e.add_field(name="Users", value="16. `!a bl [id]` - Blacklist\n17. `!a wl [id]` - Un-Blacklist\n18. `!a bll` - List Banned\n19. `!a bc [msg]` - Global Msg\n20. `!a ls` - Server List", inline=True)
    e.add_field(name="Debug", value="21. `!a p` - High-Res Ping\n22. `!a mem` - RAM Usage\n23. `!a cpu` - CPU Load\n24. `!a node` - Port Check\n25. `!a up` - Total Uptime", inline=True)
    await ctx.send(embed=e)

@bot.group(aliases=['a'])
async def admin(ctx):
    if ctx.author.id != ADMIN_ID: return

@admin.command()
async def st(ctx): await ctx.send(f"📊 CPU: {psutil.cpu_percent()}% | RAM: {psutil.virtual_memory().percent}%")
@admin.command()
async def ffrsr(ctx): 
    global water_count, is_dead
    water_count, is_dead = 0, False
    await ctx.send("🪄 Resurrected.")
@admin.command()
async def logs(ctx): await ctx.send(f"📋 Logs:\n```{chr(10).join(error_logs[-5:]) or 'None'}```")
@admin.command()
async def sw(ctx, n: int):
    global water_count; water_count = n
    await ctx.send(f"💧 Water set to {n}")
@admin.command()
async def temp(ctx, t: float):
    global ai_temp; ai_temp = t
    await ctx.send(f"🌡️ AI Temperature set to {t}")
@admin.command()
async def bl(ctx, user_id: int): 
    blacklist.append(user_id); await ctx.send(f"🚫 {user_id} Banned.")
@admin.command()
async def wl(ctx, user_id: int):
    if user_id in blacklist: blacklist.remove(user_id)
    await ctx.send(f"✅ {user_id} Freed.")
@admin.command()
async def bc(ctx, *, msg):
    for g in bot.guilds:
        try: await (g.system_channel or g.text_channels[0]).send(f"📢 **FARMER BROADCAST:** {msg}")
        except: continue
@admin.command()
async def die(ctx): global is_dead; is_dead = True; await ctx.send("💀 Forced death.")
@admin.command()
async def live(ctx): global is_dead; is_dead = False; await ctx.send("🌱 Forced life.")
@admin.command()
async def dry(ctx): global water_count; water_count = 0; await ctx.send("🌵 Dried up.")
@admin.command()
async def rb(ctx): await ctx.send("♻️ Rebooting..."); os.execv(sys.executable, ['python'] + sys.argv)
@admin.command()
async def k(ctx): await ctx.send("🔌 Killing..."); await bot.close(); sys.exit()
@admin.command()
async def hunt(ctx): await ctx.send(f"🎯 Models: `{HUNT_LIST}`")
@admin.command()
async def brain(ctx): await ctx.send(f"🧠 Contexts Cached: `{len(memory_cache)}`")
@admin.command()
async def mfull(ctx): global memory_cache; memory_cache = {}; await ctx.send("🧹 Total memory wipe.")
@admin.command()
async def cll(ctx): global error_logs; error_logs = []; await ctx.send("📋 Logs cleared.")
@admin.command()
async def bll(ctx): await ctx.send(f"🚫 Banned IDs: `{blacklist}`")
@admin.command()
async def ls(ctx): await ctx.send(f"🏢 Servers: `{len(bot.guilds)}` - `{[g.name for g in bot.guilds]}`")
@admin.command()
async def p(ctx): await ctx.send(f"🛰️ API Latency: `{bot.latency * 1000:.2f}ms`")
@admin.command()
async def mem(ctx): await ctx.send(f"💾 RAM: `{psutil.Process().memory_info().rss / 1024 / 1024:.2f}MB`")
@admin.command()
async def cpu(ctx): await ctx.send(f"⚙️ CPU: `{psutil.cpu_percent()}%` over 1s")
@admin.command()
async def node(ctx): await ctx.send(f"🌐 Fake Web Node listening on port `{PORT}`")
@admin.command()
async def up(ctx): await ctx.send(f"⏱️ Uptime: `{round(time.time() - start_time)}s`")

@bot.command()
async def clear(ctx):
    if ctx.author.id == ADMIN_ID:
        memory_cache[ctx.channel.id] = []
        await ctx.send("🧹 Channel memory cleared.")

# --- LAUNCH ---
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)
