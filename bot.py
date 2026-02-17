#!/usr/bin/env python3
# coding: utf-8
import os
import asyncio
import random
import aiosqlite
from datetime import datetime, timedelta
from functools import wraps
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ---------------- CONFIG ----------------
TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"
OWNER_ID = int(os.getenv("OWNER_ID") or 123456789)  # Owner telegram id

DATABASE = "bot.db"

# ---------------- HELPERS ----------------
def sudo_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = await aiosqlite.connect(DATABASE)
        async with db.execute("SELECT 1 FROM sudo_users WHERE user_id=?", (update.effective_user.id,)) as cursor:
            sudo = await cursor.fetchone()
        await db.close()
        if update.effective_user.id == OWNER_ID or sudo:
            return await func(update, context)
        else:
            await update.message.reply_text("❌ You are not a sudo user!")
    return wrapper

def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id == OWNER_ID:
            return await func(update, context)
        else:
            await update.message.reply_text("❌ Only owner can use this!")
    return wrapper

RARITY_LEVELS = [
    ("⚪ Common", 40),
    ("🟢 Uncommon", 25),
    ("🔵 Rare", 15),
    ("🟣 Epic", 8),
    ("🟠 Legendary", 5),
    ("🔴 Mythic", 3),
    ("🟡 Divine", 2),
    ("💎 Celestial", 1.5),
    ("👑 Supreme", 0.5),
    ("✨ Animated", 0.2),
]

# ---------------- DB INIT ----------------
async def init_db():
    db = await aiosqlite.connect(DATABASE)
    await db.execute("""
    CREATE TABLE IF NOT EXISTS cards(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        movie TEXT,
        rarity TEXT,
        media_type TEXT,
        file_id TEXT
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 1000
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS sudo_users(
        user_id INTEGER PRIMARY KEY
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS gban(
        user_id INTEGER PRIMARY KEY
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS gmute(
        user_id INTEGER PRIMARY KEY
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS drop_settings(
        id INTEGER PRIMARY KEY,
        interval INTEGER DEFAULT 60
    )""")
    await db.commit()
    await db.close()

# ---------------- SUDO COMMANDS ----------------
@sudo_only
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❌ Send an image to upload as a card!")
        return
    photo = update.message.photo[-1]
    rarity = random.choices([r[0] for r in RARITY_LEVELS], [r[1] for r in RARITY_LEVELS])[0]
    db = await aiosqlite.connect(DATABASE)
    await db.execute("INSERT INTO cards(name, movie, rarity, media_type, file_id) VALUES(?,?,?,?,?)",
                     (f"Card {random.randint(1000,9999)}", "Unknown", rarity, "image", photo.file_id))
    await db.commit()
    await db.close()
    await update.message.reply_text(f"✅ Card uploaded with rarity {rarity}!")

@sudo_only
async def uploadvd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("❌ Send a video to upload as an animated card!")
        return
    video = update.message.video
    rarity = "✨ Animated"
    db = await aiosqlite.connect(DATABASE)
    await db.execute("INSERT INTO cards(name, movie, rarity, media_type, file_id) VALUES(?,?,?,?,?)",
                     (f"Card {random.randint(1000,9999)}", "Unknown", rarity, "video", video.file_id))
    await db.commit()
    await db.close()
    await update.message.reply_text(f"✅ Video card uploaded!")

@sudo_only
async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /delete <id>")
        return
    card_id = int(context.args[0])
    db = await aiosqlite.connect(DATABASE)
    await db.execute("DELETE FROM cards WHERE id=?", (card_id,))
    await db.commit()
    await db.close()
    await update.message.reply_text(f"✅ Card {card_id} deleted!")

@sudo_only
async def setdrop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /setdrop <seconds>")
        return
    interval = int(context.args[0])
    db = await aiosqlite.connect(DATABASE)
    await db.execute("INSERT OR REPLACE INTO drop_settings(id, interval) VALUES(1,?)", (interval,))
    await db.commit()
    await db.close()
    await update.message.reply_text(f"✅ Drop interval set to {interval} seconds.")

# ---------------- OWNER COMMANDS ----------------
@owner_only
async def addsudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /addsudo <user_id>")
        return
    uid = int(context.args[0])
    db = await aiosqlite.connect(DATABASE)
    await db.execute("INSERT OR IGNORE INTO sudo_users(user_id) VALUES(?)", (uid,))
    await db.commit()
    await db.close()
    await update.message.reply_text(f"✅ Added sudo: {uid}")

# ---------------- USER COMMANDS ----------------
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db = await aiosqlite.connect(DATABASE)
    async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cursor:
        row = await cursor.fetchone()
    if not row:
        await db.execute("INSERT INTO users(user_id) VALUES(?)", (user_id,))
        await db.commit()
        balance_val = 1000
    else:
        balance_val = row[0]
    await db.close()
    await update.message.reply_text(f"💰 Your balance: {balance_val} coins")

# ---------------- DROP SYSTEM ----------------
async def drop_task(app):
    while True:
        db = await aiosqlite.connect(DATABASE)
        async with db.execute("SELECT interval FROM drop_settings WHERE id=1") as cursor:
            row = await cursor.fetchone()
        interval = row[0] if row else 60
        # Pick a random card
        async with db.execute("SELECT id, name, rarity, media_type, file_id FROM cards ORDER BY RANDOM() LIMIT 1") as cursor:
            card = await cursor.fetchone()
        await db.close()
        if card:
            chat_id = -1001234567890  # Replace with your group id
            if card[3] == "image":
                await app.bot.send_photo(chat_id, card[4], caption=f"🎴 Card Drop: {card[1]} | {card[2]}")
            else:
                await app.bot.send_video(chat_id, card[4], caption=f"🎴 Animated Drop: {card[1]} | {card[2]}")
        await asyncio.sleep(interval)

# ---------------- MAIN ----------------
async def main():
    await init_db()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()

    # Sudo commands
    app.add_handler(CommandHandler("upload", upload))
    app.add_handler(CommandHandler("uploadvd", uploadvd))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("setdrop", setdrop))

    # Owner commands
    app.add_handler(CommandHandler("addsudo", addsudo))

    # User commands
    app.add_handler(CommandHandler("balance", balance))

    # Start drop task
    app.create_task(drop_task(app))

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
