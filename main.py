import os
import re
import sqlite3
import unicodedata
import threading

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
BAN_STRIKE = 3


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
        cursor.execute(
            """
            SELECT strikes
            FROM strikes
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id)
        )
        result = cursor.fetchone()
        return result[0] if result else 0


def add_strike(guild_id, user_id):
    current = get_strikes(guild_id, user_id)
    new_amount = current + 1

    with sqlite3.connect("moderation.db") as db:
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO strikes
            (guild_id, user_id, strikes)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET strikes = excluded.strikes
            """,
            (guild_id, user_id, new_amount)
        )
        db.commit()

    return new_amount


def clear_strikes(guild_id, user_id):
    with sqlite3.connect("moderation.db") as db:
        cursor = db.cursor()
        cursor.execute(
            """
            DELETE FROM strikes
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id)
        )
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
# BLOCKED WORDS
# =========================================================

BLOCKED_WORDS = [
    "fuck", "fucker", "fucking", "fucked", "motherfuck", "motherfucker",
    "shit", "shitty", "bullshit", "bitch", "bitches", "bitching",
    "asshole", "assholes", "dumbass", "jackass", "badass", "crap",
    "piss", "pissed", "dick", "dicks", "dickhead", "cock", "cocks",
    "cocksucker", "pussy", "bastard", "damn", "dammit", "hell", "ass",
    "slut", "sluts", "whore", "whores", "hoe", "hoes",
    "idiot", "idiots", "moron", "morons", "stupid", "dumbasses",
    "retard", "retarded", "nigger", "niger", "nigga",
    "kys", "killyourself",
]


# =========================================================
# TEXT NORMALIZATION & BYPASS DETECTION
# =========================================================

def contains_blocked_word(text):
    text_lower = text.lower()

    for word in BLOCKED_WORDS:
        pattern_chars = [re.escape(char) + r"+" for char in word]
        regex_str = r"[\s.\-_]*".join(pattern_chars)

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


# =========================================================
# BOT
# =========================================================

class ModBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):
        await self.tree.sync()
        auto_scan_loop.start()
        print("Slash commands synchronized and auto-scan loop started.")


bot = ModBot()


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():
    print("=" * 50)
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(f"Servers: {len(bot.guilds)}")
    print("Discord-Mod-Bot is ONLINE")
    print("=" * 50)


# =========================================================
# PERMISSION FUNCTIONS
# =========================================================

def is_moderator(member):
    permissions = member.guild_permissions
    return (
        permissions.administrator
        or permissions.manage_messages
        or permissions.moderate_members
        or permissions.kick_members
        or permissions.ban_members
    )


def can_moderate(moderator, target):
    if target == moderator:
        return False
    if target == moderator.guild.owner:
        return False
    if target.top_role >= moderator.top_role:
        return False
    return True


# =========================================================
# AUTOMATIC PUNISHMENT
# =========================================================

async def automatic_punishment(message):
    member = message.author
    guild = message.guild

    if not isinstance(member, discord.Member):
        return

    if member == guild.owner or is_moderator(member):
        return

    bad_message_content = message.content
    channel_mention = message.channel.mention

    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    strikes = add_strike(guild.id, member.id)
    
    await send_mod_log(
        guild,
        "🚨 Auto-Mod: Message Deleted",
        f"**User:** {member.mention} ({member.id})\n"
        f"**Channel:** {channel_mention}\n"
        f"**Strikes:** {strikes}\n"
        f"**Message:** {bad_message_content}",
        color=discord.Color.orange()
    )

    if strikes < KICK_STRIKE:
        try:
            await member.timeout(timedelta(minutes=AUTO_MUTE_MINUTES), reason="Automatic moderation")
        except discord.Forbidden:
            pass

        remaining = KICK_STRIKE - strikes
        message_text = (
            f"{member.mention}, your message was removed.\n"
            f"⚠️ You now have **{strikes} strike(s)**. "
            f"You have **{remaining} strikes remaining** before an automatic kick."
        )
        try:
            await message.channel.send(message_text, delete_after=8)
        except discord.HTTPException:
            pass

    elif strikes == KICK_STRIKE:
        try:
            await message.channel.send(f"{member.mention} has reached **{KICK_STRIKE} strikes** and has been kicked.", delete_after=8)
        except discord.HTTPException:
            pass
            
        await send_mod_log(
            guild, "👢 Auto-Mod: User Kicked",
            f"**User:** {member.mention} ({member.id})\n**Reason:** Reached {KICK_STRIKE} strikes.",
            color=discord.Color.red()
        )
        try:
            await member.kick(reason="Automatic moderation: strike limit reached")
        except discord.Forbidden:
            pass

    elif strikes >= BAN_STRIKE:
        try:
            await message.channel.send(f"{member.mention} has reached **{BAN_STRIKE} strikes** and has been banned.", delete_after=8)
        except discord.HTTPException:
            pass
            
        await send_mod_log(
            guild, "🔨 Auto-Mod: User Banned",
            f"**User:** {member.mention} ({member.id})\n**Reason:** Reached {BAN_STRIKE} strikes.",
            color=discord.Color.dark_red()
        )
        try:
            await member.ban(reason="Automatic moderation: ban strike limit reached", delete_message_seconds=0)
        except discord.Forbidden:
            pass


# =========================================================
# MESSAGE EVENT
# =========================================================

@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    if contains_blocked_word(message.content):
        await automatic_punishment(message)
        return

    await bot.process_commands(message)


# =========================================================
# AUTOMATIC BACKGROUND SCAN TASK (Runs every 1 minute)
# =========================================================

@tasks.loop(minutes=1)
async def auto_scan_loop():
    await bot.wait_until_ready()

    for guild in bot.guilds:
        log_channel = guild.get_channel(LOG_CHANNEL_ID)
        
        if log_channel:
            try:
                await log_channel.send(
                    "🔎 *Channel is being scanned for moderation history...*",
                    delete_after=15
                )
            except discord.HTTPException:
                pass

        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)
            if not permissions.view_channel or not permissions.read_message_history:
                continue

            try:
                async for message in channel.history(limit=500, oldest_first=True):
                    if message.author.bot or not contains_blocked_word(message.content):
                        continue

                    bad_message_content = message.content
                    try:
                        await message.delete()
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        pass

                    member = message.author
                    if not isinstance(member, discord.Member) or member == guild.owner or is_moderator(member):
                        continue

                    strikes = add_strike(guild.id, member.id)
                    
                    await send_mod_log(
                        guild,
                        "🔎 Automated Scan: Message Deleted",
                        f"**User:** {member.mention} ({member.id})\n"
                        f"**Channel:** {channel.mention}\n"
                        f"**Strikes:** {strikes}\n"
                        f"**Message:** {bad_message_content}",
                        color=discord.Color.gold()
                    )

                    if strikes < KICK_STRIKE:
                        try:
                            await member.timeout(timedelta(minutes=AUTO_MUTE_MINUTES), reason="Automated server scan")
                        except discord.Forbidden:
                            pass
                    elif strikes == KICK_STRIKE:
                        try:
                            await member.kick(reason="Automated scan: strike limit")
                        except discord.Forbidden:
                            pass
                    elif strikes >= BAN_STRIKE:
                        try:
                            await member.ban(reason="Automated scan: ban strike limit", delete_message_seconds=0)
                        except discord.Forbidden:
                            pass

            except discord.Forbidden:
                print(f"Cannot scan #{channel.name}")
            except discord.HTTPException as error:
                print(f"Error scanning #{channel.name}: {error}")


# =========================================================
# STANDARD COMMANDS
# =========================================================

@bot.tree.command(name="warn", description="Warn a member and give them a strike.")
@app_commands.describe(member="The member to warn", reason="Reason for the warning")
async def warn(interaction, member: discord.Member, reason: str = "No reason provided"):
    moderator = interaction.user
    if not isinstance(moderator, discord.Member) or not is_moderator(moderator):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    if not can_moderate(moderator, member):
        await interaction.response.send_message("❌ You cannot moderate this member.", ephemeral=True)
        return

    strikes = add_strike(interaction.guild.id, member.id)
    await send_mod_log(interaction.guild, "⚠️ Manual Warning Issued", f"**Target:** {member.mention}\n**Moderator:** {moderator.mention}\n**Reason:** {reason}\n**Total Strikes:** {strikes}", color=discord.Color.yellow())
    await interaction.response.send_message(f"⚠️ **Warning issued**\nMember: {member.mention}\nReason: {reason}\nStrikes: **{strikes}**")


@bot.tree.command(name="mute", description="Timeout a member.")
@app_commands.describe(member="The member to mute", minutes="Duration in minutes", reason="Reason for the timeout")
async def mute(interaction, member: discord.Member, minutes: int = 15, reason: str = "No reason provided"):
    moderator = interaction.user
    if not isinstance(moderator, discord.Member) or not moderator.guild_permissions.moderate_members:
        await interaction.response.send_message("❌ You don't have permission to mute members.", ephemeral=True)
        return
    if not can_moderate(moderator, member):
        await interaction.response.send_message("❌ You cannot moderate this member.", ephemeral=True)
        return

    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        await send_mod_log(interaction.guild, "🔇 User Timed Out", f"**Target:** {member.mention}\n**Moderator:** {moderator.mention}\n**Duration:** {minutes} minutes\n**Reason:** {reason}", color=discord.Color.orange())
        await interaction.response.send_message(f"🔇 {member.mention} has been timed out for **{minutes} minutes**.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to timeout that member.", ephemeral=True)


@bot.tree.command(name="kick", description="Kick a member.")
@app_commands.describe(member="The member to kick", reason="Reason for the kick")
async def kick(interaction, member: discord.Member, reason: str = "No reason provided"):
    moderator = interaction.user
    if not isinstance(moderator, discord.Member) or not moderator.guild_permissions.kick_members:
        await interaction.response.send_message("❌ You don't have permission to kick members.", ephemeral=True)
        return
    if not can_moderate(moderator, member):
        await interaction.response.send_message("❌ You cannot kick this member.", ephemeral=True)
        return

    try:
        await member.kick(reason=reason)
        await send_mod_log(interaction.guild, "👢 User Kicked", f"**Target:** {member.mention}\n**Moderator:** {moderator.mention}\n**Reason:** {reason}", color=discord.Color.red())
        await interaction.response.send_message(f"👢 {member.mention} was kicked.\nReason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to kick that member.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member.")
@app_commands.describe(member="The member to ban", reason="Reason for the ban")
async def ban(interaction, member: discord.Member, reason: str = "No reason provided"):
    moderator = interaction.user
    if not isinstance(moderator, discord.Member) or not moderator.guild_permissions.ban_members:
        await interaction.response.send_message("❌ You don't have permission to ban members.", ephemeral=True)
        return
    if not can_moderate(moderator, member):
        await interaction.response.send_message("❌ You cannot ban this member.", ephemeral=True)
        return

    try:
        await member.ban(reason=reason, delete_message_seconds=0)
        await send_mod_log(interaction.guild, "🔨 User Banned", f"**Target:** {member.mention}\n**Moderator:** {moderator.mention}\n**Reason:** {reason}", color=discord.Color.dark_red())
        await interaction.response.send_message(f"🔨 {member.mention} was banned.\nReason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to ban that member.", ephemeral=True)


@bot.tree.command(name="strikes", description="Check a member's strikes.")
@app_commands.describe(member="The member to check")
async def strikes(interaction, member: discord.Member):
    amount = get_strikes(interaction.guild.id, member.id)
    await interaction.response.send_message(f"📋 {member.mention} has **{amount} strike(s)**.")


@bot.tree.command(name="clearstrikes", description="Clear a member's strikes.")
@app_commands.describe(member="The member whose strikes should be cleared")
async def clearstrikes(interaction, member: discord.Member):
    moderator = interaction.user
    if not isinstance(moderator, discord.Member) or not is_moderator(moderator):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    clear_strikes(interaction.guild.id, member.id)
    await send_mod_log(interaction.guild, "🔄 Strikes Cleared", f"**Target:** {member.mention}\n**Moderator:** {moderator.mention}", color=discord.Color.green())
    await interaction.response.send_message(f"✅ Cleared all strikes for {member.mention}.")


@bot.tree.command(name="scan", description="Scan accessible message history for blocked words.")
async def scan(interaction):
    moderator = interaction.user
    if not isinstance(moderator, discord.Member) or not moderator.guild_permissions.manage_messages:
        await interaction.response.send_message("❌ You need **Manage Messages** to use /scan.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    total_messages = 0
    flagged_messages = 0
    channels_scanned = 0

    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(
                f"🔎 *Channel is being scanned for moderation history...*",
                delete_after=15
            )
        except discord.HTTPException:
            pass

    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            continue

        channels_scanned += 1
        try:
            async for message in channel.history(limit=500, oldest_first=True):
                total_messages += 1
                if message.author.bot or not contains_blocked_word(message.content):
                    continue

                flagged_messages += 1
                bad_message_content = message.content
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass

                member = message.author
                if not isinstance(member, discord.Member) or member == guild.owner or is_moderator(member):
                    continue

                strikes = add_strike(guild.id, member.id)
                await send_mod_log(
                    guild,
                    "🔎 Historical Scan: Message Deleted",
                    f"**User:** {member.mention} ({member.id})\n**Channel:** {channel.mention}\n**Strikes:** {strikes}\n**Message:** {bad_message_content}",
                    color=discord.Color.gold()
                )

                if strikes < KICK_STRIKE:
                    try:
                        await member.timeout(timedelta(minutes=AUTO_MUTE_MINUTES), reason="Server scan")
                    except discord.Forbidden:
                        pass
                elif strikes == KICK_STRIKE:
                    try:
                        await member.kick(reason="Server scan: strike limit")
                    except discord.Forbidden:
                        pass
                elif strikes >= BAN_STRIKE:
                    try:
                        await member.ban(reason="Server scan: ban strike limit", delete_message_seconds=0)
                    except discord.Forbidden:
                        pass
        except discord.Forbidden:
            print(f"Cannot scan #{channel.name}")

    await interaction.followup.send(
        f"🔎 **Server scan complete!**\nChannels scanned: **{channels_scanned}**\nMessages checked: **{total_messages:,}**\nFlagged messages: **{flagged_messages:,}**"
    )


# =========================================================
# RENDER WEB SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Discord-Mod-Bot is online!")

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# =========================================================
# START
# =========================================================

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

web_thread = threading.Thread(target=run_web_server, daemon=True)
web_thread.start()

bot.run(TOKEN)
