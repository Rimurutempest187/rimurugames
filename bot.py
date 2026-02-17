#!/usr/bin/env python3
# coding: utf-8
"""
Telegram "drop card" game bot (single-file).
Usage:
  - Set TELEGRAM_TOKEN environment variable
  - Optionally set OWNER_ID environment variable (int). Owner has highest privileges.
  - Run: python bot.py
Notes:
  - SQLite DB: cards.db (created automatically)
  - Media: uses Telegram file_id (no local download)
  - This is a compact but complete implementation of requested commands.
"""

import os
import asyncio
import random
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- Config (env) ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Please set TELEGRAM_TOKEN env variable.")

OWNER_ID = int(os.getenv("OWNER_ID") or 0)  # optional
DB_FILE = os.getenv("DB_FILE") or "cards.db"
DROP_INTERVAL_SECONDS = 300  # default drop interval; settable via /setdrop
GROUP_IDS_TO_DROP = []  # If you want automatic drops to specific group ids, fill here (or use broadcast/drop manually)

# --- Rarity labels (0..9) ---
RARITY_LABELS = [
    "Common", "Uncommon", "Rare", "Epic", "Legendary",
    "Mythic", "Divine", "Celestial", "Supreme", "Animated"
]
RARITY_EMOJIS = ["⚪","🟢","🔵","🟣","🟠","🔴","🟡","💎","👑","✨"]

# --- DB helpers ---
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
            file_type TEXT NOT NULL, -- photo/video
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
def is_owner(user_id:int) -> bool:
    return (OWNER_ID and user_id == OWNER_ID)

async def is_sudo(conn, user_id:int) -> bool:
    if is_owner(user_id): return True
    row = await db_get(conn, "SELECT 1 FROM sudo_users WHERE id = ?", (user_id,))
    return bool(row)

def rarity_text(r:int) -> str:
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

async def change_balance(conn, user_id:int, amount:int, note:str=""):
    await conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
    await conn.execute("INSERT INTO transactions(user_id, amount, note, at) VALUES (?,?,?,?)", (user_id, amount, note, datetime.utcnow().isoformat()))
    await conn.commit()

# --- Command decorators / checks ---
async def require_sudo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[aiosqlite.Connection]:
    user = update.effective_user
    conn = await aiosqlite.connect(DB_FILE)
    if not await is_sudo(conn, user.id):
        await conn.close()
        await update.message.reply_text("⛔ You are not authorized to run this command (sudo only).")
        return None
    return conn

async def require_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[aiosqlite.Connection]:
    user = update.effective_user
    if is_owner(user.id):
        conn = await aiosqlite.connect(DB_FILE)
        return conn
    await update.message.reply_text("⛔ Owner-only command.")
    return None

# --- Core commands ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    await ensure_user(conn, update.effective_user)
    await conn.close()
    await update.message.reply_text("Welcome to CardDrop Bot! Use /help to see commands.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🃏 *Card Drop Bot commands*\n\n"
        "*Sudo commands*\n"
        "/upload - reply to an image to upload card (caption: name|movie|rarity_index)\n"
        "/uploadvd - reply to a video to upload animated card\n"
        "/edit <id> <name> <movie> - edit card\n        /delete <id>\n"
        "/setdrop <seconds> - set automatic drop interval\n        /gban <id/username/reply>\n        /ungban <id/username/reply>\n        /gmute <id/username/reply>\n        /ungmute <id/username/reply>\n\n"
        "*User commands*\n"
        "/balance /shop /buy /daily\n"
        "/slots /wheel /basket\n"
        "/givecoin /deposit /topcoin\n"
        "/missions /titles\n"
        "/fusion /duel /trade (use reply)\n"
        "/marry /divorce (use reply)\n"
        "/set /removeset\n"
        "/Catch <card_name> (reply to drop message or use name)\n        /top - top collectors\n\n"
        "Owner: /addsudo /sudolist /broadcast\n"
    )
    await update.message.reply_markdown(txt)

# --- Upload image (sudo) ---
async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Expect reply to message containing a photo or document image.
    user = update.effective_user
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        if not update.message.reply_to_message:
            await update.message.reply_text("Reply to an image (photo/document) with /upload. Optionally include caption: name|movie|rarity_index")
            return
        media = update.message.reply_to_message
        file_id = None
        file_type = None
        # accept photo
        if media.photo:
            file_id = media.photo[-1].file_id
            file_type = "photo"
        elif media.document and media.document.mime_type and media.document.mime_type.startswith("image"):
            file_id = media.document.file_id
            file_type = "photo"
        else:
            await update.message.reply_text("Reply to a photo or image document.")
            return
        caption = update.message.text.strip() if update.message.text else (media.caption or "")
        name = None
        movie = ""
        rarity = 0
        if caption:
            parts = [p.strip() for p in caption.replace("/upload","").split("|") if p.strip()]
            if len(parts) >= 1:
                name = parts[0]
            if len(parts) >= 2:
                movie = parts[1]
            if len(parts) >= 3:
                try:
                    rr = int(parts[2])
                    if 0 <= rr <= 9: rarity = rr
                except:
                    pass
        if not name:
            # fallback to asking short name from command args
            if context.args:
                name = " ".join(context.args)
            else:
                await update.message.reply_text("Please provide a name. Use caption or /upload <name> as text when replying.")
                return
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "INSERT INTO cards(name, movie, file_id, file_type, rarity, animated, creator, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, movie, file_id, file_type, rarity, 0, user.id, now)
        )
        await conn.commit()
        row = await db_get(conn, "SELECT last_insert_rowid()")
        await update.message.reply_text(f"✅ Card uploaded: *{name}* ({rarity_text(rarity)})", parse_mode="Markdown")
    finally:
        await conn.close()

async def uploadvd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # reply to a video message
    user = update.effective_user
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        if not update.message.reply_to_message:
            await update.message.reply_text("Reply to a video with /uploadvd. Optionally include caption: name|movie|rarity_index (rarity optional).")
            return
        media = update.message.reply_to_message
        file_id = None
        file_type = None
        if media.video:
            file_id = media.video.file_id
            file_type = "video"
        elif media.document and media.document.mime_type and media.document.mime_type.startswith("video"):
            file_id = media.document.file_id
            file_type = "video"
        else:
            await update.message.reply_text("Reply to a video or video document.")
            return
        caption = update.message.text.strip() if update.message.text else (media.caption or "")
        name = None
        movie = ""
        rarity = 9  # Animated slot by default
        if caption:
            parts = [p.strip() for p in caption.replace("/uploadvd","").split("|") if p.strip()]
            if len(parts) >= 1:
                name = parts[0]
            if len(parts) >= 2:
                movie = parts[1]
            if len(parts) >= 3:
                try:
                    rr = int(parts[2])
                    if 0 <= rr <= 9: rarity = rr
                except:
                    pass
        if not name:
            if context.args:
                name = " ".join(context.args)
            else:
                await update.message.reply_text("Please provide a name. Use caption or /uploadvd <name> as text when replying.")
                return
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "INSERT INTO cards(name, movie, file_id, file_type, rarity, animated, creator, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, movie, file_id, file_type, rarity, 1, user.id, now)
        )
        await conn.commit()
        await update.message.reply_text(f"✅ Video Card uploaded: *{name}* ({rarity_text(rarity)})", parse_mode="Markdown")
    finally:
        await conn.close()

# --- Edit/Delete ---
async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text("Usage: /edit <id> <name> <movie>")
            return
        cid = int(args[0])
        name = args[1]
        movie = " ".join(args[2:])
        await conn.execute("UPDATE cards SET name=?, movie=? WHERE id=?", (name, movie, cid))
        await conn.commit()
        await update.message.reply_text(f"✅ Card {cid} updated.")
    finally:
        await conn.close()

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        if not context.args:
            await update.message.reply_text("Usage: /delete <id>")
            return
        cid = int(context.args[0])
        await conn.execute("DELETE FROM cards WHERE id=?", (cid,))
        await conn.commit()
        await update.message.reply_text(f"✅ Card {cid} deleted.")
    finally:
        await conn.close()

# --- Sudo: ban/mute ---
async def _resolve_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accept id, @username or reply
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user.id
    if context.args:
        t = context.args[0]
        if t.startswith("@"):
            # cannot resolve username to id reliably without bot.get_chat, so try to fetch user via get_chat
            try:
                chat = await context.bot.get_chat(t)
                return chat.id
            except:
                try:
                    return int(t.strip("@"))
                except:
                    return None
        else:
            try:
                return int(t)
            except:
                return None
    return None

async def gban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        tid = await _resolve_target_id(update, context)
        if not tid:
            await update.message.reply_text("Could not resolve target id. Use reply or id or @username.")
            return
        await conn.execute("INSERT OR REPLACE INTO bans(id, reason) VALUES (?,?)", (tid, "gban"))
        await conn.commit()
        await update.message.reply_text(f"⛔ Globally banned: {tid}")
    finally:
        await conn.close()

async def ungban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        tid = await _resolve_target_id(update, context)
        if not tid:
            await update.message.reply_text("Could not resolve target id.")
            return
        await conn.execute("DELETE FROM bans WHERE id=?", (tid,))
        await conn.commit()
        await update.message.reply_text(f"✅ Ungbanned: {tid}")
    finally:
        await conn.close()

async def gmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        tid = await _resolve_target_id(update, context)
        if not tid:
            await update.message.reply_text("Could not resolve target id.")
            return
        await conn.execute("INSERT OR REPLACE INTO mutes(id) VALUES (?)", (tid,))
        await conn.commit()
        await update.message.reply_text(f"🔇 Globally muted: {tid}")
    finally:
        await conn.close()

async def ungmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn:
        await update.message.reply_text("Could not resolve target id.")
        return
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        tid = await _resolve_target_id(update, context)
        if not tid:
            await update.message.reply_text("Could not resolve target id.")
            return
        await conn.execute("DELETE FROM mutes WHERE id=?", (tid,))
        await conn.commit()
        await update.message.reply_text(f"✅ Unmuted: {tid}")
    finally:
        await conn.close()

# --- Drop system ---
DROP_LOCK = asyncio.Lock()

async def perform_drop(bot, chat_id:int, conn: aiosqlite.Connection):
    async with DROP_LOCK:
        # pick a random card
        row = await db_get(conn, "SELECT id, name, file_id, file_type, rarity, animated FROM cards ORDER BY RANDOM() LIMIT 1")
        if not row:
            return None
        cid, name, file_id, file_type, rarity, animated = row
        text = f"🎴 *Drop!* A card appeared: *{name}* — {rarity_text(rarity)}\nReply with /Catch or use `/Catch {name}` to catch!"
        if file_type == "photo":
            msg = await bot.send_photo(chat_id=chat_id, photo=file_id, caption=text, parse_mode="Markdown")
        else:
            msg = await bot.send_video(chat_id=chat_id, video=file_id, caption=text, parse_mode="Markdown")
        await conn.execute(
            "INSERT INTO drops(card_id, chat_id, message_id, dropped_at) VALUES (?,?,?,?)",
            (cid, chat_id, msg.message_id, datetime.utcnow().isoformat())
        )
        await conn.commit()
        return (cid, msg.message_id)

# background drop loop
async def drop_loop(app):
    # Use settings table for interval
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as conn:
                row = await db_get(conn, "SELECT value FROM settings WHERE key = ?", ("drop_interval",))
                if row:
                    try:
                        interval = int(row[0])
                    except:
                        interval = DROP_INTERVAL_SECONDS
                else:
                    interval = DROP_INTERVAL_SECONDS
                # decide where to drop: if specific group ids configured, use them; else skip
                targets = GROUP_IDS_TO_DROP.copy()
                # also use chats stored in settings under 'drop_chats' as comma-separated
                row2 = await db_get(conn, "SELECT value FROM settings WHERE key = ?", ("drop_chats",))
                if row2 and row2[0]:
                    try:
                        extra = [int(x) for x in row2[0].split(",") if x.strip()]
                        for e in extra:
                            if e not in targets:
                                targets.append(e)
                    except:
                        pass
                if targets:
                    for chat_id in targets:
                        await perform_drop(app.bot, chat_id, conn)
                # sleep interval
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("Drop loop error:", e)
            await asyncio.sleep(10)

# /setdrop
async def setdrop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_sudo(update, context)
    if not conn:
        return
    try:
        if not context.args:
            await update.message.reply_text("Usage: /setdrop <seconds>")
            return
        sec = int(context.args[0])
        await conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", ("drop_interval", str(sec)))
        await conn.commit()
        await update.message.reply_text(f"✅ Drop interval set to {sec} seconds.")
    finally:
        await conn.close()

# --- Catch command ---
CATCH_LOCK = asyncio.Lock()

async def catch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # user attempts to catch either by replying to drop or by /Catch <name>
    user = update.effective_user
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await ensure_user(conn, user)
        target_drop = None
        # if reply to a message that is a drop, find it
        if update.message.reply_to_message:
            rid = update.message.reply_to_message.message_id
            row = await db_get(conn, "SELECT id, card_id, caught_by FROM drops WHERE chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1", (update.effective_chat.id, rid))
            if row:
                target_drop = row
        if not target_drop:
            # find most recent uncaught drop in this chat with that name
            name = " ".join(context.args) if context.args else None
            if name:
                row = await db_get(conn,
                    "SELECT d.id, d.card_id, d.caught_by FROM drops d JOIN cards c ON c.id=d.card_id WHERE d.chat_id=? AND d.caught_by=0 AND lower(c.name)=? ORDER BY d.dropped_at DESC LIMIT 1",
                    (update.effective_chat.id, name.lower()))
                if row:
                    target_drop = row
            else:
                # take last uncaught drop
                row = await db_get(conn, "SELECT id, card_id, caught_by FROM drops WHERE chat_id=? AND caught_by=0 ORDER BY dropped_at DESC LIMIT 1", (update.effective_chat.id,))
                if row:
                    target_drop = row
        if not target_drop:
            await update.message.reply_text("No available drop to catch here.")
            return
        drop_id, card_id, caught_by = target_drop
        # atomic claim
        async with CATCH_LOCK:
            row2 = await db_get(conn, "SELECT caught_by FROM drops WHERE id=?", (drop_id,))
            if row2 and row2[0] and row2[0] != 0:
                await update.message.reply_text("Too late — someone already caught it.")
                return
            # chance formula: higher rarity harder. We'll allow immediate catch first come first served with small randomness.
            card = await db_get(conn, "SELECT name, rarity FROM cards WHERE id=?", (card_id,))
            if not card:
                await update.message.reply_text("Card not found.")
                return
            name, rarity = card
            # small probability fail: e.g., success chance = max(0.2, 1 - rarity*0.08)
            success_chance = max(0.2, 1.0 - rarity * 0.075)
            if random.random() <= success_chance:
                # success
                await conn.execute("UPDATE drops SET caught_by=? WHERE id=?", (user.id, drop_id))
                await conn.commit()
                # reward: give some coins depending on rarity
                reward = 10 + (9 - rarity) * 5 + rarity * 10
                await change_balance(conn, user.id, reward, note=f"caught card {name}")
                await update.message.reply_text(f"🎉 You caught *{name}*! Reward: {reward} coins.", parse_mode="Markdown")
            else:
                await update.message.reply_text("😢 Your attempt failed — someone else might still catch it.")
    finally:
        await conn.close()

# --- Economy & shop ---
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await ensure_user(conn, update.effective_user)
        row = await db_get(conn, "SELECT balance FROM users WHERE id=?", (update.effective_user.id,))
        bal = row[0] if row else 0
        await update.message.reply_text(f"💰 Balance: {bal} coins")
    finally:
        await conn.close()

async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await ensure_user(conn, update.effective_user)
        row = await db_get(conn, "SELECT last_daily, balance FROM users WHERE id=?", (update.effective_user.id,))
        last_daily = row[0] if row else None
        if last_daily:
            dt = datetime.fromisoformat(last_daily)
            if datetime.utcnow() - dt < timedelta(hours=20):  # 20h cooldown
                await update.message.reply_text("Daily already claimed. Try later.")
                return
        # reward
        reward = random.randint(50, 150)
        await conn.execute("UPDATE users SET balance=balance+?, last_daily=? WHERE id=?", (reward, datetime.utcnow().isoformat(), update.effective_user.id))
        await conn.commit()
        await update.message.reply_text(f"✅ Daily claimed: {reward} coins")
    finally:
        await conn.close()

# --- Simple gambling games ---
async def slots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await ensure_user(conn, update.effective_user)
        if not context.args:
            await update.message.reply_text("Usage: /slots <bet>")
            return
        bet = int(context.args[0])
        row = await db_get(conn, "SELECT balance FROM users WHERE id=?", (update.effective_user.id,))
        bal = row[0]
        if bet <= 0 or bet > bal:
            await update.message.reply_text("Invalid bet.")
            return
        # simple slot: 3 reels of symbols 0..4
        reels = [random.randint(0,4) for _ in range(3)]
        symbols = ["🍒","🍋","🔔","⭐","💎"]
        display = " ".join(symbols[r] for r in reels)
        if reels[0] == reels[1] == reels[2]:
            # big win: 5x
            win = bet * 5
            await change_balance(conn, update.effective_user.id, win, note="slots win")
            result = f"JACKPOT! {display}\nYou won {win} coins!"
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            win = int(bet * 1.5)
            await change_balance(conn, update.effective_user.id, win, note="slots small win")
            result = f"{display}\nYou won {win} coins!"
        else:
            # lose
            await change_balance(conn, update.effective_user.id, -bet, note="slots lose")
            result = f"{display}\nYou lost {bet} coins."
        await update.message.reply_text(result)
    finally:
        await conn.close()

async def wheel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await ensure_user(conn, update.effective_user)
        if not context.args:
            await update.message.reply_text("Usage: /wheel <bet>")
            return
        bet = int(context.args[0])
        row = await db_get(conn, "SELECT balance FROM users WHERE id=?", (update.effective_user.id,))
        bal = row[0]
        if bet <= 0 or bet > bal:
            await update.message.reply_text("Invalid bet.")
            return
        # wheel with multipliers
        multipliers = [0, 0.5, 1, 2, 5, 10]
        mult = random.choice(multipliers)
        if mult == 0:
            await change_balance(conn, update.effective_user.id, -bet, note="wheel lose")
            await update.message.reply_text(f"Wheel: {mult}x — you lost {bet} coins.")
        else:
            gain = int(bet * mult)
            await change_balance(conn, update.effective_user.id, gain, note="wheel win")
            await update.message.reply_text(f"Wheel: {mult}x — you won {gain} coins!")
    finally:
        await conn.close()

# --- Trade / duel / marry simplified placeholders ---
async def duel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user with /duel to challenge them.")
        return
    challenger = update.effective_user
    target = update.message.reply_to_message.from_user
    if target.is_bot:
        await update.message.reply_text("Cannot duel bots.")
        return
    # simple duel: random winner
    winner = random.choice([challenger, target])
    await update.message.reply_text(f"⚔️ Duel result: {winner.full_name} wins!")

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Trade feature: reply to user with /trade <card_id> to propose trade (not fully implemented in this minimal version).")

async def marry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to user with /marry to marry them.")
        return
    a = update.effective_user.id
    b = update.message.reply_to_message.from_user.id
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await ensure_user(conn, update.effective_user)
        await ensure_user(conn, update.message.reply_to_message.from_user)
        await conn.execute("INSERT INTO marriages(user1, user2, at) VALUES (?,?,?)", (a,b,datetime.utcnow().isoformat()))
        await conn.commit()
        await update.message.reply_text("💍 Marriage recorded.")
    finally:
        await conn.close()

async def divorce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await conn.execute("DELETE FROM marriages WHERE user1=? OR user2=?", (user,user))
        await conn.commit()
        await update.message.reply_text("⚖️ Divorce processed (if any).")
    finally:
        await conn.close()

# --- Owner / sudo management ---
async def addsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner only
    conn = await require_owner(update, context)
    if not conn:
        return
    try:
        if update.message.reply_to_message:
            tid = update.message.reply_to_message.from_user.id
        elif context.args:
            tid = int(context.args[0])
        else:
            await update.message.reply_text("Usage: /addsudo <id> or reply.")
            return
        await conn.execute("INSERT OR REPLACE INTO sudo_users(id) VALUES (?)", (tid,))
        await conn.commit()
        await update.message.reply_text(f"✅ Added sudo user: {tid}")
    finally:
        await conn.close()

async def sudolist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_owner(update, context)
    if not conn:
        return
    try:
        rows = await db_all(conn, "SELECT id FROM sudo_users")
        if not rows:
            await update.message.reply_text("No sudo users.")
            return
        lst = "\n".join(str(r[0]) for r in rows)
        await update.message.reply_text(f"Sudo users:\n{lst}")
    finally:
        await conn.close()

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await require_owner(update, context)
    if not conn:
        return
    try:
        if not context.args and not update.message.reply_to_message:
            await update.message.reply_text("Usage: /broadcast <text> or reply to a message to broadcast.")
            return
        text = " ".join(context.args) if context.args else update.message.reply_to_message.text or ""
        # For demo: broadcast to group IDs kept in settings->drop_chats
        row = await db_get(conn, "SELECT value FROM settings WHERE key=?", ("drop_chats",))
        if not row or not row[0]:
            await update.message.reply_text("No drop_chats configured in settings (no targets). Use /setdropchats or set settings directly.")
            return
        targets = [int(x) for x in row[0].split(",") if x.strip()]
        sent = 0
        for chat_id in targets:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                sent += 1
            except Exception as e:
                print("broadcast to",chat_id,"failed:",e)
        await update.message.reply_text(f"Broadcast sent to {sent} chats.")
    finally:
        await conn.close()

# --- Misc utilities: top collectors ---
async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = await aiosqlite.connect(DB_FILE)
    try:
        rows = await db_all(conn, """
            SELECT u.id, u.username, COUNT(c.id) as cnt
            FROM users u
            LEFT JOIN drops d ON d.caught_by = u.id
            LEFT JOIN cards c ON c.id = d.card_id
            GROUP BY u.id
            ORDER BY cnt DESC
            LIMIT 10
        """)
        txt = "🏆 Top collectors:\n"
        for r in rows:
            uid, uname, cnt = r
            txt += f"{uname or uid}: {cnt}\n"
        await update.message.reply_text(txt)
    finally:
        await conn.close()

# --- Startup / main ---
async def on_startup(app):
    await init_db()
    # create owner as sudo if OWNER_ID set
    if OWNER_ID:
        async with aiosqlite.connect(DB_FILE) as conn:
            await conn.execute("INSERT OR REPLACE INTO sudo_users(id) VALUES (?)", (OWNER_ID,))
            await conn.commit()
    # start drop loop background task
    app.job_drop_loop = app.create_task(drop_loop(app))
    print("Bot started and DB initialized.")

async def on_shutdown(app):
    # cancel drop task
    task = getattr(app, "job_drop_loop", None)
    if task:
        task.cancel()
        try:
            await task
        except:
            pass
    print("Shutting down...")

def main():
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()

    # Basic commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("uploadvd", uploadvd_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("setdrop", setdrop_cmd))
    app.add_handler(CommandHandler("gban", gban_cmd))
    app.add_handler(CommandHandler("ungban", ungban_cmd))
    app.add_handler(CommandHandler("gmute", gmute_cmd))
    app.add_handler(CommandHandler("ungmute", ungmute_cmd))

    # user economy/games
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler("slots", slots_cmd))
    app.add_handler(CommandHandler("wheel", wheel_cmd))
    app.add_handler(CommandHandler("Catch", catch_cmd))
    app.add_handler(CommandHandler("catch", catch_cmd))  # lowercase alias

    # social/trade
    app.add_handler(CommandHandler("duel", duel_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("marry", marry_cmd))
    app.add_handler(CommandHandler("divorce", divorce_cmd))

    # owner / sudo
    app.add_handler(CommandHandler("addsudo", addsudo_cmd))
    app.add_handler(CommandHandler("sudolist", sudolist_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("top", top_cmd))

    app.post_init = on_startup
    app.pre_shutdown = on_shutdown

    print("Running bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
