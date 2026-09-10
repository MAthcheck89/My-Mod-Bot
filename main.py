import os
import re
import sqlite3
import unicodedata
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")

# Set to your #moderator-only channel ID
LOG_CHANNEL_ID = 1514864605212708944  

AUTO_MUTE_MINUTES = 15
KICK_STRIKE = 3
BAN_STRIKE = 5 

# Anti-Spam Settings
SPAM_THRESHOLD = 5
SPAM_WINDOW = 10           
SPAM_TIMEOUT_MINUTES = 30  
user_message_cache = defaultdict(lambda: deque(maxlen=10))

# Anti-Raid Settings
recent_joins = deque(maxlen=50)
RAID_JOIN_THRESHOLD = 6
RAID_WINDOW = 10

# =========================================================
# DATABASE SETUP & HELPERS
# =========================================================

def init_db():
    with sqlite3.connect("moderation.db") as db:
        cursor = db.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS strikes (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            strikes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        db.commit()

init_db()

def get_strikes(guild_id, user_id):
    with sqlite3.connect("moderation.db") as db:
        cursor = db.cursor()
        cursor.execute("SELECT strikes FROM strikes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        result = cursor.fetchone()
        return result[0] if result else 0

def add_strike(guild_id, user_id):
    current = get_strikes(guild_id, user_id)
    new_amount = current + 1

    with sqlite3.connect("moderation.db") as db:
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO strikes (guild_id, user_id, strikes)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET strikes = excluded.strikes
            """, (guild_id, user_id, new_amount))
        db.commit()

    return new_amount

def clear_strikes(guild_id, user_id):
    with sqlite3.connect("moderation.db") as db:
        cursor = db.cursor()
        cursor.execute("DELETE FROM strikes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        db.commit()

# =========================================================
# MOD LOGGING HELPER
# =========================================================

async def send_mod_log(guild, title, description, color=discord.Color.red()):
    if not LOG_CHANNEL_ID:
        return
        
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        print("Warning: Log channel not found. Check the LOG_CHANNEL_ID.")
        return
        
    embed = discord.Embed(title=title, description=description, color=color)
    try:
        await log_channel.send(embed=embed)
    except discord.Forbidden:
        print("Missing permissions to send messages in the log channel.")

# =========================================================
# BLOCKED WORDS & EMOJIS 
# =========================================================

BLOCKED_WORDS = [
    "fuck", "fucker", "fucking", "fucked", "motherfuck", "motherfucker", "fick", "fuh", "fuc", "fck",
    "shit", "shitty", "bullshit", "bitch", "bitches", "bitching",
    "asshole", "assholes", "dumbass", "jackass", "badass", "crap",
    "piss", "pissed", "dick", "dicks", "dickhead", "cock", "cocks",
    "cocksucker", "pussy", "bastard", "damn", "dammit", "hell", "ass",
    "slut", "sluts", "whore", "whores", "hoe", "hoes",
    "idiot", "idiots", "moron", "morons", "stupid", "dumbasses",
    "retard", "retarded", "nigger", "niger", "nigga", "naiger", "negger", "niager", "niga",
    "kys", "killyourself",
]

BLOCKED_EMOJIS = [
    "🍆", "💦", "😩", "😫", "🏳️‍🌈", "🏳️‍⚧️"
]

# =========================================================
# TEXT NORMALIZATION & BYPASS DETECTION
# =========================================================

def contains_blocked_word(text):
    text_lower = text.lower()

    for word in BLOCKED_WORDS:
        pattern_chars = [re.escape(char) + r"+" for char in word]
        regex_str = r"[\s.\-_,~|]*".join(pattern_chars)

        for match in re.finditer(regex_str, text_lower):
            start_pos = match.start()
            end_pos = match.end()

            if start_pos > 0 and text_lower[start_pos - 1].isalnum():
                continue
            if end_pos < len(text_lower) and text_lower[end_pos].isalnum():
                continue

            return True

    return False

# =========================================================
# DISCORD INTENTS
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

class ModBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        auto_scan_loop.start()
        print("Slash commands synchronized and auto-scan loop started.")

bot = ModBot()

@bot.event
async def on_ready():
    print("=" * 50)
    print(f"Logged in as: {bot.user}")
    print("Discord-Mod-Bot is ONLINE")
    print("=" * 50)

# =========================================================
# PERMISSION FUNCTIONS
# =========================================================

def is_moderator(member):
    permissions = member.guild_permissions
    return (permissions.administrator or permissions.manage_messages or 
            permissions.moderate_members or permissions.kick_members or permissions.ban_members)

def can_moderate(moderator, target):
    if target == moderator or target == moderator.guild.owner: return False
    if target.top_role >= moderator.top_role: return False
    return True

# =========================================================
# ANTI-SPAM & ANTI-RAID SYSTEM
# =========================================================

async def check_anti_spam(message):
    if message.author.bot or not message.guild: return False
    member = message.author
    if not isinstance(member, discord.Member) or member == message.guild.owner or is_moderator(member): return False

    current_time = time.time()
    user_id = member.id
    content = message.content

    user_message_cache[user_id].append((content, current_time))
    matching_count = sum(1 for c, t in user_message_cache[user_id] if c == content and (current_time - t) <= SPAM_WINDOW)

    if matching_count >= SPAM_THRESHOLD:
        user_message_cache[user_id].clear()
        try: await message.delete()
        except: pass

        try:
            await member.timeout(timedelta(minutes=SPAM_TIMEOUT_MINUTES), reason="Anti-spam")
        except discord.Forbidden:
            pass

        await send_mod_log(
            message.guild, "🛡️ Anti-Spam Triggered",
            f"**User:** {member.mention}\n**Action:** Timed out for {SPAM_TIMEOUT_MINUTES}m.",
            color=discord.Color.purple()
        )
        return True
    return False

@bot.event
async def on_member_join(member):
    current_time = time.time()
    recent_joins.append(current_time)
    joins_in_window = sum(1 for t in recent_joins if (current_time - t) <= RAID_WINDOW)

    if joins_in_window >= RAID_JOIN_THRESHOLD:
        await send_mod_log(member.guild, "🚨 Anti-Raid Alert!", f"High join rate: {joins_in_window} members in {RAID_WINDOW}s.", color=discord.Color.dark_red())

# =========================================================
# AUTOMATIC PUNISHMENT 
# =========================================================

async def automatic_punishment(message):
    member = message.author
    guild = message.guild

    if not isinstance(member, discord.Member) or member == guild.owner or is_moderator(member):
        return

    bad_message_content = message.content
    try: await message.delete()
    except: pass

    strikes = add_strike(guild.id, member.id)
    
    await send_mod_log(guild, "🚨 Auto-Mod: Message Deleted", f"**User:** {member.mention}\n**Strikes:** {strikes}\n**Message:** {bad_message_content}", color=discord.Color.orange())

    if strikes < KICK_STRIKE:
        try:
            await member.timeout(timedelta(minutes=AUTO_MUTE_MINUTES), reason="Automatic moderation")
            await message.channel.send(f"{member.mention}, message removed. ⚠️ **{strikes} strike(s)**.", delete_after=8)
        except discord.Forbidden:
            await send_mod_log(guild, "⚠️ Permission Error", "I lack permissions to timeout users. Check my role hierarchy.")

    elif strikes == KICK_STRIKE:
        try:
            await member.kick(reason="Automatic moderation: strike limit reached")
            await message.channel.send(f"{member.mention} reached **{KICK_STRIKE} strikes** and was kicked.", delete_after=8)
            await send_mod_log(guild, "👢 Auto-Mod: User Kicked", f"**User:** {member.mention}", color=discord.Color.red())
        except discord.Forbidden:
            await send_mod_log(guild, "⚠️ Permission Error", f"Failed to kick {member.mention}. My role is lower than theirs or I lack Kick permissions.", color=discord.Color.red())

    elif strikes >= BAN_STRIKE:
        try:
            await member.ban(reason="Automatic moderation: ban strike limit reached", delete_message_seconds=0)
            await message.channel.send(f"{member.mention} reached **{BAN_STRIKE} strikes** and was banned.", delete_after=8)
            await send_mod_log(guild, "🔨 Auto-Mod: User Banned", f"**User:** {member.mention}", color=discord.Color.dark_red())
        except discord.Forbidden:
            await send_mod_log(guild, "⚠️ Permission Error", f"Failed to ban {member.mention}. My role is lower than theirs or I lack Ban permissions.", color=discord.Color.dark_red())

# =========================================================
# MESSAGE & REACTION EVENTS
# =========================================================

@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None: return
    if contains_blocked_word(message.content):
        await automatic_punishment(message)
        return
    if await check_anti_spam(message): return
    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or not reaction.message.guild: return
    guild = reaction.message.guild
    member = guild.get_member(user.id)
    if not member or member == guild.owner or is_moderator(member): return

    emoji_str = str(reaction.emoji)
    if emoji_str in BLOCKED_EMOJIS or any(b in emoji_str for b in BLOCKED_EMOJIS):
        try: await reaction.remove(user)
        except: pass

        strikes = add_strike(guild.id, member.id)
        await send_mod_log(guild, "🚨 Auto-Mod: Reaction Removed", f"**User:** {member.mention}\n**Strikes:** {strikes}\n**Blocked Emoji:** {emoji_str}", color=discord.Color.orange())

        if strikes < KICK_STRIKE:
            try: await member.timeout(timedelta(minutes=AUTO_MUTE_MINUTES))
            except: pass
        elif strikes == KICK_STRIKE:
            try: await member.kick(reason="Reaction block")
            except: pass
        elif strikes >= BAN_STRIKE:
            try: await member.ban(reason="Reaction block", delete_message_seconds=0)
            except: pass

# =========================================================
# AUTOMATIC BACKGROUND SCAN TASK
# =========================================================

@tasks.loop(seconds=30)
async def auto_scan_loop():
    await bot.wait_until_ready()
    for guild in bot.guilds:
        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)
            if not permissions.view_channel or not permissions.read_message_history: continue
            try:
                async for message in channel.history(limit=50, oldest_first=True):
                    if message.author.bot or not contains_blocked_word(message.content): continue
                    try: await message.delete()
                    except: pass
                    
                    member = message.author
                    if not isinstance(member, discord.Member) or member == guild.owner or is_moderator(member): continue

                    strikes = add_strike(guild.id, member.id)
                    if strikes == KICK_STRIKE:
                        try: await member.kick(reason="Automated scan")
                        except: pass
                    elif strikes >= BAN_STRIKE:
                        try: await member.ban(reason="Automated scan", delete_message_seconds=0)
                        except: pass
            except: pass

# =========================================================
# STANDARD COMMANDS (Including Scan)
# =========================================================

@bot.tree.command(name="warn", description="Warn a member and give them a strike.")
async def warn(interaction, member: discord.Member, reason: str = "No reason provided"):
    if not is_moderator(interaction.user) or not can_moderate(interaction.user, member):
        return await interaction.response.send_message("❌ Invalid permissions.", ephemeral=True)
    strikes = add_strike(interaction.guild.id, member.id)
    await interaction.response.send_message(f"⚠️ **Warning issued**\nMember: {member.mention}\nStrikes: **{strikes}**")

@bot.tree.command(name="kick", description="Kick a member.")
async def kick(interaction, member: discord.Member, reason: str = "No reason provided"):
    if not interaction.user.guild_permissions.kick_members or not can_moderate(interaction.user, member):
        return await interaction.response.send_message("❌ Invalid permissions.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 {member.mention} kicked.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I lack permissions. Move my role higher.", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member.")
async def ban(interaction, member: discord.Member, reason: str = "No reason provided"):
    if not interaction.user.guild_permissions.ban_members or not can_moderate(interaction.user, member):
        return await interaction.response.send_message("❌ Invalid permissions.", ephemeral=True)
    try:
        await member.ban(reason=reason, delete_message_seconds=0)
        await interaction.response.send_message(f"🔨 {member.mention} banned.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I lack permissions. Move my role higher.", ephemeral=True)

@bot.tree.command(name="clearstrikes", description="Clear a member's strikes.")
async def clearstrikes(interaction, member: discord.Member):
    if not is_moderator(interaction.user): return await interaction.response.send_message("❌ Denied.", ephemeral=True)
    clear_strikes(interaction.guild.id, member.id)
    await interaction.response.send_message(f"✅ Cleared strikes for {member.mention}.")

@bot.tree.command(name="scan", description="Manually scan a channel for blocked words and apply punishments.")
async def scan(interaction: discord.Interaction, channel: discord.TextChannel = None, limit: int = 100):
    if not is_moderator(interaction.user):
        return await interaction.response.send_message("❌ Denied. You do not have permission to run manual scans.", ephemeral=True)

    target_channel = channel or interaction.channel
    await interaction.response.send_message(f"🔍 Scanning {target_channel.mention} (Checking last {limit} messages)...", ephemeral=True)

    deleted_count = 0
    punished_count = 0

    try:
        async for message in target_channel.history(limit=limit):
            if message.author.bot: 
                continue
                
            if contains_blocked_word(message.content):
                try:
                    await message.delete()
                    deleted_count += 1
                    
                    member = message.author
                    if isinstance(member, discord.Member) and member != interaction.guild.owner and not is_moderator(member):
                        strikes = add_strike(interaction.guild.id, member.id)
                        punished_count += 1
                        
                        if strikes == KICK_STRIKE:
                            try: await member.kick(reason="Manual scan detection")
                            except: pass
                        elif strikes >= BAN_STRIKE:
                            try: await member.ban(reason="Manual scan detection", delete_message_seconds=0)
                            except: pass
                except discord.Forbidden:
                    pass 
                    
        await interaction.followup.send(f"✅ **Scan complete in {target_channel.mention}**\n🗑️ Deleted `{deleted_count}` messages.\n⚠️ Applied strikes to `{punished_count}` users.", ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send("❌ Error: I lack permissions to read history or manage messages in that channel.", ephemeral=True)

# =========================================================
# RENDER WEB SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Discord-Mod-Bot is online!")
    def log_message(self, format, *args): pass

def run_web_server():
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), HealthHandler)
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(TOKEN)
