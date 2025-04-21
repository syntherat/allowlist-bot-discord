import discord
from discord.ext import commands, tasks
from discord import ui
import asyncpg
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List
from io import StringIO

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

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
    
    # Truncate and add "View full story" button
    truncated_story = (application['character_story'][:1000] + '...') if len(application['character_story']) > 1000 else application['character_story']
    embed.add_field(name="Character Story", value=truncated_story, inline=False)
    
    if len(application['character_story']) > 1000:
        embed.add_field(name="Note", value="*Story truncated. View full story in attached file.*", inline=False)
    
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
        print(f"Error creating approved log embed: {e}")
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
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

async def init_db():
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                user_name TEXT NOT NULL,
                steam_hex TEXT NOT NULL,
                real_name TEXT NOT NULL,
                character_name TEXT NOT NULL,
                age INTEGER NOT NULL,
                character_story TEXT NOT NULL,
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

async def get_application(application_id: int) -> Optional[dict]:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM applications WHERE id = $1", 
            application_id
        )

async def get_user_last_application(user_id: int) -> Optional[datetime]:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT last_application FROM applications WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
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

async def remove_cooldown_exempt(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM cooldown_exempt WHERE user_id = $1",
            user_id
        )

async def create_application(data: dict) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO applications (
                user_id, user_name, steam_hex, real_name, 
                character_name, age, character_story,
                last_application
            ) 
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            RETURNING id
            """,
            data['user_id'], data['user_name'], data['steam_hex'],
            data['real_name'], data['character_name'], data['age'],
            data['character_story']
        )

async def update_application_message_id(application_id: int, message_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE applications SET message_id = $1 WHERE id = $2",
            message_id, application_id
        )

async def update_application_status(application_id: int, status: str, 
                                 moderator_id: int = None, reason: str = None):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE applications 
            SET status = $1, 
                mod_reason = $2, 
                moderator_id = $3,
                updated_at = NOW()
            WHERE id = $4
            """,
            status, reason, moderator_id, application_id
        )

async def assign_allowlisted_role(user_id: int, guild_id: int):
    guild = bot.get_guild(guild_id)
    if not guild:
        return False
    
    member = guild.get_member(user_id)
    if not member:
        return False
    
    role = guild.get_role(ALLOWLISTED_ROLE_ID)
    if not role:
        return False
    
    try:
        await member.add_roles(role)
        return True
    except Exception as e:
        print(f"Error assigning role: {e}")
        return False

async def send_to_mod_channel(user: discord.User, application_id: int):
    """Send application to mod review channel with file attachment if story is long"""
    mod_channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
    if not mod_channel:
        return
    
    application = await get_application(application_id)
    if not application:
        return
    
    # Create the embed
    embed = discord.Embed(
        title=f"Allowlist Application - {user.display_name}",
        color=discord.Color.blue(),
        timestamp=application['created_at']
    )
    embed.add_field(name="Steam Hex ID", value=application['steam_hex'], inline=False)
    embed.add_field(name="Real Name", value=application['real_name'], inline=True)
    embed.add_field(name="Character Name", value=application['character_name'], inline=True)
    embed.add_field(name="Age", value=application['age'], inline=True)
    
    # Prepare the story text
    story = application['character_story']
    view = ApplicationReviewView(application_id)
    
    # If story is too long for a field (1024 chars)
    if len(story) > 1024:
        # Truncate for the embed
        truncated_story = story[:1000] + '...'
        embed.add_field(name="Character Story", value=truncated_story, inline=False)
        embed.add_field(name="Note", value="Full story attached as a file below.", inline=False)
        
        # Create a text file with the full story
        from io import StringIO
        file = discord.File(
            StringIO(story),
            filename=f"character_story_{user.id}.txt"
        )
        
        # Send both embed and file
        message = await mod_channel.send(embed=embed, view=view, file=file)
    else:
        # Story fits in the embed field
        embed.add_field(name="Character Story", value=story, inline=False)
        message = await mod_channel.send(embed=embed, view=view)
    
    await update_application_message_id(application_id, message.id)

# Application Modal
class ApplicationModal(ui.Modal, title="Allowlist Application"):
    def __init__(self):
        super().__init__()
        self.add_item(ui.TextInput(label="Steam Hex ID", required=True))
        self.add_item(ui.TextInput(label="Real Name", required=True))
        self.add_item(ui.TextInput(label="Character Name", required=True))
        self.add_item(ui.TextInput(label="Age", required=True))
        self.add_item(ui.TextInput(label="Character Story", style=discord.TextStyle.long, required=True))
    
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
                'age': age,
                'character_story': self.children[4].value
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
            print(f"Error creating application: {e}")
            await interaction.response.send_message(
                "An error occurred while submitting your application. Please try again later.",
                ephemeral=True
            )

# Application Review View
class ApplicationReviewView(ui.View):
    def __init__(self, application_id: int):
        super().__init__(timeout=None)
        self.application_id = application_id
    
    # In the ApplicationReviewView class (approve button handler)
    @ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="approve_btn")
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await update_application_status(
                self.application_id,
                status='approved',
                moderator_id=interaction.user.id
            )
            
            # Get the application details
            application = await get_application(self.application_id)
            if not application:
                return await interaction.response.send_message("Application not found!", ephemeral=True)
            
            # Get the user object
            user = await bot.fetch_user(application['user_id'])
            
            # Update the review message
            self.clear_items()
            await interaction.message.edit(view=self)
            
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.title = f"APPROVED - {embed.title}"
            await interaction.message.edit(embed=embed)
            
            # Assign allowlisted role
            role_assigned = await assign_allowlisted_role(application['user_id'], interaction.guild.id)
            
            # Send to logs channel
            log_channel = bot.get_channel(LOGS_CHANNEL_ID)
            if log_channel:
                log_embed = await create_approved_log_embed(user, interaction.user)
                if not role_assigned:
                    log_embed.add_field(name="Warning", value="Failed to assign allowlisted role", inline=False)
                await log_channel.send(embed=log_embed)
            
            # Notify user
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
                print(f"Error notifying user: {e}")
                if log_channel:
                    await log_channel.send(f"Could not DM user {user.mention} about their approval.")
            
            await interaction.response.send_message(
                f"Application approved. {'Role assigned successfully.' if role_assigned else 'Failed to assign role.'}",
                ephemeral=True
            )
        except Exception as e:
            print(f"Error in approval process: {e}")
            await interaction.response.send_message(
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
                
            # Get application details
            application = await get_application(self.application_id)
            if not application:
                return await interaction.followup.send("Application not found!", ephemeral=True)
            
            # Get user object
            user = await bot.fetch_user(application['user_id'])
            
            # Update the review message
            self.clear_items()
            await interaction.message.edit(view=self)
            
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.red()
            embed.title = f"DECLINED - {embed.title}"
            if modal.reason:
                embed.add_field(name="Reason", value=modal.reason, inline=False)
            await interaction.message.edit(embed=embed)
            
            # Send to logs channel
            log_channel = bot.get_channel(LOGS_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    embed=await create_declined_log_embed(user, interaction.user, modal.reason)
                )
            
            # Notify user
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
                print(f"Error notifying user: {e}")
                if log_channel:
                    await log_channel.send(f"Could not DM user {user.mention} about their decline.")
            
            await interaction.followup.send("Application declined.", ephemeral=True)
        except Exception as e:
            print(f"Error in decline process: {e}")
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
            if not await is_cooldown_exempt(interaction.user.id):
                last_app = await get_user_last_application(interaction.user.id)
                if last_app and (datetime.now() - last_app).total_seconds() < APPLICATION_COOLDOWN:
                    # remaining = timedelta(seconds=APPLICATION_COOLDOWN) - (datetime.now() - last_app)
                    return await interaction.response.send_message(
                        # f"You can apply again in {remaining}.",
                        f"You can apply only once in 24 hours. Please try again later.",
                        ephemeral=True
                    )

            await interaction.response.send_modal(ApplicationModal())
        except Exception as e:
            print(f"Error handling apply button: {e}")
            await interaction.response.send_message(
                "An error occurred while checking your cooldown status. Please try again later.",
                ephemeral=True
            )


# Bot Events and Commands
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await init_db()
    
    # Restore application button view in application channel
    try:
        app_channel = bot.get_channel(APPLICATION_CHANNEL_ID)
        if app_channel:
            # Get the last message with the application button
            async for message in app_channel.history(limit=10):
                if message.components:  # If message has components (buttons)
                    view = ApplicationButtonView()
                    bot.add_view(view, message_id=message.id)
                    break  # Only need to restore the most recent one
    except Exception as e:
        print(f"Error restoring application button view: {e}")
    
    # Restore mod review views for pending applications
    async with pool.acquire() as conn:
        pending_apps = await conn.fetch(
            "SELECT id, message_id FROM applications WHERE status = 'pending'"
        )
        
        for app in pending_apps:
            try:
                if app['message_id']:
                    channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
                    if channel:
                        message = await channel.fetch_message(app['message_id'])
                        view = ApplicationReviewView(app['id'])
                        await message.edit(view=view)
                        bot.add_view(view, message_id=message.id)
            except Exception as e:
                print(f"Failed to restore view for application {app['id']}: {e}")

@bot.command()
@commands.has_permissions(manage_guild=True)
async def setup_application(ctx):
    """Setup the application message in this channel"""
    if ctx.channel.id != APPLICATION_CHANNEL_ID:
        return await ctx.send("Please run this command in the application channel.", ephemeral=True)
    
    embed = await create_apply_channel_embed()
    view = ApplicationButtonView()
    await ctx.send(embed=embed, view=view)
    bot.add_view(view)
    await ctx.send("Application system has been set up!", ephemeral=True)

@bot.command()
async def cooldown_exempt(ctx, user: discord.Member, action: str):
    """Add or remove a user from cooldown exemption"""
    # Check if command is used in the cooldown management channel OR user has admin perms
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

# Main Function
async def main():
    await create_db_pool()
    await bot.start(os.getenv('DISCORD_TOKEN'))

if __name__ == "__main__":
    asyncio.run(main())