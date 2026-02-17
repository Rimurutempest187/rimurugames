
import os
import sys
import subprocess
import importlib
import asyncio
import random
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

# --- Bootstrap: Auto-install missing packages ---

REQUIRED_PACKAGES = [
("aiosqlite", "aiosqlite"),
("dotenv", "python-dotenv"),
("telegram", "python-telegram-bot"),
]

def install_packages():
    """Installs missing packages via pip automatically."""
    failed = []
    for module_name, pip_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(module_name)
        except ImportError:
            print(f"📦 Package '{module_name}' not found. Installing '{pip_name}'...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
                print(f"✅ Installed '{pip_name}'.")
            except subprocess.CalledProcessError as e:
                print(f"❌ Failed to install '{pip_name}': {e}")
                failed.append(pip_name)
    
    if failed:
        print("CRITICAL: Could not install the following packages manually:")
        print("  " + " ".join(failed))
        sys.exit(1)
    
    importlib.invalidate_caches()
# --- Imports (Safe after bootstrap) ---

from dotenv import load_dotenv
import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
ApplicationBuilder,
CommandHandler,
ContextTypes,
MessageHandler,
filters,
Application,
)

# --- Configuration & Logging ---

load_dotenv()

# Enable logging

logging.basicConfig(
format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
level=logging.INFO
)
logger = logging.getLogger(**name**)

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
print("❌ Error: TELEGRAM_TOKEN env variable is missing.")
sys.exit(1)

OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_FILE = os.getenv("DB_FILE", "cards.db")
DEFAULT_DROP_INTERVAL = 600  # 10 minutes

# --- Constants ---

RARITY_LABELS = [
"Common", "Uncommon", "Rare", "Epic", "Legendary",
"Mythic", "Divine", "Celestial", "Supreme", "Animated"
]
RARITY_EMOJIS = ["⚪", "🟢", "🔵", "🟣", "🟠", "🔴", "🟡", "💎", "👑", "✨"]

# --- Database Schema & Init ---

async def init_db():
async with aiosqlite.connect(DB_FILE) as db:
await db.executescript("""
PRAGMA foreign_keys = ON;

```
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER DEFAULT 0,
        last_daily TEXT
    );

    CREATE TABLE IF NOT EXISTS sudo_users (
        id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS bans (
        id INTEGER PRIMARY KEY,
        reason TEXT
    );

    CREATE TABLE IF NOT EXISTS mutes (
        id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        movie TEXT,
        file_id TEXT NOT NULL,
        file_type TEXT NOT NULL,
        rarity INTEGER DEFAULT 0,
        animated INTEGER DEFAULT 0,
        creator INTEGER,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id INTEGER,
        chat_id INTEGER,
        message_id INTEGER,
        dropped_at TEXT,
        caught_by INTEGER DEFAULT 0,
        FOREIGN KEY(card_id) REFERENCES cards(id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        note TEXT,
        at TEXT
    );

    CREATE TABLE IF NOT EXISTS marriages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1 INTEGER,
        user2 INTEGER,
        at TEXT
    );
    
    -- Default settings if not exist
    INSERT OR IGNORE INTO settings (key, value) VALUES ('drop_interval', '600');
    INSERT OR IGNORE INTO settings (key, value) VALUES ('drop_chats', '');
    """)
    await db.commit()
logger.info("Database initialized.")

```

# --- Helpers ---

def get_rarity_text(r: int) -> str:
if 0 <= r < len(RARITY_LABELS):
return f"{RARITY_EMOJIS[r]} {RARITY_LABELS[r]}"
return "Unknown"

async def is_owner(user_id: int) -> bool:
return OWNER_ID != 0 and user_id == OWNER_ID

async def is_sudo(user_id: int) -> bool:
if await is_owner(user_id):
return True
async with aiosqlite.connect(DB_FILE) as db:
async with db.execute("SELECT 1 FROM sudo_users WHERE id = ?", (user_id,)) as cursor:
return await cursor.fetchone() is not None

async def ensure_user(user_id: int, username: str):
async with aiosqlite.connect(DB_FILE) as db:
await db.execute(
"INSERT OR IGNORE INTO users(id, username, balance) VALUES (?,?,?)",
(user_id, username, 100)
)
await db.commit()

async def update_balance(user_id: int, amount: int, note: str = "transaction"):
async with aiosqlite.connect(DB_FILE) as db:
await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
await db.execute(
"INSERT INTO transactions(user_id, amount, note, at) VALUES (?,?,?,?)",
(user_id, amount, note, datetime.utcnow().isoformat())
)
await db.commit()

# --- Decorators / Permissions ---

def sudo_restricted(func):
async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
if not await is_sudo(update.effective_user.id):
await update.message.reply_text("⛔ Access denied. Sudo/Owner only.")
return
return await func(update, context, *args, **kwargs)
return wrapper

def owner_restricted(func):
async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
if not await is_owner(update.effective_user.id):
await update.message.reply_text("⛔ Access denied. Owner only.")
return
return await func(update, context, *args, **kwargs)
return wrapper

# --- Admin Commands ---

@sudo_restricted
async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
msg = update.message
if not msg.reply_to_message:
await msg.reply_text("Reply to an image/video with: `/upload <name>|<series>|<rarity_0-9>`", parse_mode=ParseMode.MARKDOWN)
return

```
media = msg.reply_to_message
file_id, file_type, animated = None, None, 0

if media.photo:
    file_id = media.photo[-1].file_id
    file_type = "photo"
elif media.video:
    file_id = media.video.file_id
    file_type = "video"
    animated = 1
elif media.document and media.document.mime_type:
    if media.document.mime_type.startswith("image"):
        file_id = media.document.file_id
        file_type = "photo"
    elif media.document.mime_type.startswith("video"):
        file_id = media.document.file_id
        file_type = "video"
        animated = 1

if not file_id:
    await msg.reply_text("❌ Could not detect valid media.")
    return

# Parse arguments
raw_args = " ".join(context.args)
# If no args, check caption of original media
if not raw_args and media.caption:
    raw_args = media.caption

name, movie, rarity = "Unknown", "Unknown", 0

if "|" in raw_args:
    parts = [p.strip() for p in raw_args.split("|")]
    name = parts[0] if len(parts) > 0 else "Unknown"
    movie = parts[1] if len(parts) > 1 else "Unknown"
    try:
        rarity = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        rarity = 0
else:
    # Simple usage: /upload Name
    if raw_args:
        name = raw_args

async with aiosqlite.connect(DB_FILE) as db:
    await db.execute(
        "INSERT INTO cards(name, movie, file_id, file_type, rarity, animated, creator, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, movie, file_id, file_type, rarity, animated, update.effective_user.id, datetime.utcnow().isoformat())
    )
    await db.commit()

await msg.reply_text(f"✅ **Uploaded:** {name}\n🎥 **Series:** {movie}\n{get_rarity_text(rarity)}", parse_mode=ParseMode.MARKDOWN)

```

@sudo_restricted
async def add_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
chat_id = update.effective_chat.id
if update.effective_chat.type == "private":
await update.message.reply_text("Use this command in a group to enable drops there.")
return

```
async with aiosqlite.connect(DB_FILE) as db:
    async with db.execute("SELECT value FROM settings WHERE key='drop_chats'") as cursor:
        row = await cursor.fetchone()
        current_chats = row[0] if row else ""
    
    chat_list = current_chats.split(",") if current_chats else []
    if str(chat_id) in chat_list:
        await update.message.reply_text("✅ This chat is already in the drop list.")
        return
        
    chat_list.append(str(chat_id))
    new_val = ",".join(chat_list)
    await db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('drop_chats', ?)", (new_val,))
    await db.commit()
    
await update.message.reply_text(f"✅ Added {update.effective_chat.title} (ID: {chat_id}) to drop list.")

```

@sudo_restricted
async def remove_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
chat_id = update.effective_chat.id
async with aiosqlite.connect(DB_FILE) as db:
async with db.execute("SELECT value FROM settings WHERE key='drop_chats'") as cursor:
row = await cursor.fetchone()
current_chats = row[0] if row else ""

```
    chat_list = current_chats.split(",") if current_chats else []
    if str(chat_id) not in chat_list:
        await update.message.reply_text("⚠️ This chat is not in the drop list.")
        return
        
    chat_list.remove(str(chat_id))
    new_val = ",".join(chat_list)
    await db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('drop_chats', ?)", (new_val,))
    await db.commit()

await update.message.reply_text(f"🗑 Removed {update.effective_chat.title} from drop list.")

```

@sudo_restricted
async def set_drop_interval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not context.args:
await update.message.reply_text("Usage: `/setdrop <seconds>`", parse_mode=ParseMode.MARKDOWN)
return
try:
seconds = int(context.args[0])
if seconds < 10:
await update.message.reply_text("Minimum interval is 10 seconds.")
return
async with aiosqlite.connect(DB_FILE) as db:
await db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('drop_interval', ?)", (str(seconds),))
await db.commit()
await update.message.reply_text(f"⏱ Drop interval updated to {seconds} seconds.")
except ValueError:
await update.message.reply_text("Please provide a valid number.")

@owner_restricted
async def add_sudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
msg = update.message
target_id = None
if msg.reply_to_message:
target_id = msg.reply_to_message.from_user.id
elif context.args:
try:
target_id = int(context.args[0])
except ValueError:
pass

```
if not target_id:
    await msg.reply_text("Usage: Reply to user or `/addsudo <id>`")
    return

async with aiosqlite.connect(DB_FILE) as db:
    await db.execute("INSERT OR IGNORE INTO sudo_users(id) VALUES (?)", (target_id,))
    await db.commit()
await msg.reply_text(f"✅ User {target_id} is now Sudo.")

```

# --- Drop Logic ---

async def spawn_drop(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
async with aiosqlite.connect(DB_FILE) as db:
# Get random card
async with db.execute("SELECT id, name, file_id, file_type, rarity FROM cards ORDER BY RANDOM() LIMIT 1") as cursor:
card = await cursor.fetchone()

```
    if not card:
        return # No cards in DB
    
    cid, name, fid, ftype, rarity = card
    caption = f"🎴 **A WILD CARD APPEARED!**\n\nName: **{name}**\nRarity: {get_rarity_text(rarity)}\n\n/Catch to grab it!"
    
    try:
        msg = None
        if ftype == "photo":
            msg = await context.bot.send_photo(chat_id, photo=fid, caption=caption, parse_mode=ParseMode.MARKDOWN)
        elif ftype == "video":
            msg = await context.bot.send_video(chat_id, video=fid, caption=caption, parse_mode=ParseMode.MARKDOWN)
        
        if msg:
            await db.execute(
                "INSERT INTO drops(card_id, chat_id, message_id, dropped_at) VALUES (?,?,?,?)",
                (cid, chat_id, msg.id, datetime.utcnow().isoformat())
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to send drop to {chat_id}: {e}")

```

async def drop_loop_task(app: Application):
"""Background task to manage drops."""
logger.info("Starting Drop Loop...")
while True:
try:
async with aiosqlite.connect(DB_FILE) as db:
# Get settings
async with db.execute("SELECT value FROM settings WHERE key='drop_interval'") as cursor:
row = await cursor.fetchone()
interval = int(row[0]) if row else DEFAULT_DROP_INTERVAL

```
            async with db.execute("SELECT value FROM settings WHERE key='drop_chats'") as cursor:
                row = await cursor.fetchone()
                chats_str = row[0] if row else ""
        
        if chats_str:
            chat_ids = [int(c) for c in chats_str.split(",") if c.strip()]
            for cid in chat_ids:
                await spawn_drop(app, cid)
                await asyncio.sleep(5) # Delay between groups to avoid flood wait
        
        await asyncio.sleep(interval)
        
    except asyncio.CancelledError:
        break
    except Exception as e:
        logger.error(f"Error in drop loop: {e}")
        await asyncio.sleep(60)

```

# --- User Commands ---

async def catch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
chat = update.effective_chat
await ensure_user(user.id, user.first_name)

```
async with aiosqlite.connect(DB_FILE) as db:
    # Check for latest active drop in this chat
    async with db.execute(
        "SELECT id, card_id, dropped_at FROM drops WHERE chat_id = ? AND caught_by = 0 ORDER BY id DESC LIMIT 1",
        (chat.id,)
    ) as cursor:
        drop = await cursor.fetchone()
    
    if not drop:
        await update.message.reply_text("There are no cards to catch right now.")
        return

    drop_id, card_id, dropped_at = drop
    
    # Lock the drop
    await db.execute("UPDATE drops SET caught_by = ? WHERE id = ? AND caught_by = 0", (user.id, drop_id))
    await db.commit()
    
    # Verify if update worked (concurrency check)
    async with db.execute("SELECT caught_by FROM drops WHERE id = ?", (drop_id,)) as cursor:
        row = await cursor.fetchone()
        if not row or row[0] != user.id:
            await update.message.reply_text("🏃‍♂️ Too slow! Someone else caught it.")
            return

    # Fetch Card Details
    async with db.execute("SELECT name, rarity FROM cards WHERE id = ?", (card_id,)) as cursor:
        card_info = await cursor.fetchone()
        c_name, c_rarity = card_info
    
    reward = 50 + (c_rarity * 10)
    await update_balance(user.id, reward, f"Caught {c_name}")
    
    await update.message.reply_text(
        f"🎉 **{user.first_name}** caught **{c_name}**!\n"
        f"💰 Added {reward} coins to your wallet.",
        parse_mode=ParseMode.MARKDOWN
    )

```

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
await ensure_user(user.id, user.first_name)
txt = (
f"👋 Hello {user.first_name}!\n\n"
"I am a Card Drop Bot. I will drop cards in registered groups.\n"
"Use /help to see commands."
)
await update.message.reply_text(txt)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
txt = (
"📚 **COMMAND LIST**\n\n"
"**User Commands:**\n"
"/Catch - Catch the last dropped card\n"
"/balance - Check your wallet\n"
"/daily - Claim daily reward\n"
"/slots <amount> - Play slots\n"
"/marry <reply> - Marry a user\n"
"/divorce - Divorce current partner\n\n"
"**Admin Commands:**\n"
"/upload - Reply to image to add card\n"
"/addchat - Enable drops in this group\n"
"/rmchat - Disable drops in this group\n"
"/setdrop <seconds> - Set drop interval\n"
"/addsudo <id> - Add admin (Owner only)"
)
await update.message.reply_markdown(txt)

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
await ensure_user(user.id, user.first_name)
async with aiosqlite.connect(DB_FILE) as db:
async with db.execute("SELECT balance FROM users WHERE id = ?", (user.id,)) as cursor:
row = await cursor.fetchone()
bal = row[0] if row else 0
await update.message.reply_text(f"💰 **Wallet:** {bal} coins", parse_mode=ParseMode.MARKDOWN)

async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
await ensure_user(user.id, user.first_name)

```
async with aiosqlite.connect(DB_FILE) as db:
    async with db.execute("SELECT last_daily FROM users WHERE id = ?", (user.id,)) as cursor:
        row = await cursor.fetchone()
        last_date_str = row[0]
    
    now = datetime.utcnow()
    if last_date_str:
        last_date = datetime.fromisoformat(last_date_str)
        if now - last_date < timedelta(hours=24):
            next_time = last_date + timedelta(hours=24)
            remaining = next_time - now
            hours, remainder = divmod(remaining.seconds, 3600)
            mins, _ = divmod(remainder, 60)
            await update.message.reply_text(f"⏳ Come back in {hours}h {mins}m.")
            return

    amount = random.randint(100, 300)
    await db.execute("UPDATE users SET balance = balance + ?, last_daily = ? WHERE id = ?", (amount, now.isoformat(), user.id))
    await db.commit()
    await update.message.reply_text(f"☀️ Daily reward claimed: **{amount} coins**!", parse_mode=ParseMode.MARKDOWN)

```

async def slots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
await ensure_user(user.id, user.first_name)

```
if not context.args:
    await update.message.reply_text("Usage: `/slots <amount>`", parse_mode=ParseMode.MARKDOWN)
    return

try:
    bet = int(context.args[0])
    if bet < 1: raise ValueError
except ValueError:
    await update.message.reply_text("Invalid bet amount.")
    return

async with aiosqlite.connect(DB_FILE) as db:
    async with db.execute("SELECT balance FROM users WHERE id = ?", (user.id,)) as cursor:
        bal = (await cursor.fetchone())[0]
    
    if bal < bet:
        await update.message.reply_text("💸 Insufficient funds.")
        return

    # Game Logic
    items = ["🍒", "🍋", "🍇", "7️⃣", "💎"]
    slots = [random.choice(items) for _ in range(3)]
    
    winnings = 0
    result_text = f"🎰 | {' | '.join(slots)} | 🎰"
    
    if slots[0] == slots[1] == slots[2]:
        winnings = bet * 5
        result_text += f"\n\n🔥 **JACKPOT!** You won {winnings} coins!"
    elif slots[0] == slots[1] or slots[1] == slots[2] or slots[0] == slots[2]:
        winnings = int(bet * 1.5)
        result_text += f"\n\n✨ Small match! You won {winnings} coins."
    else:
        winnings = -bet
        result_text += f"\n\n📉 You lost {bet} coins."

    await update_balance(user.id, winnings, "Slots")
    await update.message.reply_text(result_text, parse_mode=ParseMode.MARKDOWN)

```

async def marry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
msg = update.message
if not msg.reply_to_message:
await msg.reply_text("Reply to the user you want to marry.")
return

```
u1 = update.effective_user
u2 = msg.reply_to_message.from_user

if u2.is_bot or u1.id == u2.id:
    await msg.reply_text("You cannot marry bots or yourself.")
    return

async with aiosqlite.connect(DB_FILE) as db:
    # Check if already married
    async with db.execute("SELECT 1 FROM marriages WHERE user1=? OR user2=? OR user1=? OR user2=?", (u1.id, u1.id, u2.id, u2.id)) as cursor:
        if await cursor.fetchone():
            await msg.reply_text("One of you is already married!")
            return
    
    await db.execute("INSERT INTO marriages(user1, user2, at) VALUES (?,?,?)", (u1.id, u2.id, datetime.utcnow().isoformat()))
    await db.commit()

await msg.reply_text(f"💍 **{u1.first_name}** and **{u2.first_name}** are now married!", parse_mode=ParseMode.MARKDOWN)

```

async def divorce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
uid = update.effective_user.id
async with aiosqlite.connect(DB_FILE) as db:
res = await db.execute("DELETE FROM marriages WHERE user1=? OR user2=?", (uid, uid))
await db.commit()
if res.rowcount > 0:
await update.message.reply_text("💔 You are now single.")
else:
await update.message.reply_text("You aren't married.")

# --- Initialization & Main ---

async def post_init(app: Application):
"""Lifecycle hook: runs after app is initialized but before polling."""
await init_db()
# Add the background drop task to the application's task loop
app.create_task(drop_loop_task(app))

def main():
if not TOKEN:
return

```
application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

# Handlers
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("balance", balance_cmd))
application.add_handler(CommandHandler("daily", daily_cmd))
application.add_handler(CommandHandler("slots", slots_cmd))
application.add_handler(CommandHandler("catch", catch_cmd))
application.add_handler(CommandHandler("marry", marry_cmd))
application.add_handler(CommandHandler("divorce", divorce_cmd))

# Sudo/Owner Handlers
application.add_handler(CommandHandler("upload", upload_cmd))
application.add_handler(CommandHandler("addchat", add_chat_cmd))
application.add_handler(CommandHandler("rmchat", remove_chat_cmd))
application.add_handler(CommandHandler("setdrop", set_drop_interval_cmd))
application.add_handler(CommandHandler("addsudo", add_sudo_cmd))

print("🤖 Bot is starting...")
application.run_polling()

```

if **name** == "**main**":
main()
