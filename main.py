import os
import re
import sqlite3
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands


# =========================================================
# CONFIGURATION
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")

# Number of strikes before automatic actions
KICK_STRIKE = 4
BAN_STRIKE = 5

# Automatic timeout duration
AUTO_MUTE_MINUTES = 15


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect("moderation.db")
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


def get_strikes(guild_id: int, user_id: int) -> int:
    cursor.execute(
        """
        SELECT strikes
        FROM strikes
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id)
    )

    result = cursor.fetchone()

    if result is None:
        return 0

    return result[0]


def add_strike(guild_id: int, user_id: int) -> int:
    current = get_strikes(guild_id, user_id)
    new_amount = current + 1

    cursor.execute(
        """
        INSERT INTO strikes (guild_id, user_id, strikes)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET strikes = excluded.strikes
        """,
        (guild_id, user_id, new_amount)
    )

    db.commit()

    return new_amount


def clear_strikes(guild_id: int, user_id: int):
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
#
# Add your moderation terms here.
#
# IMPORTANT:
# Don't put spaces between characters in this list.
# The bot normalizes the message before checking it.
#
# Example:
#
# BLOCKED_WORDS = [
#     "example",
#     "anotherword",
# ]
#
# =========================================================

BLOCKED_WORDS = [
    # Add your blocked words here
]


def normalize_text(text: str) -> str:
    """
    Makes it harder for users to bypass the filter by using:
      E X A M P L E
      e-x-a-m-p-l-e
      e.x.a.m.p.l.e
      E__X__A__M__P__L__E
    """

    text = text.lower()

    # Remove Unicode combining marks
    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )

    # Keep only letters and numbers
    text = re.sub(r"[^a-z0-9]", "", text)

    return text


def contains_blocked_word(text: str) -> bool:
    normalized = normalize_text(text)

    for word in BLOCKED_WORDS:
        normalized_word = normalize_text(word)

        if normalized_word and normalized_word in normalized:
            return True

    return False


# =========================================================
# DISCORD SETUP
# =========================================================

intents = discord.Intents.default()

intents.message_content = True
intents.members = True
intents.guilds = True


class ModBot(commands.Bot):

    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):

        # Sync slash commands with Discord
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
# PERMISSION HELPERS
# =========================================================

def is_moderator(member: discord.Member) -> bool:

    permissions = member.guild_permissions

    return (
        permissions.administrator
        or permissions.manage_messages
        or permissions.kick_members
        or permissions.ban_members
    )


def can_moderate(
    moderator: discord.Member,
    target: discord.Member
) -> bool:

    if target == moderator:
        return False

    if target == moderator.guild.owner:
        return False

    if target.top_role >= moderator.top_role:
        return False

    return True


# =========================================================
# AUTOMATIC MODERATION
# =========================================================

async def punish_for_blocked_message(
    message: discord.Message
):

    member = message.author

    if not isinstance(member, discord.Member):
        return

    # Don't punish server owner
    if member == message.guild.owner:
        return

    # Don't automatically punish moderators
    if is_moderator(member):
        return

    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    strikes = add_strike(
        message.guild.id,
        member.id
    )

    # -----------------------------------------------------
    # STRIKE 1-3
    # -----------------------------------------------------

    if strikes < KICK_STRIKE:

        try:
            await member.timeout(
                timedelta(minutes=AUTO_MUTE_MINUTES),
                reason="Automatic moderation"
            )
        except discord.Forbidden:
            print(
                f"Cannot timeout {member}. "
                "Check bot role position and permissions."
            )

        remaining = KICK_STRIKE - strikes

        if remaining == 1:
            warning = (
                f"{member.mention}, your message was removed. "
                f"You now have **{strikes} strike(s)**. "
                f"Your next strike will result in a kick."
            )
        else:
            warning = (
                f"{member.mention}, your message was removed. "
                f"You now have **{strikes} strike(s)**. "
                f"You have **{remaining} strikes remaining** "
                f"before an automatic kick."
            )

        try:
            await message.channel.send(
                warning,
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
            print(f"Cannot kick {member}.")

    # -----------------------------------------------------
    # STRIKE 5 = BAN
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
            print(f"Cannot ban {member}.")


# =========================================================
# MESSAGE LISTENER
# =========================================================

@bot.event
async def on_message(message: discord.Message):

    # Ignore bots
    if message.author.bot:
        return

    # Ignore DMs
    if message.guild is None:
        return

    # Check blocked words
    if contains_blocked_word(message.content):

        await punish_for_blocked_message(message)

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
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):

    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not is_moderator(moderator):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "You cannot moderate this member.",
            ephemeral=True
        )
        return

    strikes = add_strike(
        interaction.guild.id,
        member.id
    )

    await interaction.response.send_message(
        f"⚠️ **Warning issued**\n"
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
    minutes="How many minutes",
    reason="Reason for the timeout"
)
async def mute(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: int = 15,
    reason: str = "No reason provided"
):

    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not is_moderator(moderator):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "You cannot moderate this member.",
            ephemeral=True
        )
        return

    if minutes < 1:
        await interaction.response.send_message(
            "Minutes must be at least 1.",
            ephemeral=True
        )
        return

    if minutes > 40320:
        await interaction.response.send_message(
            "The timeout cannot exceed 28 days.",
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
            "I don't have permission to timeout that member.",
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
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):

    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not moderator.guild_permissions.kick_members:
        await interaction.response.send_message(
            "You don't have permission to kick members.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "You cannot kick this member.",
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
            "I don't have permission to kick that member.",
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
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided"
):

    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not moderator.guild_permissions.ban_members:
        await interaction.response.send_message(
            "You don't have permission to ban members.",
            ephemeral=True
        )
        return

    if not can_moderate(moderator, member):
        await interaction.response.send_message(
            "You cannot ban this member.",
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
            "I don't have permission to ban that member.",
            ephemeral=True
        )


# =========================================================
# /STRIKES
# =========================================================

@bot.tree.command(
    name="strikes",
    description="Check a member's strike count."
)
@app_commands.describe(
    member="The member to check"
)
async def strikes(
    interaction: discord.Interaction,
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
    interaction: discord.Interaction,
    member: discord.Member
):

    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not is_moderator(moderator):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
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
    description="Scan the server's accessible message history."
)
async def scan(
    interaction: discord.Interaction
):

    moderator = interaction.user

    if not isinstance(moderator, discord.Member):
        return

    if not moderator.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "You need the **Manage Messages** permission to use /scan.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    total_messages = 0
    flagged_messages = 0

    guild = interaction.guild

    for channel in guild.text_channels:

        # Skip channels the bot cannot read
        permissions = channel.permissions_for(guild.me)

        if not permissions.view_channel:
            continue

        if not permissions.read_message_history:
            continue

        try:

            async for message in channel.history(
                limit=None,
                oldest_first=True
            ):

                total_messages += 1

                if message.author.bot:
                    continue

                if contains_blocked_word(message.content):

                    flagged_messages += 1

                    try:
                        await message.delete()
                    except (
                        discord.Forbidden,
                        discord.NotFound,
                        discord.HTTPException
                    ):
                        pass

                    if isinstance(
                        message.author,
                        discord.Member
                    ):

                        if (
                            message.author != guild.owner
                            and not is_moderator(message.author)
                        ):

                            strikes = add_strike(
                                guild.id,
                                message.author.id
                            )

                            if strikes < KICK_STRIKE:

                                try:
                                    await message.author.timeout(
                                        timedelta(
                                            minutes=AUTO_MUTE_MINUTES
                                        ),
                                        reason="Server scan"
                                    )
                                except discord.Forbidden:
                                    pass

                            elif strikes == KICK_STRIKE:

                                try:
                                    await message.author.kick(
                                        reason="Server scan: strike limit"
                                    )
                                except discord.Forbidden:
                                    pass

                            elif strikes >= BAN_STRIKE:

                                try:
                                    await message.author.ban(
                                        reason="Server scan: ban limit",
                                        delete_message_seconds=0
                                    )
                                except discord.Forbidden:
                                    pass

            # Prevent hammering Discord's API
            await discord.utils.sleep_until(
                discord.utils.utcnow()
            )

        except discord.Forbidden:
            print(
                f"No permission to scan #{channel.name}"
            )

        except discord.HTTPException as error:
            print(
                f"Error scanning #{channel.name}: {error}"
            )

    await interaction.followup.send(
        f"🔎 **Scan complete!**\n\n"
        f"Messages checked: **{total_messages:,}**\n"
        f"Flagged messages: **{flagged_messages:,}**"
    )


# =========================================================
# ERROR HANDLING
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    print(f"Command error: {error}")

    if interaction.response.is_done():

        await interaction.followup.send(
            "Something went wrong while running that command.",
            ephemeral=True
        )

    else:

        await interaction.response.send_message(
            "Something went wrong while running that command.",
            ephemeral=True
        )


# =========================================================
# START BOT
# =========================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing."
    )

bot.run(TOKEN)
