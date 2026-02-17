#!/usr/bin/env python3
# coding: utf-8

import os
import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
import subprocess
import sys

# ------------------ auto install missing packages ------------------
def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

try:
    import telegram
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
except ModuleNotFoundError:
    print("Required packages not found, installing...")
    install("python-telegram-bot==20.7")
    import telegram
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ----------------- simple env loader -----------------
def load_env(path=".env"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

load_env()

# ----------------- config -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else None
DB_FILE = os.getenv("DB_FILE", "dropbot.db")
DROP_CHECK_INTERVAL = int(os.getenv("DROP_CHECK_INTERVAL", "5"))

# ----------------- logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ----------------- Async DB wrapper -----------------
class AsyncDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()

    async def executescript(self, script: str):
        async with self._lock:
            await asyncio.to_thread(self.conn.executescript, script)

    async def execute(self, sql: str, params=()):
        async with self._lock:
            def _fn():
                cur = self.conn.execute(sql, params)
                return cur
            return await asyncio.to_thread(_fn)

    async def fetchone(self, sql: str, params=()):
        cur = await self.execute(sql, params)
        return cur.fetchone()

    async def fetchall(self, sql: str, params=()):
        cur = await self.execute(sql, params)
        return cur.fetchall()

    async def commit(self):
        async with self._lock:
            await asyncio.to_thread(self.conn.commit)

    async def close(self):
        await asyncio.to_thread(self.conn.close)

db = AsyncDB(DB_FILE)

# ----------------- Rarity -----------------
RARITY_NAME = {1:"common",2:"uncommon",3:"rare",4:"epic",5:"legendary",6:"mythic",
               7:"divine",8:"celestial",9:"supreme",10:"animated"}
RARITY_WEIGHT = {1:500,2:250,3:120,4:60,5:30,6:18,7:10,8:5,9:1,10:3}

# ----------------- helpers -----------------
def is_owner(user_id: int) -> bool:
    return OWNER_ID is not None and user_id == OWNER_ID

async def ensure_user(user_id: int):
    row = await db.fetchone("SELECT id FROM users WHERE id = ?", (user_id,))
    if not row:
        await db.execute("INSERT INTO users (id, coins) VALUES (?,?)", (user_id, 100))
        await db.commit()

def sudo_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **k):
        user_id = update.effective_user.id
        if is_owner(user_id):
            return await func(update, context, *a, **k)
        row = await db.fetchone("SELECT 1 FROM sudo WHERE user_id = ?", (user_id,))
        if row:
            return await func(update, context, *a, **k)
        await update.message.reply_text("Forbidden: sudo only command.")
    return wrapper

# ----------------- DB init -----------------
async def init_db():
    await db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        movie TEXT,
        file_id TEXT NOT NULL,
        file_type TEXT NOT NULL,
        rarity INTEGER NOT NULL,
        animated INTEGER DEFAULT 0,
        uploader INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        coins INTEGER DEFAULT 100,
        married_to INTEGER,
        daily_ts TEXT,
        fav_file_id TEXT
    );
    CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        card_id INTEGER NOT NULL,
        obtained_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sudo ( user_id INTEGER PRIMARY KEY );
    CREATE TABLE IF NOT EXISTS bans ( user_id INTEGER PRIMARY KEY );
    CREATE TABLE IF NOT EXISTS mutes ( user_id INTEGER PRIMARY KEY );
    CREATE TABLE IF NOT EXISTS drops (
        chat_id INTEGER PRIMARY KEY,
        interval_seconds INTEGER DEFAULT 0,
        next_drop_ts TEXT
    );
    CREATE TABLE IF NOT EXISTS drops_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        card_id INTEGER,
        message_id INTEGER,
        drop_ts TEXT,
        claimed_by INTEGER
    );
    """)
    log.info("DB initialized")

# ----------------- card ops -----------------
async def add_card(name,movie,file_id,file_type,rarity,animated,uploader):
    await db.execute("INSERT INTO cards (name,movie,file_id,file_type,rarity,animated,uploader,created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (name,movie,file_id,file_type,rarity,animated,uploader,datetime.utcnow().isoformat()))
    await db.commit()

async def pick_random_card():
    rows = await db.fetchall("SELECT * FROM cards")
    if not rows:
        return None
    weighted = [(r,RARITY_WEIGHT.get(r['rarity'],1)) for r in rows]
    choices = [r for r,w in weighted for _ in range(max(1,w))]
    return random.choice(choices)

async def award_card_to_user(user_id:int, card_id:int):
    await db.execute("INSERT INTO inventory (user_id,card_id,obtained_at) VALUES (?,?,?)", (user_id, card_id, datetime.utcnow().isoformat()))
    await db.commit()

async def get_user_coins(user_id:int) -> int:
    await ensure_user(user_id)
    row = await db.fetchone("SELECT coins FROM users WHERE id = ?", (user_id,))
    return int(row['coins'])

async def add_coins(user_id:int, amount:int):
    await ensure_user(user_id)
    row = await db.fetchone("SELECT coins FROM users WHERE id = ?", (user_id,))
    new = int(row['coins']) + amount
    await db.execute("UPDATE users SET coins = ? WHERE id = ?", (new, user_id))
    await db.commit()
    return new

# ----------------- command handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Drop Card Bot ready. Use /help")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Commands: /upload /uploadvd /edit /delete /setdrop /Catch /balance /daily /slots /wheel /shop /buy /top /addsudo /sudolist /broadcast")

# ----------------- more commands omitted for brevity -----------------
# Use your previous full command code here (upload, uploadvd, edit, delete, setdrop, catch, buy, slots, wheel, etc.)
# All DB operations and handlers remain the same

# ----------------- drop scheduler -----------------
async def drop_scheduler(app):
    log.info('Drop scheduler started')
    while True:
        try:
            rows = await db.fetchall('SELECT chat_id, interval_seconds, next_drop_ts FROM drops')
            now = datetime.utcnow()
            for r in rows:
                chat_id = r['chat_id']
                interval = int(r['interval_seconds'])
                next_ts = datetime.fromisoformat(r['next_drop_ts']) if r['next_drop_ts'] else now
                if now >= next_ts:
                    card = await pick_random_card()
                    if not card:
                        continue
                    caption = f"Drop — {card['name']} ({RARITY_NAME.get(card['rarity'],'?')})\nTap Catch to claim!"
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton('Catch', callback_data='claim:temp')]])
                    try:
                        if card['file_type'] == 'photo':
                            sent = await app.bot.send_photo(chat_id=chat_id, photo=card['file_id'], caption=caption, reply_markup=kb)
                        else:
                            sent = await app.bot.send_video(chat_id=chat_id, video=card['file_id'], caption=caption, reply_markup=kb)
                        # insert drop record
                        await db.execute('INSERT INTO drops_history (chat_id, card_id, message_id, drop_ts, claimed_by) VALUES (?,?,?,?,NULL)',
                                         (chat_id, card['id'], sent.message_id, datetime.utcnow().isoformat()))
                        await db.commit()
                        # get last insert id
                        hid = db.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                        # update button
                        await app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=sent.message_id,
                                                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Catch', callback_data=f'claim:{hid}')]]))
                        nnext = (now + timedelta(seconds=interval)).isoformat()
                        await db.execute('UPDATE drops SET next_drop_ts = ? WHERE chat_id = ?', (nnext, chat_id))
                        await db.commit()
                    except Exception as e:
                        log.exception('drop send failed %s', e)
            await asyncio.sleep(DROP_CHECK_INTERVAL)
        except Exception as e:
            log.exception('scheduler error %s', e)
            await asyncio.sleep(5)

# ----------------- startup -----------------
async def main():
    if not BOT_TOKEN:
        log.error('BOT_TOKEN missing in env or .env')
        return
    await init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Add your command handlers here as shown in your code
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    # ... add all other handlers

    # start scheduler
    app.create_task(drop_scheduler(app))

    await app.run_polling()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info('Bot stopped')
