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
            except Exception as e:
                failed.append(pip_name)
    
    if failed:
        print(f"CRITICAL: Failed to install: {failed}")
        sys.exit(1)
    importlib.invalidate_caches()

install_packages()

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
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_FILE = os.getenv("DB_FILE", "cards.db")
DEFAULT_DROP_INTERVAL = 600

# --- Constants ---
RARITY_LABELS = ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic", "Divine", "Celestial", "Supreme", "Animated"]
RARITY_EMOJIS = ["⚪", "🟢", "🔵", "🟣", "🟠", "🔴", "🟡", "💎", "👑", "✨"]

# --- Database & Helpers ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 100, last_daily TEXT);
        CREATE TABLE IF NOT EXISTS sudo_users (id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS bans (id INTEGER PRIMARY KEY, reason TEXT);
        CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, movie TEXT, file_id TEXT, file_type TEXT, rarity INTEGER, animated INTEGER DEFAULT 0, creator INTEGER, created_at TEXT);
        CREATE TABLE IF NOT EXISTS drops (id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER, chat_id INTEGER, message_id INTEGER, dropped_at TEXT, caught_by INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS marriages (id INTEGER PRIMARY KEY AUTOINCREMENT, user1 INTEGER, user2 INTEGER, at TEXT);
        INSERT OR IGNORE INTO settings (key, value) VALUES ('drop_interval', '600');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('drop_chats', '');
        """)
        await db.commit()

def get_rarity_text(r: int) -> str:
    if 0 <= r < len(RARITY_LABELS):
        return f"{RARITY_EMOJIS[r]} {RARITY_LABELS[r]}"
    return "Unknown"

async def is_sudo(user_id: int) -> bool:
    if user_id == OWNER_ID: return True
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT 1 FROM sudo_users WHERE id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None

async def ensure_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, username, balance) VALUES (?,?,?)", (user_id, username, 100))
        await db.commit()

# --- Decorators ---
def sudo_restricted(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_sudo(update.effective_user.id):
            await update.message.reply_text("⛔ Sudo only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Drop Logic ---
async def spawn_drop(app: Application, chat_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id, name, file_id, file_type, rarity FROM cards ORDER BY RANDOM() LIMIT 1") as cur:
            card = await cur.fetchone()
        if not card: return
        
        cid, name, fid, ftype, rarity = card
        caption = f"🎴 **A WILD CARD APPEARED!**\n\nName: **{name}**\nRarity: {get_rarity_text(rarity)}\n\n/Catch to grab it!"
        
        try:
            if ftype == "photo":
                msg = await app.bot.send_photo(chat_id, photo=fid, caption=caption, parse_mode=ParseMode.MARKDOWN)
            else:
                msg = await app.bot.send_video(chat_id, video=fid, caption=caption, parse_mode=ParseMode.MARKDOWN)
            
            await db.execute("INSERT INTO drops(card_id, chat_id, message_id, dropped_at) VALUES (?,?,?,?)", 
                            (cid, chat_id, msg.message_id, datetime.utcnow().isoformat()))
            await db.commit()
        except Exception as e:
            logger.error(f"Drop error: {e}")

async def drop_loop_task(app: Application):
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT value FROM settings WHERE key='drop_interval'") as cur:
                    interval = int((await cur.fetchone())[0])
                async with db.execute("SELECT value FROM settings WHERE key='drop_chats'") as cur:
                    chats_str = (await cur.fetchone())[0]
            
            if chats_str:
                for cid in [int(c) for c in chats_str.split(",") if c]:
                    await spawn_drop(app, cid)
            await asyncio.sleep(interval)
        except Exception as e:
            logger.error(f"Loop error: {e}")
            await asyncio.sleep(60)

# --- Commands ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user.id, update.effective_user.first_name)
    await update.message.reply_text("👋 Card Bot Active! Use /help to see commands.")

@sudo_restricted
async def add_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT value FROM settings WHERE key='drop_chats'") as cur:
            current = (await cur.fetchone())[0]
        new_chats = f"{current},{cid}" if current else cid
        await db.execute("UPDATE settings SET value=? WHERE key='drop_chats'", (new_chats,))
        await db.commit()
    await update.message.reply_text("✅ Chat added to drop list.")

async def catch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await ensure_user(uid, update.effective_user.first_name)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id, card_id FROM drops WHERE chat_id=? AND caught_by=0 ORDER BY id DESC LIMIT 1", (update.effective_chat.id,)) as cur:
            drop = await cur.fetchone()
        if not drop:
            await update.message.reply_text("❌ No active drops!")
            return
        
        await db.execute("UPDATE drops SET caught_by=? WHERE id=?", (uid, drop[0]))
        await db.execute("UPDATE users SET balance = balance + 100 WHERE id=?", (uid,))
        await db.commit()
        await update.message.reply_text(f"🎉 Caught! +100 coins.")

# --- Main ---
async def post_init(app: Application):
    await init_db()
    asyncio.create_task(drop_loop_task(app))

def main():
    if not TOKEN: return
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("catch", catch_cmd))
    app.add_handler(CommandHandler("addchat", add_chat_cmd))
    # Add other commands here...

    print("🤖 Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
