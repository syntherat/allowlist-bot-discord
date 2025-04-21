# Discord Allowlist Application Bot

A robust Discord bot that manages allowlist applications with:
- Application forms with modals
- Moderation review system
- Role assignment
- Cooldown management
- PostgreSQL database integration

## Features

✅ **User Application System**  
- Steam Hex ID, character info, and backstory collection
- Age verification (18+)
- 24-hour cooldown between applications (configurable)

🛠 **Moderation Tools**  
- Dedicated mod review channel
- Approve/decline buttons with reason input
- Logging of all actions

⚙ **Admin Controls**  
- Cooldown exemption management
- Role hierarchy verification
- Database maintenance commands

## Setup Guide

### Prerequisites
- Python 3.10+
- PostgreSQL database
- Discord bot token with:
  - `Manage Roles` permission
  - `Administrator` recommended for easier setup

### Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/allowlist-bot-discord/allowlist-bot.git
   cd allowlist-bot

## Usage

### Bot Commands

| Command                                      | Description                        | Permission Required        |
|---------------------------------------------|------------------------------------|----------------------------|
| `!setup_application`                        | Creates application button         | Manage Server              |
| `!cooldown_exempt @user [add/remove]`       | Manage cooldown exemptions         | Admin/Cooldown Channel     |
| `!list_exempt`                              | Show cooldown-exempt users         | Admin                      |
| `!check_role_hierarchy`                     | Verify role permissions            | Admin                      |
