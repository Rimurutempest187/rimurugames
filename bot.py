import os
import sys
import asyncio
import random
import logging
from datetime import datetime, timedelta

# --- Third Party Imports ---
from dotenv import load_dotenv
import aiosqlite
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Application,
    Defaults
)

# --- Configuration ---
load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_FILE = os.getenv("DB_FILE", "cards.db")

# --- Constants ---
RARITY_LABELS = ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic", "Divine", "Celestial", "Supreme", "Animated"]
RARITY_EMOJIS = ["⚪", "🟢", "🔵", "🟣", "🟠", "🔴", "🟡", "💎", "👑", "✨"]

# --- Database Initialization ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, 
            username TEXT, 
            balance INTEGER DEFAULT 100, 
            last_daily TEXT
        );
        CREATE TABLE IF NOT EXISTS sudo_users (id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            name TEXT, 
            series TEXT, 
            file_id TEXT, 
            file_type TEXT, 
            rarity INTEGER
        );
        CREATE TABLE IF NOT EXISTS drops (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            card_id INTEGER, 
            chat_id INTEGER, 
            message_id INTEGER, 
            caught_by INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS marriages (
            user1 INTEGER, 
            user2 INTEGER, 
            at TEXT
        );
        
        INSERT OR IGNORE INTO settings (key, value) VALUES ('drop_interval', '600');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('drop_chats', '');
        """)
        await db.commit()
    logger.info("✅ Database initialized successfully.")

# --- Helper Functions ---
async def is_sudo(user_id: int) -> bool:
    if user_id == OWNER_ID: return True
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT 1 FROM sudo_users WHERE id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None

async def ensure_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(id, username, balance) VALUES (?,?,?)",
            (user_id, username, 100)
        )
        await db.commit()

# --- Core Logic: Drops ---
async def spawn_drop(app: Application, chat_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id, name, file_id, file_type, rarity FROM cards ORDER BY RANDOM() LIMIT 1") as cur:
            card = await cur.fetchone()
        
        if not card: return
        
        cid, name, fid, ftype, rarity = card
        caption = (
            f"🎴 **A NEW CARD HAS DROPPED!**\n\n"
            f"👤 **Name:** {name}\n"
            f"🌟 **Rarity:** {RARITY_EMOJIS[rarity]} {RARITY_LABELS[rarity]}\n\n"
            f"👉 Use `/catch` to claim this card!"
        )
        
        try:
            if ftype == "photo":
                msg = await app.bot.send_photo(chat_id, photo=fid, caption=caption, parse_mode=ParseMode.MARKDOWN)
            else:
                msg = await app.bot.send_video(chat_id, video=fid, caption=caption, parse_mode=ParseMode.MARKDOWN)
            
            await db.execute(
                "INSERT INTO drops(card_id, chat_id, message_id) VALUES (?,?,?)",
                (cid, chat_id, msg.message_id)
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Failed to drop card in {chat_id}: {e}")

async def drop_loop(app: Application):
    while True:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT value FROM settings WHERE key='drop_interval'") as cur:
                res = await cur.fetchone()
                interval = int(res[0]) if res else 600
            async with db.execute("SELECT value FROM settings WHERE key='drop_chats'") as cur:
                res = await cur.fetchone()
                chats = res[0].split(",") if res and res[0] else []
        
        for chat_id in chats:
            if chat_id:
                await spawn_drop(app, int(chat_id))
                await asyncio.sleep(2) # Avoid spamming
        
        await asyncio.sleep(interval)

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.first_name)
    await update.message.reply_text(
        f"🌟 **Welcome {user.first_name}!**\n\n"
        "ကျွန်တော်ကတော့ Card Drop Bot ဖြစ်ပါတယ်။ Group တွေထဲမှာ ကတ်တွေလိုက်ချပေးမှာဖြစ်ပြီး "
        "စုဆောင်းထားတဲ့ ကတ်တွေကို တခြားသူတွေနဲ့ လဲလှယ်လို့လည်း ရပါတယ်။\n\n"
        "📜 Command တွေကိုကြည့်ဖို့ /help ကိုနှိပ်ပါ။"
    )

async def catch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await ensure_user(user_id, update.effective_user.first_name)

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, card_id FROM drops WHERE chat_id = ? AND caught_by = 0 ORDER BY id DESC LIMIT 1",
            (chat_id,)
        ) as cur:
            drop = await cur.fetchone()
        
        if not drop:
            await update.message.reply_text("❌ ဒီ Group မှာ အခုလောလောဆယ် ဖမ်းစရာကတ်မရှိသေးပါဘူး။")
            return
        
        drop_id, card_id = drop
        await db.execute("UPDATE drops SET caught_by = ? WHERE id = ?", (user_id, drop_id))
        await db.execute("UPDATE users SET balance = balance + 50 WHERE id = ?", (user_id,))
        await db.commit()
        
        await update.message.reply_text(f"🎉 **{update.effective_user.first_name}** ကတ်ကို အမိအရ ဖမ်းလိုက်နိုင်ပါပြီ! (+50 Coins 💰)")

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.now()
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT last_daily FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            last_daily = row[0] if row else None
            
        if last_daily and datetime.fromisoformat(last_daily).date() == now.date():
            await update.message.reply_text("⏳ ဒီနေ့အတွက် Daily Reward ယူပြီးပါပြီ။ မနက်ဖြန်မှ ပြန်လာခဲ့ပါ။")
            return
            
        reward = random.randint(100, 500)
        await db.execute(
            "UPDATE users SET balance = balance + ?, last_daily = ? WHERE id = ?",
            (reward, now.isoformat(), user_id)
        )
        await db.commit()
        await update.message.reply_text(f"🎁 Daily Reward အဖြစ် **{reward} Coins** ရရှိပါတယ်!")

# --- Admin/Sudo Commands ---
async def add_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_sudo(update.effective_user.id): return
    
    chat_id = str(update.effective_chat.id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT value FROM settings WHERE key='drop_chats'") as cur:
            res = await cur.fetchone()
            current = res[0] if res else ""
        
        if chat_id not in current.split(","):
            new_chats = f"{current},{chat_id}" if current else chat_id
            await db.execute("UPDATE settings SET value = ? WHERE key = 'drop_chats'", (new_chats,))
            await db.commit()
            await update.message.reply_text("✅ ဒီ Group ကို Drop List ထဲ ထည့်လိုက်ပါပြီ။")
        else:
            await update.message.reply_text("ℹ️ ဒီ Group က List ထဲမှာ ရှိပြီးသားပါ။")

# --- Main Setup ---
async def post_init(app: Application):
    await init_db()
    asyncio.create_task(drop_loop(app))

def main():
    if not TOKEN:
        print("❌ Error: TELEGRAM_TOKEN not found in .env file.")
        return

    # Default settings to avoid Markdown errors
    defaults = Defaults(parse_mode=ParseMode.MARKDOWN)
    app = ApplicationBuilder().token(TOKEN).defaults(defaults).post_init(post_init).build()

    # Add Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catch", catch))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("addchat", add_chat))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
