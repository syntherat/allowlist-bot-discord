import discord
from discord.ext import commands, tasks
from discord import ui
import asyncpg
import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from io import StringIO
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import uvicorn

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

app = FastAPI()
@app.get("/")
async def health_check():
    return PlainTextResponse("Bot is running")

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('discord')

# Database connection pool
pool = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Configuration
APPLICATION_CHANNEL_ID = int(os.getenv('APPLICATION_CHANNEL_ID'))
MOD_REVIEW_CHANNEL_ID = int(os.getenv('MOD_REVIEW_CHANNEL_ID'))
LOGS_CHANNEL_ID = int(os.getenv('LOGS_CHANNEL_ID'))
ALLOWLISTED_ROLE_ID = int(os.getenv('ALLOWLISTED_ROLE_ID'))
DATABASE_URL = os.getenv('DATABASE_URL')
APPLICATION_BANNER = os.getenv('APPLICATION_BANNER_URL')
APPROVED_BANNER = os.getenv('APPROVED_BANNER_URL')
DECLINED_BANNER = os.getenv('DECLINED_BANNER_URL')

# Cooldown configuration (in seconds)
APPLICATION_COOLDOWN = int(os.getenv('APPLICATION_COOLDOWN', 86400))  # Default 24 hours
COOLDOWN_BYPASS_IDS = [int(id.strip()) for id in os.getenv('COOLDOWN_BYPASS_IDS', '').split(',') if id.strip()]
COOLDOWN_MANAGEMENT_CHANNEL_ID = int(os.getenv('COOLDOWN_MANAGEMENT_CHANNEL_ID'))

MAX_MESSAGE_LENGTH = 2000  # Discord's message limit

# Embed Creation Functions
async def create_apply_channel_embed():
    """Create the initial embed for the application channel (with banner)"""
    embed = discord.Embed(
        title="Allowlist Application",
        description="Click the button below to apply for the server allowlist.",
        color=discord.Color.blue()
    )
    if APPLICATION_BANNER:
        embed.set_image(url=APPLICATION_BANNER)
    return embed

async def create_mod_review_embed(user: discord.User, application: dict):
    """Create embed for mod review channel (no banner)"""
    embed = discord.Embed(
        title=f"Allowlist Application - {user.display_name}",
        color=discord.Color.blue(),
        timestamp=application['created_at']
    )
    embed.add_field(name="Steam Hex ID", value=application['steam_hex'], inline=False)
    embed.add_field(name="Real Name", value=application['real_name'], inline=True)
    embed.add_field(name="Character Name", value=application['character_name'], inline=True)
    embed.add_field(name="Age", value=application['age'], inline=True)
    
    embed.set_footer(text=f"Application ID: {application['id']} | User ID: {user.id}")
    return embed

async def create_approved_log_embed(user: discord.User, moderator: discord.Member):
    try:
        embed = discord.Embed(
            title="Application Approved",
            description=f"{user.mention} has been approved for the allowlist.",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        if APPROVED_BANNER:
            embed.set_image(url=APPROVED_BANNER)
        embed.set_footer(text=f"Approved by {moderator.display_name}")
        return embed
    except Exception as e:
        logger.error(f"Error creating approved log embed: {e}")
        return None

async def create_declined_log_embed(user: discord.User, moderator: discord.Member, reason: str = None):
    """Create embed for decline logs (with banner)"""
    embed = discord.Embed(
        title="Application Declined",
        description=f"{user.mention} has been declined for the allowlist.",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    if DECLINED_BANNER:
        embed.set_image(url=DECLINED_BANNER)
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text=f"Declined by {moderator.display_name}")
    return embed

async def create_user_response_embed(title: str, description: str, color: discord.Color, banner_url: str = None):
    """Create embed for user-facing messages (optional banner)"""
    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    if banner_url:
        embed.set_image(url=banner_url)
    return embed

# Database Functions
async def create_db_pool():
    global pool
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL, 
            min_size=1,  # Start with just 1 connection
            max_size=5,  # Small pool size for Render's free tier
            command_timeout=30,
            max_inactive_connection_lifetime=60,
            server_settings={
                'application_name': 'discord-bot'
            }
        )
        # Test the connection
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        logger.info("Database pool created successfully")
    except Exception as e:
        logger.critical(f"Failed to create database pool: {e}")
        raise

async def init_db():
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS new_applications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                user_name TEXT NOT NULL,
                steam_hex TEXT NOT NULL,
                real_name TEXT NOT NULL,
                character_name TEXT NOT NULL,
                age INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                mod_reason TEXT,
                moderator_id BIGINT,
                message_id BIGINT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                last_application TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cooldown_exempt (
                user_id BIGINT PRIMARY KEY
            )
        """)
    logger.info("Database initialized")

async def get_application(application_id: int) -> Optional[dict]:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM new_applications WHERE id = $1", 
            application_id
        )

async def get_user_last_application(user_id: int) -> Optional[datetime]:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT last_application FROM new_applications WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id
        )

async def is_cooldown_exempt(user_id: int) -> bool:
    if user_id in COOLDOWN_BYPASS_IDS:
        return True
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM cooldown_exempt WHERE user_id = $1)",
            user_id
        )

async def add_cooldown_exempt(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cooldown_exempt (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )
    logger.info(f"Added cooldown exemption for user {user_id}")

async def remove_cooldown_exempt(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM cooldown_exempt WHERE user_id = $1",
            user_id
        )
    logger.info(f"Removed cooldown exemption for user {user_id}")

async def create_application(data: dict) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO new_applications (
                user_id, user_name, steam_hex, real_name, 
                character_name, age, last_application
            ) 
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            RETURNING id
            """,
            data['user_id'], data['user_name'], data['steam_hex'],
            data['real_name'], data['character_name'], data['age']
        )

async def update_application_message_id(application_id: int, message_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE new_applications SET message_id = $1 WHERE id = $2",
            message_id, application_id
        )

async def update_application_status(application_id: int, status: str, 
                                 moderator_id: int = None, reason: str = None):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE new_applications 
            SET status = $1, 
                mod_reason = $2, 
                moderator_id = $3,
                updated_at = NOW()
            WHERE id = $4
            """,
            status, reason, moderator_id, application_id
        )
    logger.info(f"Updated application {application_id} to status {status}")

async def assign_allowlisted_role(user_id: int, guild_id: int):
    guild = bot.get_guild(guild_id)
    if not guild:
        logger.warning(f"Guild {guild_id} not found")
        return False
    
    member = guild.get_member(user_id)
    if not member:
        logger.warning(f"Member {user_id} not found in guild {guild_id}")
        return False
    
    role = guild.get_role(ALLOWLISTED_ROLE_ID)
    if not role:
        logger.warning(f"Role {ALLOWLISTED_ROLE_ID} not found in guild {guild_id}")
        return False
    
    try:
        await member.add_roles(role)
        logger.info(f"Assigned allowlist role to user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error assigning role to user {user_id}: {e}")
        return False

async def send_to_mod_channel(user: discord.User, application_id: int):
    """Send application to mod review channel"""
    mod_channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
    if not mod_channel:
        logger.warning("Mod review channel not found")
        return
    
    application = await get_application(application_id)
    if not application:
        logger.warning(f"Application {application_id} not found")
        return
    
    embed = discord.Embed(
        title=f"Allowlist Application - {user.display_name}",
        color=discord.Color.blue(),
        timestamp=application['created_at']
    )
    embed.add_field(name="Steam Hex ID", value=application['steam_hex'], inline=False)
    embed.add_field(name="Real Name", value=application['real_name'], inline=True)
    embed.add_field(name="Character Name", value=application['character_name'], inline=True)
    embed.add_field(name="Age", value=application['age'], inline=True)
    
    view = ApplicationReviewView(application_id)
    message = await mod_channel.send(embed=embed, view=view)
    
    await update_application_message_id(application_id, message.id)
    logger.info(f"Sent application {application_id} to mod channel")

# Application Modal
class ApplicationModal(ui.Modal, title="Allowlist Application"):
    def __init__(self):
        super().__init__()
        self.add_item(ui.TextInput(label="Steam Hex ID", required=True))
        self.add_item(ui.TextInput(label="Real Name", required=True))
        self.add_item(ui.TextInput(label="Character Name", required=True))
        self.add_item(ui.TextInput(label="Age", required=True))
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            age = int(self.children[3].value)
            if age < 18:
                embed = await create_user_response_embed(
                    title="Application Declined",
                    description="You must be 18+ to apply for the allowlist.",
                    color=discord.Color.red(),
                    banner_url=DECLINED_BANNER
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)

                log_channel = bot.get_channel(LOGS_CHANNEL_ID)
                if log_channel:
                    embed = await create_declined_log_embed(
                        interaction.user,
                        bot.user,
                        "Automatically declined for being under 18"
                    )
                    await log_channel.send(embed=embed)
                return

            app_data = {
                'user_id': interaction.user.id,
                'user_name': interaction.user.display_name,
                'steam_hex': self.children[0].value,
                'real_name': self.children[1].value,
                'character_name': self.children[2].value,
                'age': age
            }

            app_id = await create_application(app_data)

            await interaction.response.send_message(
                embed=await create_user_response_embed(
                    title="Application Submitted",
                    description="Your application is under review by our staff team.",
                    color=discord.Color.orange()
                ),
                ephemeral=True
            )

            await send_to_mod_channel(interaction.user, app_id)

        except ValueError:
            await interaction.response.send_message("Please enter a valid number for age.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error creating application: {e}")
            await interaction.response.send_message(
                "An error occurred while submitting your application. Please try again later.",
                ephemeral=True
            )

# Application Review View
class ApplicationReviewView(ui.View):
    def __init__(self, application_id: int):
        super().__init__(timeout=None)
        self.application_id = application_id
    
    @ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="approve_btn")
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        try:
            # Defer immediately to prevent timeout
            await interaction.response.defer(ephemeral=True)
            
            await update_application_status(
                self.application_id,
                status='approved',
                moderator_id=interaction.user.id
            )
            
            application = await get_application(self.application_id)
            if not application:
                return await interaction.followup.send("Application not found!", ephemeral=True)
            
            user = await bot.fetch_user(application['user_id'])
            
            self.clear_items()
            await interaction.message.edit(view=self)
            
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.title = f"APPROVED - {embed.title}"
            await interaction.message.edit(embed=embed)
            
            role_assigned = await assign_allowlisted_role(application['user_id'], interaction.guild.id)
            
            log_channel = bot.get_channel(LOGS_CHANNEL_ID)
            if log_channel:
                log_embed = await create_approved_log_embed(user, interaction.user)
                if not role_assigned:
                    log_embed.add_field(name="Warning", value="Failed to assign allowlisted role", inline=False)
                await log_channel.send(embed=log_embed)
            
            try:
                await user.send(
                    embed=await create_user_response_embed(
                        title="Application Approved",
                        description="Your allowlist application has been approved!" + 
                                ("\n\nYou have been granted the allowlisted role!" if role_assigned else ""),
                        color=discord.Color.green(),
                        banner_url=APPROVED_BANNER
                    )
                )
            except Exception as e:
                logger.error(f"Error notifying user: {e}")
                if log_channel:
                    await log_channel.send(f"Could not DM user {user.mention} about their approval.")
            
            await interaction.followup.send(
                f"Application approved. {'Role assigned successfully.' if role_assigned else 'Failed to assign role.'}",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in approval process: {e}")
            await interaction.followup.send(
                "An error occurred during approval. Please check logs.",
                ephemeral=True
            )
        
    @ui.button(label="Decline", style=discord.ButtonStyle.red, custom_id="decline_btn")
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        try:
            modal = DeclineReasonModal(self.application_id)
            await interaction.response.send_modal(modal)
            await modal.wait()
            
            if not modal.reason:
                return
                
            application = await get_application(self.application_id)
            if not application:
                return await interaction.followup.send("Application not found!", ephemeral=True)
            
            user = await bot.fetch_user(application['user_id'])
            
            self.clear_items()
            await interaction.message.edit(view=self)
            
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.red()
            embed.title = f"DECLINED - {embed.title}"
            if modal.reason:
                embed.add_field(name="Reason", value=modal.reason, inline=False)
            await interaction.message.edit(embed=embed)
            
            log_channel = bot.get_channel(LOGS_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    embed=await create_declined_log_embed(user, interaction.user, modal.reason)
                )
            
            try:
                user_embed = await create_user_response_embed(
                    title="Application Declined",
                    description="Your allowlist application has been declined.",
                    color=discord.Color.red(),
                    banner_url=DECLINED_BANNER
                )
                if modal.reason:
                    user_embed.add_field(name="Reason", value=modal.reason, inline=False)
                await user.send(embed=user_embed)
            except Exception as e:
                logger.error(f"Error notifying user: {e}")
                if log_channel:
                    await log_channel.send(f"Could not DM user {user.mention} about their decline.")
            
            await interaction.followup.send("Application declined.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in decline process: {e}")
            await interaction.followup.send(
                "An error occurred during decline. Please check logs.",
                ephemeral=True
            )

# Decline Reason Modal
class DeclineReasonModal(ui.Modal, title="Decline Reason"):
    def __init__(self, application_id: int):
        super().__init__()
        self.application_id = application_id
        self.reason = None
        self.add_item(ui.TextInput(
            label="Reason for Decline",
            style=discord.TextStyle.long,
            required=True
        ))
    
    async def on_submit(self, interaction: discord.Interaction):
        self.reason = self.children[0].value
        await update_application_status(
            self.application_id,
            status='declined',
            moderator_id=interaction.user.id,
            reason=self.reason
        )
        await interaction.response.defer()

# Application Button View
class ApplicationButtonView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Apply for Allowlist", style=discord.ButtonStyle.blurple, custom_id="apply_btn")
    async def apply_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            # First check cooldown status
            if not await is_cooldown_exempt(interaction.user.id):
                last_app = await get_user_last_application(interaction.user.id)
                if last_app and (datetime.now() - last_app).total_seconds() < APPLICATION_COOLDOWN:
                    return await interaction.response.send_message(
                        "You can apply only once in 24 hours. Please try again later.",
                        ephemeral=True
                    )
            
            # If no cooldown, send the modal as the initial response
            await interaction.response.send_modal(ApplicationModal())
            
        except Exception as e:
            logger.error(f"Error handling apply button: {e}")
            await interaction.response.send_message(
                "An error occurred while checking your cooldown status. Please try again later.",
                ephemeral=True
            )

# Bot Events and Commands
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await init_db()
    
    # Restore application button view in application channel
    try:
        app_channel = bot.get_channel(APPLICATION_CHANNEL_ID)
        if app_channel:
            async for message in app_channel.history(limit=10):
                if message.components:
                    view = ApplicationButtonView()
                    bot.add_view(view, message_id=message.id)
                    break
    except Exception as e:
        logger.error(f"Error restoring application button view: {e}")
    
    # Restore mod review views for pending applications
    async with pool.acquire() as conn:
        pending_apps = await conn.fetch(
            "SELECT id, message_id FROM new_applications WHERE status = 'pending'"
        )
        
        for app in pending_apps:
            try:
                if app['message_id']:
                    channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
                    if channel:
                        try:
                            message = await channel.fetch_message(app['message_id'])
                            view = ApplicationReviewView(app['id'])
                            await message.edit(view=view)
                            bot.add_view(view, message_id=message.id)
                        except discord.NotFound:
                            logger.warning(f"Message {app['message_id']} not found, skipping")
                        except discord.Forbidden:
                            logger.warning(f"No permission to access message {app['message_id']}, skipping")
            except Exception as e:
                logger.error(f"Failed to restore view for application {app['id']}: {e}")
    
    # Add any other persistent views here
    bot.add_view(ApplicationButtonView())  # For the application button

@bot.command()
@commands.has_permissions(manage_guild=True)
async def setup_application(ctx):
    """Setup the application message in this channel"""
    if ctx.channel.id != APPLICATION_CHANNEL_ID:
        return await ctx.send(
            f"Please run this command in <#{APPLICATION_CHANNEL_ID}>.",
            ephemeral=True
        )

    try:
        async for message in ctx.channel.history(limit=10):
            if message.components or (message.embeds and message.embeds[0].title == "Allowlist Application"):
                await message.delete()
    except Exception as e:
        logger.error(f"Error deleting old messages: {e}")

    embed = await create_apply_channel_embed()
    view = ApplicationButtonView()
    await ctx.send(embed=embed, view=view)
    bot.add_view(view)
    
    await ctx.send("Application system has been set up!", ephemeral=True)

@bot.command()
async def cooldown_exempt(ctx, user: discord.Member, action: str):
    """Add or remove a user from cooldown exemption"""
    if not (ctx.channel.id == COOLDOWN_MANAGEMENT_CHANNEL_ID or ctx.author.guild_permissions.administrator):
        return await ctx.send(
            f"This command can only be used in <#{COOLDOWN_MANAGEMENT_CHANNEL_ID}> or by administrators.",
            ephemeral=True
        )

    if action.lower() in ['add', 'grant']:
        await add_cooldown_exempt(user.id)
        await ctx.send(f"{user.mention} has been granted cooldown exemption.")
    elif action.lower() in ['remove', 'revoke']:
        await remove_cooldown_exempt(user.id)
        await ctx.send(f"{user.mention} has been removed from cooldown exemption.")
    else:
        await ctx.send("Invalid action. Use 'add' or 'remove'.", ephemeral=True)
        
@bot.command()
@commands.has_permissions(administrator=True)
async def setup_cooldown_channel(ctx):
    """Setup the cooldown management channel"""
    embed = discord.Embed(
        title="Cooldown Management",
        description="Use `!cooldown_exempt @user add/remove` to manage cooldown exemptions.",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)
    await ctx.send("Cooldown management channel has been set up!", ephemeral=True)

# Error Handler
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error(f"Error in command {ctx.command}: {error}", exc_info=True)
    await ctx.send("An error occurred while executing that command.", ephemeral=True)

# New utility functions
def split_message(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split a long message into chunks that fit within Discord's message limit."""
    if len(content) <= max_length:
        return [content]
    
    chunks = []
    while content:
        # Find the last space or newline within the limit
        split_at = max_length
        if len(content) > max_length:
            split_at = content.rfind('\n', 0, max_length)
            if split_at == -1:
                split_at = content.rfind(' ', 0, max_length)
                if split_at == -1:
                    split_at = max_length
        
        chunk = content[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        content = content[split_at:].strip()
    
    return chunks

# Add this function to check roles
def has_allowed_role(member: discord.Member, allowed_role_names: list) -> bool:
    """Check if member has any of the allowed roles."""
    return any(role.name.lower() in [r.lower() for r in allowed_role_names] for role in member.roles)

# New commands (add to your existing commands)
@bot.command()
async def announce(ctx, channel: discord.TextChannel = None):
    """Send a long announcement (Restricted to specific roles)"""
    # List of allowed role names (case-insensitive)
    ALLOWED_ROLES = [".", "MANAGEMENT"]  # 👈 Change these to your role names
    
    if not has_allowed_role(ctx.author, ALLOWED_ROLES):
        return await ctx.send(
            "❌ You need an **Admin** role to use this command!",
            ephemeral=True
        )
    
    """Send a long announcement to a specific channel (supports multiple messages and attachments)"""
    if channel is None:
        channel = ctx.channel
    
    # Collect the long message from multiple user messages
    content, attachments = await gather_long_message(ctx)
    if content is None:  # Was cancelled
        return
    
    # Create a preview
    preview = content[:500] + ("..." if len(content) > 500 else "")
    embed = discord.Embed(
        title="Announcement Preview",
        description=f"**First 500 characters:**\n{preview}\n\n"
                   f"Total length: {len(content)} characters\n"
                   f"Will be split into {len(split_message(content))} messages\n"
                   f"Attachments: {len(attachments)}",
        color=discord.Color.blue()
    )
    
    view = ConfirmView()
    preview_msg = await ctx.send(
        f"Ready to send to {channel.mention}",
        embed=embed,
        view=view,
        ephemeral=True
    )
    
    # Wait for confirmation
    await view.wait()
    if view.value is None:
        await preview_msg.edit(content="Timed out waiting for confirmation.", view=None)
    elif not view.value:
        await preview_msg.edit(content="Announcement cancelled.", view=None)
    else:
        await preview_msg.edit(content="Sending announcement...", view=None)
        try:
            await send_long_message(channel, content, attachments)
            await preview_msg.edit(content=f"✅ Announcement sent to {channel.mention}!")
        except Exception as e:
            logger.error(f"Error sending announcement: {e}")
            await preview_msg.edit(content=f"❌ Failed to send announcement: {e}")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def longmsg(ctx, channel: discord.TextChannel = None, *, message: str = None):
    """Quick command to send a long message to a channel (alternative to announce)"""
    if channel is None:
        channel = ctx.channel
    
    if not message:
        await ctx.send("Please provide a message to send.", ephemeral=True)
        return
    
    chunks = split_message(message)
    
    try:
        for chunk in chunks:
            await channel.send(chunk)
            if len(chunks) > 1:  # Only delay if we have multiple messages
                await asyncio.sleep(1)
        
        await ctx.send(f"Message sent to {channel.mention} in {len(chunks)} parts.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in longmsg command: {e}")
        await ctx.send("An error occurred while sending your message.", ephemeral=True)

async def gather_long_message(ctx) -> tuple[str, list[discord.Attachment]]:
    """Collect multiple messages from the user and combine them into one content."""
    messages = []
    attachments = []
    
    await ctx.send(
        "**Please send your long announcement (you can send multiple messages):**\n"
        "• Send your text in as many messages as you need\n"
        "• Include any attachments\n"
        "• Type `!done` when finished\n"
        "• Type `!cancel` to abort",
        ephemeral=True
    )
    
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel
    
    while True:
        try:
            msg = await bot.wait_for('message', check=check, timeout=600)  # 10 minute timeout
            
            if msg.content.lower() == '!done':
                break
            if msg.content.lower() == '!cancel':
                await ctx.send("Announcement cancelled.", ephemeral=True)
                return None, None
                
            messages.append(msg.content)
            attachments.extend(msg.attachments)
            
            # Acknowledge each message
            await ctx.send(f"✓ Added message part ({len(messages)} so far)", ephemeral=True)
            
        except asyncio.TimeoutError:
            await ctx.send("Timed out waiting for your messages.", ephemeral=True)
            return None, None
    
    combined = "\n\n".join(messages)
    return combined, attachments

async def send_long_message(destination: discord.TextChannel, content: str, attachments: list[discord.Attachment] = None):
    """Send a potentially long message with attachments to a channel."""
    # First upload all attachments so they're available
    attachment_urls = []
    if attachments:
        for att in attachments:
            try:
                file = await att.to_file()
                msg = await destination.send(file=file)
                attachment_urls.append(msg.attachments[0].url)
            except Exception as e:
                logger.error(f"Failed to upload attachment: {e}")
                attachment_urls.append(f"[Failed to upload {att.filename}]")
    
    # Then send the text content in chunks
    chunks = split_message(content)
    
    for i, chunk in enumerate(chunks):
        # Include attachment URLs at the end of the first message
        if i == 0 and attachment_urls:
            chunk += "\n\n**Attachments:**\n" + "\n".join(f"• {url}" for url in attachment_urls)
        
        await destination.send(chunk)
        if i < len(chunks) - 1:  # Don't wait after last message
            await asyncio.sleep(1)

class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.value = None
    
    @discord.ui.button(label="Send", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.value = True
        self.stop()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.value = False
        self.stop()

async def wait_for_db(max_retries=5, delay=5):
    retries = 0
    while retries < max_retries:
        try:
            await create_db_pool()
            return True
        except Exception as e:
            retries += 1
            logger.warning(f"Database connection failed (attempt {retries}/{max_retries}): {e}")
            if retries < max_retries:
                await asyncio.sleep(delay)
    return False

async def run_web_server():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        log_level="info",
        access_log=False
    )
    server = uvicorn.Server(config)
    await server.serve()

# Main Function
async def main():
    try:
        if not await wait_for_db():
            logger.critical("Could not establish database connection after multiple attempts")
            return

        # Run both the bot and web server concurrently
        await asyncio.gather(
            bot.start(os.getenv('DISCORD_TOKEN')),
            run_web_server()
        )
    except Exception as e:
        logger.critical(f"Bot crashed: {e}", exc_info=True)
    finally:
        if pool:
            await pool.close()
        logger.info("Bot has shut down")