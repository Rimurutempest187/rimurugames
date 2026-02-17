#!/usr/bin/env python3
# coding: utf-8
"""
Compact Card Drop Bot — Fixed & Runnable.
Set TELEGRAM_TOKEN env var before running or create a .env file.
"""

import os
import sys
import asyncio
import random
import importlib
import subprocess
from datetime import datetime, timedelta
from typing import Optional

# --- Bootstrap: Auto-install missing packages ---
REQUIRED = [
    ("aiosqlite", "aiosqlite"),
    ("dotenv", "python-dotenv"),
    ("telegram", "python-telegram-bot"),
]

def ensure_packages():
    """Ensures required packages are installed before importing."""
    failed = []
    for mod, pkg in REQUIRED:
        try:
            importlib.import_module(mod)
        except ImportError:
            print(f"📦 Installing missing package: {pkg}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
                importlib.invalidate_caches()
                importlib.import_module(mod)
                print(f"✅ {pkg} installed.")
            except Exception as e:
                print(f"❌ Failed to install {pkg}: {e}")
                failed.append(pkg)
    
    if failed:
        print("Please install manually: pip install " + " ".join(failed))
        sys.exit(1)

ensure_packages()
# -----------------------------------------------

# --- Imports (Safe after bootstrap) ---
import aiosqlite
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# Load .env file immediately
load_dotenv()

# --- Config ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_FILE = os.getenv("DB_FILE") or "cards.db"
DROP_INTERVAL_SECONDS = 300 
GROUP_IDS_TO_DROP = [] 

if not TOKEN:
    print("❌ Error: TELEGRAM_TOKEN not found in environment variables or .env file.")
    sys.exit(1)

# --- Rarity Config ---
RARITY_LABELS = [
    "Common", "Uncommon", "Rare", "Epic", "Legendary",
    "Mythic", "Divine", "Celestial", "Supreme", "Animated"
]
RARITY_EMOJIS = ["⚪","🟢","🔵","🟣","🟠","🔴","🟡","💎","👑","✨"]

# --- Database Setup ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript("""
        PRAGMA foreign_keys = ON;
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
        """)
        await db.commit()
        print("✅ Database initialized.")

async def db_get(conn, query, params=()):
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return row

async def db_all(conn, query, params=()):
    cur = await conn.execute(query, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows

# --- Utilities ---
def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID

async def is_sudo(conn, user_id: int) -> bool:
    if is_owner(user_id): return True
    row = await db_get(conn, "SELECT 1 FROM sudo_users WHERE id = ?", (user_id,))
    return bool(row)

def rarity_text(r: int) -> str:
    if 0 <= r < len(RARITY_LABELS):
        return f"{RARITY_EMOJIS[r]} {RARITY_LABELS[r]}"
    return f"{r}"

async def ensure_user(conn, user):
    if user is None: return
    uid = user.id
    row = await db_get(conn, "SELECT id FROM users WHERE id = ?", (uid,))
    if not row:
        await conn.execute("INSERT INTO users(id, username, balance) VALUES (?,?,?)", (uid, user.username or "", 100))
        await conn.commit()

async def change_balance(conn, user_id: int, amount: int, note: str = ""):
    await conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
    await conn.execute("INSERT INTO transactions(user_id, amount, note, at) VALUES (?,?,?,?)", 
                       (user_id, amount, note, datetime.utcnow().isoformat()))
    await conn.commit()

# --- Decorators / Checks ---
async def require_sudo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[aiosqlite.Connection]:
    user = update.effective_user
    conn = await aiosqlite.connect(DB_FILE)
    if not await is_sudo(conn, user.id):
        await conn.close()
        await update.message.reply_text("⛔ You are not authorized (Sudo Only).")
        return None
    return conn

async def require_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[aiosqlite.Connection]:
    user = update.effective_user
    if is_owner(user.id):
        conn = await aiosqlite.connect(DB_FILE)
        return conn
    await update.message.reply_text("⛔ Owner-only command.")
    return None

# --- Core Commands ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    await ensure_user(conn, update.effective_user)
    await conn.close()
    await update.message.reply_text("🃏 Welcome to CardDrop Bot! Use /help to see commands.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🃏 *Card Drop Bot commands*\n\n"
        "*Sudo commands*\n"
        "/upload - Reply to image (caption: name|movie|rarity_0-9)\n"
        "/uploadvd - Reply to video (caption: name|movie|rarity_0-9)\n"
        "/edit <id> <name> <movie>\n/delete <id>\n"
        "/setdrop <seconds> - Set drop interval\n"
        "/gban, /ungban, /gmute, /ungmute <id/reply>\n\n"
        "*User commands*\n"
        "/balance, /daily\n"
        "/slots <bet>, /wheel <bet>\n"
        "/catch <name> (or reply)\n"
        "Owner: /addsudo, /broadcast"
    )
    await update.message.reply_markdown(txt)

# --- Uploads ---
async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn: return
    try:
        if not update.message.reply_to_message:
            await update.message.reply_text("Reply to an image with /upload. Caption: name|movie|rarity")
            return
        
        media = update.message.reply_to_message
        file_id, file_type = None, None
        
        if media.photo:
            file_id = media.photo[-1].file_id
            file_type = "photo"
        elif media.document and media.document.mime_type and media.document.mime_type.startswith("image"):
            file_id = media.document.file_id
            file_type = "photo"
        else:
            await update.message.reply_text("Not an image.")
            return

        # Parsing caption logic
        caption = update.message.text.strip() if update.message.text else (media.caption or "")
        name, movie, rarity = None, "", 0
        
        clean_cap = caption.replace("/upload", "").strip()
        if clean_cap:
            parts = [p.strip() for p in clean_cap.split("|") if p.strip()]
            if len(parts) >= 1: name = parts[0]
            if len(parts) >= 2: movie = parts[1]
            if len(parts) >= 3 and parts[2].isdigit(): rarity = int(parts[2])

        if not name and context.args:
            name = " ".join(context.args)
        
        if not name:
            await update.message.reply_text("Please provide a name in caption (name|movie|rarity) or command args.")
            return

        await conn.execute(
            "INSERT INTO cards(name, movie, file_id, file_type, rarity, animated, creator, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, movie, file_id, file_type, rarity, 0, update.effective_user.id, datetime.utcnow().isoformat())
        )
        await conn.commit()
        await update.message.reply_text(f"✅ Card uploaded: *{name}* ({rarity_text(rarity)})", parse_mode="Markdown")
    finally:
        await conn.close()

async def uploadvd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn: return
    try:
        if not update.message.reply_to_message:
            await update.message.reply_text("Reply to a video with /uploadvd.")
            return
        
        media = update.message.reply_to_message
        file_id, file_type = None, None

        if media.video:
            file_id = media.video.file_id
            file_type = "video"
        elif media.document and media.document.mime_type and media.document.mime_type.startswith("video"):
            file_id = media.document.file_id
            file_type = "video"
        else:
            await update.message.reply_text("Not a video.")
            return

        caption = update.message.text.strip() if update.message.text else (media.caption or "")
        name, movie, rarity = None, "", 9 # Default animated rarity
        
        clean_cap = caption.replace("/uploadvd", "").strip()
        if clean_cap:
            parts = [p.strip() for p in clean_cap.split("|") if p.strip()]
            if len(parts) >= 1: name = parts[0]
            if len(parts) >= 2: movie = parts[1]
            if len(parts) >= 3 and parts[2].isdigit(): rarity = int(parts[2])

        if not name and context.args: name = " ".join(context.args)

        if not name:
            await update.message.reply_text("Provide a name.")
            return

        await conn.execute(
            "INSERT INTO cards(name, movie, file_id, file_type, rarity, animated, creator, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, movie, file_id, file_type, rarity, 1, update.effective_user.id, datetime.utcnow().isoformat())
        )
        await conn.commit()
        await update.message.reply_text(f"✅ Video Card uploaded: *{name}*", parse_mode="Markdown")
    finally:
        await conn.close()

# --- Admin Utils ---
async def setdrop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn: return
    try:
        if not context.args:
            await update.message.reply_text(f"Usage: /setdrop <seconds>. Default: {DROP_INTERVAL_SECONDS}")
            return
        sec = int(context.args[0])
        await conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", ("drop_interval", str(sec)))
        await conn.commit()
        await update.message.reply_text(f"✅ Drop interval set to {sec} seconds.")
    except ValueError:
        await update.message.reply_text("Invalid number.")
    finally:
        await conn.close()

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_owner(update, context)
    if not conn: return
    try:
        if not context.args:
            await update.message.reply_text("Usage: /broadcast <message>")
            return
        msg = " ".join(context.args)
        rows = await db_all(conn, "SELECT id FROM users")
        count = 0
        for r in rows:
            uid = r[0]
            try:
                await context.bot.send_message(chat_id=uid, text=msg)
                count += 1
                await asyncio.sleep(0.05) # Rate limit protection
            except Exception:
                pass
        await update.message.reply_text(f"Broadcast sent to {count} users.")
    finally:
        await conn.close()

async def addsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_owner(update, context)
    if not conn: return
    try:
        tid = None
        if update.message.reply_to_message:
            tid = update.message.reply_to_message.from_user.id
        elif context.args:
            try: tid = int(context.args[0])
            except: pass
        
        if not tid:
            await update.message.reply_text("Invalid ID or reply.")
            return
            
        await conn.execute("INSERT OR REPLACE INTO sudo_users(id) VALUES (?)", (tid,))
        await conn.commit()
        await update.message.reply_text(f"✅ Added sudo: {tid}")
    finally:
        await conn.close()

# --- Drop Logic ---
DROP_LOCK = asyncio.Lock()

async def perform_drop(bot, chat_id: int, conn: aiosqlite.Connection):
    async with DROP_LOCK:
        row = await db_get(conn, "SELECT id, name, file_id, file_type, rarity FROM cards ORDER BY RANDOM() LIMIT 1")
        if not row: return
        
        cid, name, file_id, file_type, rarity = row
        text = f"🎴 *Drop!* A card appeared: *{name}* — {rarity_text(rarity)}\nReply with /catch or use `/catch {name}`!"
        
        try:
            if file_type == "photo":
                msg = await bot.send_photo(chat_id=chat_id, photo=file_id, caption=text, parse_mode="Markdown")
            else:
                msg = await bot.send_video(chat_id=chat_id, video=file_id, caption=text, parse_mode="Markdown")
            
            await conn.execute(
                "INSERT INTO drops(card_id, chat_id, message_id, dropped_at) VALUES (?,?,?,?)",
                (cid, chat_id, msg.message_id, datetime.utcnow().isoformat())
            )
            await conn.commit()
        except Exception as e:
            print(f"Failed to drop in {chat_id}: {e}")

async def drop_loop(app: Application):
    """Background task to handle drops."""
    print("⏳ Drop loop started...")
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as conn:
                # 1. Get Interval
                row = await db_get(conn, "SELECT value FROM settings WHERE key = ?", ("drop_interval",))
                interval = int(row[0]) if row else DROP_INTERVAL_SECONDS
                
                # 2. Get Targets (In a real bot, you'd store enabled chats in DB)
                # For this compact version, we use the global list + logic to add chats
                targets = GROUP_IDS_TO_DROP.copy()
                
                # 3. Perform Drops
                for chat_id in targets:
                    await perform_drop(app.bot, chat_id, conn)
            
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Drop loop error: {e}")
            await asyncio.sleep(60)

# --- Catch ---
CATCH_LOCK = asyncio.Lock()

async def catch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await ensure_user(conn, user)
        target_drop = None
        
        # 1. Try Catch by Reply
        if update.message.reply_to_message:
            rid = update.message.reply_to_message.message_id
            target_drop = await db_get(conn, "SELECT id, card_id, caught_by FROM drops WHERE chat_id=? AND message_id=? LIMIT 1", (chat_id, rid))
        
        # 2. Try Catch by Name (if args provided)
        if not target_drop and context.args:
            name_query = " ".join(context.args).lower()
            target_drop = await db_get(conn, 
                "SELECT d.id, d.card_id, d.caught_by FROM drops d JOIN cards c ON c.id=d.card_id WHERE d.chat_id=? AND d.caught_by=0 AND lower(c.name)=? ORDER BY d.dropped_at DESC LIMIT 1", 
                (chat_id, name_query))

        # 3. Try Catch Most Recent (Lazy catch)
        if not target_drop and not context.args:
             target_drop = await db_get(conn, "SELECT id, card_id, caught_by FROM drops WHERE chat_id=? AND caught_by=0 ORDER BY dropped_at DESC LIMIT 1", (chat_id,))

        if not target_drop:
            await update.message.reply_text("No drop found to catch.")
            return

        drop_id, card_id, caught_by = target_drop

        async with CATCH_LOCK:
            # Re-check concurrency
            check = await db_get(conn, "SELECT caught_by FROM drops WHERE id=?", (drop_id,))
            if check and check[0] != 0:
                await update.message.reply_text("🏃 Too late! Someone already caught it.")
                return

            # Success Logic
            card = await db_get(conn, "SELECT name, rarity FROM cards WHERE id=?", (card_id,))
            name, rarity = card
            
            # Simple Math: Higher rarity = Harder catch
            success_chance = max(0.3, 1.0 - (rarity * 0.08)) 
            if random.random() <= success_chance:
                await conn.execute("UPDATE drops SET caught_by=? WHERE id=?", (user.id, drop_id))
                reward = 10 + (rarity * 15)
                await change_balance(conn, user.id, reward, f"Caught {name}")
                await update.message.reply_text(f"🎉 *{user.first_name}* caught *{name}*!\nEarned {reward} coins.", parse_mode="Markdown")
            else:
                await update.message.reply_text("💨 It slipped through your fingers! (Try again)")
                
    finally:
        await conn.close()

# --- Economy ---
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    await ensure_user(conn, update.effective_user)
    row = await db_get(conn, "SELECT balance FROM users WHERE id=?", (update.effective_user.id,))
    await conn.close()
    bal = row[0] if row else 0
    await update.message.reply_text(f"💰 Balance: {bal} coins")

async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    await ensure_user(conn, update.effective_user)
    row = await db_get(conn, "SELECT last_daily FROM users WHERE id=?", (update.effective_user.id,))
    
    if row and row[0]:
        last = datetime.fromisoformat(row[0])
        if datetime.utcnow() - last < timedelta(hours=20):
            await update.message.reply_text("⏳ Daily already claimed today.")
            await conn.close()
            return

    amt = random.randint(50, 200)
    await conn.execute("UPDATE users SET balance=balance+?, last_daily=? WHERE id=?", (amt, datetime.utcnow().isoformat(), update.effective_user.id))
    await conn.commit()
    await conn.close()
    await update.message.reply_text(f"✅ Daily claimed: +{amt} coins.")

async def slots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Usage: /slots <bet>")
    try: bet = int(context.args[0])
    except: return await update.message.reply_text("Invalid bet.")
    
    conn = await aiosqlite.connect(DB_FILE)
    await ensure_user(conn, update.effective_user)
    row = await db_get(conn, "SELECT balance FROM users WHERE id=?", (update.effective_user.id,))
    if row[0] < bet or bet <= 0:
        await conn.close()
        return await update.message.reply_text("Insufficient funds.")

    reels = [random.choice(["🍒","🍋","🔔","⭐","💎"]) for _ in range(3)]
    display = " | ".join(reels)
    
    win_amt = 0
    note = "slots lose"
    
    if reels[0] == reels[1] == reels[2]:
        win_amt = bet * 5
        note = "slots jackpot"
        msg = f"🎰 {display}\nJACKPOT! Won {win_amt}!"
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        win_amt = int(bet * 1.5)
        note = "slots win"
        msg = f"🎰 {display}\nNice! Won {win_amt}!"
    else:
        win_amt = -bet
        msg = f"🎰 {display}\nLost {bet}."

    await change_balance(conn, update.effective_user.id, win_amt, note)
    await conn.close()
    await update.message.reply_text(msg)

# --- LifeCycle ---
async def post_init(application: Application):
    """Executes on bot startup."""
    await init_db()
    # Start the drop loop as a background task
    asyncio.create_task(drop_loop(application))
    print("🤖 Bot is online!")

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    
    # Sudo
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("uploadvd", uploadvd_cmd))
    app.add_handler(CommandHandler("setdrop", setdrop_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("addsudo", addsudo_cmd))

    # User
    app.add_handler(CommandHandler("catch", catch_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler("slots", slots_cmd))

    print("🚀 Starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
