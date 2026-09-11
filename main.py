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
BAN_STRIKE = 3

# Anti-Spam Settings
SPAM_THRESHOLD = 5
SPAM_WINDOW = 10           # Time window in seconds to check for repeated messages
SPAM_TIMEOUT_MINUTES = 30  # Timeout duration when spamming is detected
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
# BLOCKED WORDS & EMOJIS
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
intents.reactions = True


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
    # Ensure the server owner can ALWAYS moderate, even if they have no top roles assigned
    if moderator == moderator.guild.owner:
        return True
    if target.top_role >= moderator.top_role:
        return False
    return True


# =========================================================
# ANTI-SPAM & ANTI-RAID SYSTEM
# =========================================================

async def check_anti_spam(message):
    if message.author.bot or not message.guild:
        return False

    member = message.author
    if not isinstance(member, discord.Member):
        return False

    if member == message.guild.owner or is_moderator(member):
        return False

    current_time = time.time()
    user_id = member.id
    content = message.content

    user_message_cache[user_id].append((content, current_time))
    recent_msgs = user_message_cache[user_id]

    matching_count = sum(1 for c, t in recent_msgs if c == content and (current_time - t) <= SPAM_WINDOW)

    if matching_count >= SPAM_THRESHOLD:
        user_message_cache[user_id].clear()

        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        try:
            await member.timeout(timedelta(minutes=SPAM_TIMEOUT_MINUTES), reason="Anti-spam: repeating messages")
        except discord.Forbidden:
            pass

        await send_mod_log(
            message.guild,
            "🛡️ Anti-Spam: User Timed Out",
            f"**User:** {member.mention} ({member.id})\n"
            f"**Channel:** {message.channel.mention}\n"
            f"**Reason:** Repeated the same message {matching_count} times.\n"
            f"**Action:** Timed out for {SPAM_TIMEOUT_MINUTES} minutes.",
            color=discord.Color.purple()
        )

        try:
            await message.channel.send(f"{member.mention} has been automatically timed out for **{SPAM_TIMEOUT_MINUTES} minutes** for spamming.", delete_after=8)
        except discord.HTTPException:
            pass

        return True

    return False


@bot.event
async def on_member_join(member):
    guild = member.guild
    current_time = time.time()
    recent_joins.append(current_time)

    joins_in_window = sum(1 for t in recent_joins if (current_time - t) <= RAID_WINDOW)

    if joins_in_window >= RAID_JOIN_THRESHOLD:
        await send_mod_log(
            guild,
            "🚨 Anti-Raid Alert!",
            f"**High join rate detected!** {joins_in_window} members joined within {RAID_WINDOW} seconds.\n"
            f"Potential raid in progress. Please review server security settings.",
            color=discord.Color.dark_red()
        )


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
# MESSAGE & REACTION EVENTS
# =========================================================

@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    if contains_blocked_word(message.content):
        await automatic_punishment(message)
        return

    if await check_anti_spam(message):
        return

    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or not reaction.message.guild:
        return

    guild = reaction.message.guild
    member = guild.get_member(user.id)
    if not member or member == guild.owner or is_moderator(member):
        return

    emoji_str = str(reaction.emoji)

    if emoji_str in BLOCKED_EMOJIS or any(b in emoji_str for b in BLOCKED_EMOJIS):
        try:
            await reaction.remove(user)
        except (discord.Forbidden, discord.HTTPException):
            pass

        strikes = add_strike(guild.id, member.id)

        await send_mod_log(
            guild,
            "🚨 Auto-Mod: Reaction Removed",
            f"**User:** {member.mention} ({member.id})\n"
            f"**Channel:** {reaction.message.channel.mention}\n"
            f"**Strikes:** {strikes}\n"
            f"**Blocked Emoji:** {emoji_str}",
            color=discord.Color.orange()
        )

        if strikes < KICK_STRIKE:
            try:
                await member.timeout(timedelta(minutes=AUTO_MUTE_MINUTES), reason="Automatic moderation: blocked reaction")
            except discord.Forbidden:
                pass

            try:
                await reaction.message.channel.send(
                    f"{member.mention}, that reaction is not allowed.\n"
                    f"⚠️ You now have **{strikes} strike(s)**.",
                    delete_after=8
                )
            except discord.HTTPException:
                pass

        elif strikes == KICK_STRIKE:
            try:
                await reaction.message.channel.send(f"{member.mention} has reached **{KICK_STRIKE} strikes** and has been kicked.", delete_after=8)
            except discord.HTTPException:
                pass

            await send_mod_log(
                guild, "👢 Auto-Mod: User Kicked",
                f"**User:** {member.mention} ({member.id})\n**Reason:** Reached {KICK_STRIKE} strikes via reactions.",
                color=discord.Color.red()
            )
            try:
                await member.kick(reason="Automatic moderation: strike limit reached")
            except discord.Forbidden:
                pass

        elif strikes >= BAN_STRIKE:
            try:
                await reaction.message.channel.send(f"{member.mention} has reached **{BAN_STRIKE} strikes** and has been banned.", delete_after=8)
            except discord.HTTPException:
                pass

            await send_mod_log(
                guild, "🔨 Auto-Mod: User Banned",
                f"**User:** {member.mention} ({member.id})\n**Reason:** Reached {BAN_STRIKE} strikes via reactions.",
                color=discord.Color.dark_red()
            )
            try:
                await member.ban(reason="Automatic moderation: ban strike limit reached", delete_message_seconds=0)
            except discord.Forbidden:
                pass


# =========================================================
# AUTOMATIC BACKGROUND SCAN TASK (Runs every 30 seconds)
# =========================================================

@tasks.loop(seconds=30)
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
                async for message in channel.history(limit=100, oldest_first=True):
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
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
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
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: int = 15, reason: str = "No reason provided"):
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
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
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
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
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


@bot.tree.command(name="unban", description="Unban a user and DM them an invite link.")
@app_commands.describe(user="The user to unban (select them or paste their ID)", reason="Reason for the unban")
async def unban(interaction: discord.Interaction, user: discord.User, reason: str = "No reason provided"):
    moderator = interaction.user
    
    if not isinstance(moderator, discord.Member) or not moderator.guild_permissions.ban_members:
        await interaction.response.send_message("❌ You don't have permission to unban members.", ephemeral=True)
        return

    try:
        # Unban the user
        await interaction.guild.unban(user, reason=reason)
        
        # Create a one-time use invite link from the channel the command was run in
        invite = await interaction.channel.create_invite(
            max_uses=1, 
            max_age=86400, # 24 hours
            reason=f"Unban invite for {user.name}"
        )
        
        # Attempt to DM the user
        dm_status = ""
        try:
            await user.send(
                f"You have been unbanned from **{interaction.guild.name}**.\n"
                f"**Reason:** {reason}\n"
                f"Here is your invite link to rejoin: {invite.url}"
            )
            dm_status = "and an invite link was sent to their DMs."
        except discord.Forbidden:
            dm_status = "but their DMs are closed or we don't share a server, so the invite wasn't sent."
        
        # Log the action
        await send_mod_log(
            interaction.guild, 
            "🕊️ User Unbanned", 
            f"**Target:** {user.mention} ({user.id})\n**Moderator:** {moderator.mention}\n**Reason:** {reason}", 
            color=discord.Color.green()
        )
        
        # Respond to the moderator
        await interaction.response.send_message(f"✅ {user.mention} was unbanned {dm_status}")
        
    except discord.NotFound:
        await interaction.response.send_message(f"❌ {user.mention} is not currently banned.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to unban that user.", ephemeral=True)


@bot.tree.command(name="strikes", description="Check a member's strikes.")
@app_commands.describe(member="The member to check")
async def strikes(interaction: discord.Interaction, member: discord.Member):
    amount = get_strikes(interaction.guild.id, member.id)
    await interaction.response.send_message(f"📋 {member.mention} has **{amount} strike(s)**.")


@bot.tree.command(name="clearstrikes", description="Clear a member's strikes.")
@app_commands.describe(member="The member whose strikes should be cleared")
async def clearstrikes(interaction: discord.Interaction, member: discord.Member):
    moderator = interaction.user
    if not isinstance(moderator, discord.Member) or not is_moderator(moderator):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    clear_strikes(interaction.guild.id, member.id)
    await send_mod_log(interaction.guild, "🔄 Strikes Cleared", f"**Target:** {member.mention}\n**Moderator:** {moderator.mention}", color=discord.Color.green())
    await interaction.response.send_message(f"✅ Cleared all strikes for {member.mention}.")


@bot.tree.command(name="scan", description="Scan accessible message history for blocked words.")
async def scan(interaction: discord.Interaction):
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
