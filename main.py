import os
import re
import sqlite3
import unicodedata
import threading

from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
from discord.ext import commands


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")

AUTO_MUTE_MINUTES = 15
KICK_STRIKE = 4
BAN_STRIKE = 5


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
# BLOCKED WORDS
# =========================================================

BLOCKED_WORDS = [
    # General profanity
    "fuck",
    "fucker",
    "fucking",
    "fucked",
    "motherfuck",
    "motherfucker",
    "shit",
    "shitty",
    "bullshit",
    "bitch",
    "bitches",
    "bitching",
    "asshole",
    "assholes",
    "dumbass",
    "jackass",
    "badass",
    "crap",
    "piss",
    "pissed",
    "dick",
    "dicks",
    "dickhead",
    "cock",
    "cocks",
    "cocksucker",
    "pussy",
    "bastard",
    "damn",
    "dammit",
    "hell",

    # Sexual profanity
    "slut",
    "sluts",
    "whore",
    "whores",
    "hoe",
    "hoes",

    # Common insults
    "idiot",
    "idiots",
    "moron",
    "morons",
    "stupid",
    "dumbass",
    "dumbasses",
    "retard",
    "retarded",

    # Harassment
    "kys",
    "killyourself",
]


# =========================================================
# TEXT NORMALIZATION
# =========================================================

def normalize_text(text):
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )
    text = re.sub(r"[^a-z0-9]", "", text)
    return text


# Pre-normalize blocked words once at startup for fast, whole-word matching
NORMALIZED_BLOCKED_WORDS = {normalize_text(word) for word in BLOCKED_WORDS if normalize_text(word)}

def contains_blocked_word(text):
    # Extract individual words using regex word boundaries before stripping spaces
    words = re.findall(r"\b\w+\b", text.lower())
    
    for word in words:
        if normalize_text(word) in NORMALIZED_BLOCKED_WORDS:
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
        print("Slash commands synchronized.")


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

    if member == guild.owner:
        return

    if is_moderator(member):
        return

    # Delete the message
    try:
        await message.delete()
    except (
        discord.Forbidden,
        discord.NotFound,
        discord.HTTPException
    ):
        pass

    # Add strike
    strikes = add_strike(
        guild.id,
        member.id
    )

    # -----------------------------------------------------
    # STRIKES 1-3
    # -----------------------------------------------------

    if strikes < KICK_STRIKE:
        try:
            await member.timeout(
                timedelta(
                    minutes=AUTO_MUTE_MINUTES
                ),
                reason="Automatic moderation"
            )
        except discord.Forbidden:
            print(
                f"Could not timeout {member}. "
                "Check bot permissions and role position."
            )

        remaining = KICK_STRIKE - strikes

        if remaining == 1:
            message_text = (
                f"{member.mention}, your message was removed.\n"
                f"⚠️ You now have **{strikes} strike(s)**.\n"
                f"Your next strike will result in a kick."
            )
        else:
            message_text = (
                f"{member.mention}, your message was removed.\n"
                f"⚠️ You now have **{strikes} strike(s)**.\n"
                f"You have **{remaining} strikes remaining** "
                f"before an automatic kick."
            )

        try:
            await message.channel.send(
                message_text,
                delete_after=8
            )
        except discord.HTTPException:
            pass

    # -----------------------------------------------------
    # STRIKE 4 = KICK
    # -----------------------------------------------------

    elif strikes == KICK_STRIKE:
        try:
            await message.channel.send(
                f"{member.mention} has reached "
                f"**{KICK_STRIKE} strikes** and has been kicked.",
                delete_after=8
            )
        except discord.HTTPException:
            pass

        try:
            await member.kick(
                reason="Automatic moderation: strike limit reached"
            )
        except discord.Forbidden:
            print(
                f"Could not kick {member}."
            )

    # -----------------------------------------------------
    # STRIKE 5+ = BAN
    # -----------------------------------------------------

    elif strikes >= BAN_STRIKE:
        try:
            await message.channel.send(
                f"{member.mention} has reached "
                f"**{BAN_STRIKE} strikes** and has been banned.",
                delete_after=8
            )
        except discord.HTTPException:
            pass

        try:
            await member.ban(
                reason="Automatic moderation: ban strike limit reached",
                delete_message_seconds=0
            )
        except discord.Forbidden:
            print(
                f"Could not ban {member}."
            )


# =========================================================
# MESSAGE EVENT
# =========================================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild is None:
        return

    if contains_blocked_word(message.content):
        await automatic_punishment(message)
        return

    await bot.process_commands(message)


# =========================================================
# /WARN
# =========================================================

@bot.tree.command(
    name="warn",
    description="Warn a member and give them a strike."
)
@app_commands.describe(
    member="The member to warn",
    reason="Reason for the warning"
)
async def warn(
    interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):
    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not is_moderator(moderator):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "❌ You cannot moderate this member.",
            ephemeral=True
        )
        return

    strikes = add_strike(
        interaction.guild.id,
        member.id
    )

    await interaction.response.send_message(
        f"⚠️ **Warning issued**\n\n"
        f"Member: {member.mention}\n"
        f"Reason: {reason}\n"
        f"Strikes: **{strikes}**"
    )


# =========================================================
# /MUTE
# =========================================================

@bot.tree.command(
    name="mute",
    description="Timeout a member."
)
@app_commands.describe(
    member="The member to mute",
    minutes="Duration in minutes",
    reason="Reason for the timeout"
)
async def mute(
    interaction,
    member: discord.Member,
    minutes: int = 15,
    reason: str = "No reason provided"
):
    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not moderator.guild_permissions.moderate_members:
        await interaction.response.send_message(
            "❌ You don't have permission to mute members.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "❌ You cannot moderate this member.",
            ephemeral=True
        )
        return

    if minutes < 1:
        await interaction.response.send_message(
            "❌ Duration must be at least 1 minute.",
            ephemeral=True
        )
        return

    if minutes > 40320:
        await interaction.response.send_message(
            "❌ Discord's maximum timeout is 28 days.",
            ephemeral=True
        )
        return

    try:
        await member.timeout(
            timedelta(minutes=minutes),
            reason=reason
        )
        await interaction.response.send_message(
            f"🔇 {member.mention} has been timed out "
            f"for **{minutes} minutes**.\n"
            f"Reason: {reason}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to timeout that member.",
            ephemeral=True
        )


# =========================================================
# /KICK
# =========================================================

@bot.tree.command(
    name="kick",
    description="Kick a member."
)
@app_commands.describe(
    member="The member to kick",
    reason="Reason for the kick"
)
async def kick(
    interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):
    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not moderator.guild_permissions.kick_members:
        await interaction.response.send_message(
            "❌ You don't have permission to kick members.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "❌ You cannot kick this member.",
            ephemeral=True
        )
        return

    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(
            f"👢 {member.mention} was kicked.\n"
            f"Reason: {reason}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to kick that member.",
            ephemeral=True
        )


# =========================================================
# /BAN
# =========================================================

@bot.tree.command(
    name="ban",
    description="Ban a member."
)
@app_commands.describe(
    member="The member to ban",
    reason="Reason for the ban"
)
async def ban(
    interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):
    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not moderator.guild_permissions.ban_members:
        await interaction.response.send_message(
            "❌ You don't have permission to ban members.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "❌ You cannot ban this member.",
            ephemeral=True
        )
        return

    try:
        await member.ban(
            reason=reason,
            delete_message_seconds=0
        )
        await interaction.response.send_message(
            f"🔨 {member.mention} was banned.\n"
            f"Reason: {reason}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to ban that member.",
            ephemeral=True
        )


# =========================================================
# /STRIKES
# =========================================================

@bot.tree.command(
    name="strikes",
    description="Check a member's strikes."
)
@app_commands.describe(
    member="The member to check"
)
async def strikes(
    interaction,
    member: discord.Member
):
    amount = get_strikes(
        interaction.guild.id,
        member.id
    )
    await interaction.response.send_message(
        f"📋 {member.mention} has **{amount} strike(s)**."
    )


# =========================================================
# /CLEARSTRIKES
# =========================================================

@bot.tree.command(
    name="clearstrikes",
    description="Clear a member's strikes."
)
@app_commands.describe(
    member="The member whose strikes should be cleared"
)
async def clearstrikes(
    interaction,
    member: discord.Member
):
    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not is_moderator(moderator):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.",
            ephemeral=True
        )
        return

    clear_strikes(
        interaction.guild.id,
        member.id
    )

    await interaction.response.send_message(
        f"✅ Cleared all strikes for {member.mention}."
    )


# =========================================================
# /SCAN
# =========================================================

@bot.tree.command(
    name="scan",
    description="Scan accessible message history for blocked words."
)
async def scan(interaction):
    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not moderator.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "❌ You need **Manage Messages** to use /scan.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    total_messages = 0
    flagged_messages = 0
    channels_scanned = 0

    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)

        if not permissions.view_channel:
            continue

        if not permissions.read_message_history:
            continue

        channels_scanned += 1

        try:
            # Capped limit at 500 per channel to prevent rate-limit exhaustion and hanging
            async for message in channel.history(
                limit=500,
                oldest_first=True
            ):
                total_messages += 1

                if message.author.bot:
                    continue

                if not contains_blocked_word(message.content):
                    continue

                flagged_messages += 1

                try:
                    await message.delete()
                except (
                    discord.Forbidden,
                    discord.NotFound,
                    discord.HTTPException
                ):
                    pass

                member = message.author

                if not isinstance(member, discord.Member):
                    continue

                if member == guild.owner:
                    continue

                if is_moderator(member):
                    continue

                strikes = add_strike(
                    guild.id,
                    member.id
                )

                if strikes < KICK_STRIKE:
                    try:
                        await member.timeout(
                            timedelta(minutes=AUTO_MUTE_MINUTES),
                            reason="Server scan"
                        )
                    except discord.Forbidden:
                        pass

                elif strikes == KICK_STRIKE:
                    try:
                        await member.kick(
                            reason="Server scan: strike limit"
                        )
                    except discord.Forbidden:
                        pass

                elif strikes >= BAN_STRIKE:
                    try:
                        await member.ban(
                            reason="Server scan: ban strike limit",
                            delete_message_seconds=0
                        )
                    except discord.Forbidden:
                        pass

        except discord.Forbidden:
            print(f"Cannot scan #{channel.name}")
        except discord.HTTPException as error:
            print(f"Error scanning #{channel.name}: {error}")

    await interaction.followup.send(
        f"🔎 **Server scan complete!**\n\n"
        f"Channels scanned: **{channels_scanned}**\n"
        f"Messages checked: **{total_messages:,}**\n"
        f"Flagged messages: **{flagged_messages:,}**"
    )


# =========================================================
# COMMAND ERROR HANDLER
# =========================================================

@bot.tree.error
async def command_error(
    interaction,
    error
):
    print(f"Command error: {error}")

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Something went wrong.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Something went wrong.",
                ephemeral=True
            )
    except discord.HTTPException:
        pass


# =========================================================
# RENDER WEB SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain"
        )
        self.end_headers()
        self.wfile.write(
            b"Discord-Mod-Bot is online!"
        )

    def log_message(
        self,
        format,
        *args
    ):
        pass


def run_web_server():
    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )
    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )
    print(
        f"Web server running on port {port}"
    )
    server.serve_forever()


# =========================================================
# START
# =========================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing."
    )

web_thread = threading.Thread(
    target=run_web_server,
    daemon=True
)
web_thread.start()

bot.run(TOKEN)
