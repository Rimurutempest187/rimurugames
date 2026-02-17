import os
import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------- simple env loader (no python-dotenv) -----------------

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

# ----------------- Async DB wrapper over sqlite3 -----------------
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
            cur = await asyncio.to_thread(_fn)
            return cur

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

# single DB instance
db = AsyncDB(DB_FILE)

# ----------------- Rarity system -----------------
RARITY_NAME = {
    1: "common",
    2: "uncommon",
    3: "rare",
    4: "epic",
    5: "legendary",
    6: "mythic",
    7: "divine",
    8: "celestial",
    9: "supreme",
    10: "animated",
}
RARITY_WEIGHT = {1:500,2:250,3:120,4:60,5:30,6:18,7:10,8:5,9:1,10:3}

# ----------------- helpers -----------------

def is_owner(user_id: int) -> bool:
    return OWNER_ID is not None and user_id == OWNER_ID

async def ensure_user(user_id: int):
    row = await db.fetchone("SELECT id FROM users WHERE id = ?", (user_id,))
    if not row:
        await db.execute("INSERT INTO users (id, coins) VALUES (?,?)", (user_id, 100))
        await db.commit()

# decorator for sudo (owner or sudo table)
from functools import wraps

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
    await db.executescript(r"""
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

# ----------------- core card ops -----------------
async def add_card(name, movie, file_id, file_type, rarity, animated, uploader):
    await db.execute("INSERT INTO cards (name,movie,file_id,file_type,rarity,animated,uploader,created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (name,movie,file_id,file_type,rarity,animated,uploader,datetime.utcnow().isoformat()))
    await db.commit()

async def pick_random_card():
    rows = await db.fetchall("SELECT * FROM cards")
    if not rows:
        return None
    weighted = []
    for r in rows:
        w = RARITY_WEIGHT.get(r['rarity'],1)
        weighted.append((r,w))
    choices = [r for r,w in weighted for _ in range(max(1,w//1))]
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

@sudo_only
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    file_id = None
    file_type = None
    if msg.reply_to_message and msg.reply_to_message.photo:
        file_id = msg.reply_to_message.photo[-1].file_id
        file_type = 'photo'
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = 'photo'
    elif msg.reply_to_message and msg.reply_to_message.document and msg.reply_to_message.document.mime_type.startswith('image'):
        file_id = msg.reply_to_message.document.file_id
        file_type = 'photo'
    else:
        await msg.reply_text('Please reply to an image or send one with the command')
        return
    args = context.args
    name = args[0] if args else f"card_{random.randint(1000,9999)}"
    movie = args[1] if len(args)>1 else ''
    rarity = int(args[2]) if len(args)>2 and args[2].isdigit() else 1
    await add_card(name,movie,file_id,file_type,rarity,0,user.id)
    await msg.reply_text(f"Added card '{name}' (rarity {RARITY_NAME.get(rarity,'?')})")

@sudo_only
async def uploadvd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    file_id = None
    if msg.reply_to_message and msg.reply_to_message.video:
        file_id = msg.reply_to_message.video.file_id
        file_type='video'
    elif msg.video:
        file_id = msg.video.file_id
        file_type='video'
    else:
        await msg.reply_text('Please reply to a video or send one with the command')
        return
    args = context.args
    name = args[0] if args else f"anim_{random.randint(1000,9999)}"
    movie = args[1] if len(args)>1 else ''
    rarity = int(args[2]) if len(args)>2 and args[2].isdigit() else 10
    await add_card(name,movie,file_id,file_type,rarity,1,user.id)
    await msg.reply_text(f"Added animated card '{name}'")

@sudo_only
async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text('Usage: /edit <id> <name> <movie>')
        return
    cid = int(context.args[0])
    name = context.args[1]
    movie = context.args[2]
    await db.execute('UPDATE cards SET name = ?, movie = ? WHERE id = ?', (name, movie, cid))
    await db.commit()
    await update.message.reply_text('Card updated')

@sudo_only
async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('Usage: /delete <id>')
        return
    cid = int(context.args[0])
    await db.execute('DELETE FROM cards WHERE id = ?', (cid,))
    await db.commit()
    await update.message.reply_text('Card deleted')

@sudo_only
async def setdrop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        await update.message.reply_text('/setdrop must be used in a group chat')
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text('Usage: /setdrop <seconds> (0 to disable)')
        return
    interval = int(context.args[0])
    chat_id = update.effective_chat.id
    if interval <= 0:
        await db.execute('DELETE FROM drops WHERE chat_id = ?', (chat_id,))
        await db.commit()
        await update.message.reply_text('Drops disabled for this chat')
        return
    next_ts = (datetime.utcnow() + timedelta(seconds=interval)).isoformat()
    await db.execute('INSERT OR REPLACE INTO drops (chat_id, interval_seconds, next_drop_ts) VALUES (?,?,?)', (chat_id, interval, next_ts))
    await db.commit()
    await update.message.reply_text(f'Drops set every {interval} seconds')

@sudo_only
async def gban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('Usage: /gban <user_id>')
        return
    uid = int(context.args[0])
    await db.execute('INSERT OR IGNORE INTO bans (user_id) VALUES (?)', (uid,))
    await db.commit()
    await update.message.reply_text('Globally banned')

@sudo_only
async def ungban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('Usage: /ungban <user_id>')
        return
    uid = int(context.args[0])
    await db.execute('DELETE FROM bans WHERE user_id = ?', (uid,))
    await db.commit()
    await update.message.reply_text('Globally unbanned')

@sudo_only
async def gmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('Usage: /gmute <user_id>')
        return
    uid = int(context.args[0])
    await db.execute('INSERT OR IGNORE INTO mutes (user_id) VALUES (?)', (uid,))
    await db.commit()
    await update.message.reply_text('Globally muted')

@sudo_only
async def ungmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('Usage: /ungmute <user_id>')
        return
    uid = int(context.args[0])
    await db.execute('DELETE FROM mutes WHERE user_id = ?', (uid,))
    await db.commit()
    await update.message.reply_text('Globally unmuted')

# owner
async def addsudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text('Owner only')
        return
    if not context.args:
        await update.message.reply_text('Usage: /addsudo <user_id>')
        return
    uid = int(context.args[0])
    await db.execute('INSERT OR IGNORE INTO sudo (user_id) VALUES (?)', (uid,))
    await db.commit()
    await update.message.reply_text('Added sudo')

async def sudolist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text('Owner only')
        return
    rows = await db.fetchall('SELECT user_id FROM sudo')
    await update.message.reply_text('Sudo users:\n' + '\n'.join(str(r['user_id']) for r in rows) if rows else '<none>')

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text('Owner only')
        return
    text = ' '.join(context.args) if context.args else (update.message.reply_to_message.text if update.message.reply_to_message else '')
    if not text:
        await update.message.reply_text('Usage: /broadcast <text>')
        return
    rows = await db.fetchall('SELECT chat_id FROM drops')
    sent = 0
    for r in rows:
        try:
            await context.bot.send_message(r['chat_id'], text)
            sent += 1
        except Exception as e:
            log.warning('broadcast fail %s', e)
    await update.message.reply_text(f'Sent to {sent} chats')

# user commands
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    coins = await get_user_coins(uid)
    await update.message.reply_text(f'Balance: {coins} coins')

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    row = await db.fetchone('SELECT daily_ts FROM users WHERE id = ?', (uid,))
    now = datetime.utcnow()
    if row and row['daily_ts']:
        last = datetime.fromisoformat(row['daily_ts'])
        if now - last < timedelta(hours=24):
            await update.message.reply_text('Daily already claimed')
            return
    await add_coins(uid, 100)
    await db.execute('UPDATE users SET daily_ts = ? WHERE id = ?', (now.isoformat(), uid))
    await db.commit()
    await update.message.reply_text('Claimed 100 coins (daily)')

async def slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bet = int(context.args[0]) if context.args and context.args[0].isdigit() else 10
    coins = await get_user_coins(uid)
    if bet <= 0 or bet > coins:
        await update.message.reply_text('Invalid bet')
        return
    await add_coins(uid, -bet)
    symbols = ['🍒','🔔','💎','7️⃣']
    res = [random.choice(symbols) for _ in range(3)]
    if len(set(res)) == 1:
        win = bet*5
        await add_coins(uid, win)
        await update.message.reply_text(f"{''.join(res)} — JACKPOT! +{win}")
    elif len(set(res)) == 2:
        win = bet*2
        await add_coins(uid, win)
        await update.message.reply_text(f"{''.join(res)} — Win +{win}")
    else:
        await update.message.reply_text(f"{''.join(res)} — Lost {bet}")

async def wheel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bet = int(context.args[0]) if context.args and context.args[0].isdigit() else 10
    coins = await get_user_coins(uid)
    if bet <= 0 or bet > coins:
        await update.message.reply_text('Invalid bet')
        return
    await add_coins(uid, -bet)
    sectors = [0,0,10,20,-bet,bet*2,50]
    res = random.choice(sectors)
    if res > 0:
        await add_coins(uid, res)
        await update.message.reply_text(f'Wheel +{res} — you win')
    elif res == 0:
        await update.message.reply_text('Wheel 0 — no change')
    else:
        await update.message.reply_text(f'Penalty {abs(res)}')

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Shop: pack_common 50, pack_rare 200 — /buy <item>')

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text('Usage: /buy <item>')
        return
    item = context.args[0]
    price_map = {'pack_common':50, 'pack_rare':200}
    if item not in price_map:
        await update.message.reply_text('Unknown item')
        return
    price = price_map[item]
    coins = await get_user_coins(uid)
    if coins < price:
        await update.message.reply_text('Not enough coins')
        return
    await add_coins(uid, -price)
    if item == 'pack_common':
        row = await db.fetchone('SELECT * FROM cards WHERE rarity <= 2 ORDER BY RANDOM() LIMIT 1')
    else:
        row = await db.fetchone('SELECT * FROM cards WHERE rarity >= 3 ORDER BY RANDOM() LIMIT 1')
    if not row:
        await add_coins(uid, price)
        await update.message.reply_text('No card available — refunded')
        return
    await award_card_to_user(uid, row['id'])
    await update.message.reply_text(f'You received: {row["name"]}')

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db.fetchall('SELECT user_id, COUNT(*) as c FROM inventory GROUP BY user_id ORDER BY c DESC LIMIT 10')
    txt = 'Top collectors:\n' + '\n'.join(f"{i+1}. {r['user_id']} — {r['c']}" for i,r in enumerate(rows))
    await update.message.reply_text(txt)

# catch via reply or callback
async def catch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.reply_to_message:
        chat_id = update.effective_chat.id
        orig_mid = update.message.reply_to_message.message_id
        row = await db.fetchone('SELECT * FROM drops_history WHERE chat_id = ? AND message_id = ?', (chat_id, orig_mid))
        if not row:
            await update.message.reply_text('Not a drop')
            return
        if row['claimed_by']:
            await update.message.reply_text('Already claimed')
            return
        await db.execute('UPDATE drops_history SET claimed_by = ? WHERE id = ?', (uid, row['id']))
        await award_card_to_user(uid, row['card_id'])
        await db.commit()
        await update.message.reply_text('You caught the card!')
        return
    await update.message.reply_text('Reply to the drop message or use the Catch button')

# callback handler for inline "Catch" button
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith('claim:'):
        return
    hid = int(data.split(':',1)[1])
    row = await db.fetchone('SELECT * FROM drops_history WHERE id = ?', (hid,))
    if not row:
        await query.edit_message_caption((query.message.caption or '') + "\n\n[Drop expired or not found]")
        return
    if row['claimed_by']:
        await query.answer('Already claimed')
        return
    uid = query.from_user.id
    # check ban
    b = await db.fetchone('SELECT 1 FROM bans WHERE user_id = ?', (uid,))
    if b:
        await query.answer('You are banned')
        return
    await db.execute('UPDATE drops_history SET claimed_by = ? WHERE id = ?', (uid, hid))
    await award_card_to_user(uid, row['card_id'])
    await db.commit()
    try:
        await query.edit_message_reply_markup(None)
    except:
        pass
    await query.message.reply_text(f"{query.from_user.first_name} caught the card!")

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
                    # send media
                    try:
                        if card['file_type'] == 'photo':
                            sent = await app.bot.send_photo(chat_id=chat_id, photo=card['file_id'], caption=caption, reply_markup=kb)
                        else:
                            sent = await app.bot.send_video(chat_id=chat_id, video=card['file_id'], caption=caption, reply_markup=kb)
                        # record drop
                        res = await db.execute('INSERT INTO drops_history (chat_id, card_id, message_id, drop_ts, claimed_by) VALUES (?,?,?,?,NULL)', (chat_id, card['id'], sent.message_id, datetime.utcnow().isoformat()))
                        await db.commit()
                        # retrieve last inserted id
                        hid = db.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                        # update button callback to real id
                        try:
                            await app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=sent.message_id,
                                                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Catch', callback_data=f'claim:{hid}')]]))
                        except Exception:
                            pass
                        # set next
                        nnext = (now + timedelta(seconds=interval)).isoformat()
                        await db.execute('UPDATE drops SET next_drop_ts = ? WHERE chat_id = ?', (nnext, chat_id))
                        await db.commit()
                    except Exception as e:
                        log.exception('drop send failed %s', e)
            await asyncio.sleep(DROP_CHECK_INTERVAL)
        except Exception as e:
            log.exception('scheduler error %s', e)
            await asyncio.sleep(5)

# ----------------- message handler (ignore banned) -----------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    b = await db.fetchone('SELECT 1 FROM bans WHERE user_id = ?', (uid,))
    if b:
        return

# ----------------- startup -----------------
async def main():
    if not BOT_TOKEN:
        log.error('BOT_TOKEN missing in env or .env')
        return
    await init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))

    app.add_handler(CommandHandler('upload', upload))
    app.add_handler(CommandHandler('uploadvd', uploadvd))
    app.add_handler(CommandHandler('edit', edit))
    app.add_handler(CommandHandler('delete', delete))
    app.add_handler(CommandHandler('setdrop', setdrop))
    app.add_handler(CommandHandler('gban', gban))
    app.add_handler(CommandHandler('ungban', ungban))
    app.add_handler(CommandHandler('gmute', gmute))
    app.add_handler(CommandHandler('ungmute', ungmute))

    app.add_handler(CommandHandler('addsudo', addsudo))
    app.add_handler(CommandHandler('sudolist', sudolist))
    app.add_handler(CommandHandler('broadcast', broadcast))

    app.add_handler(CommandHandler('balance', balance))
    app.add_handler(CommandHandler('daily', daily))
    app.add_handler(CommandHandler('slots', slots))
    app.add_handler(CommandHandler('wheel', wheel))
    app.add_handler(CommandHandler('shop', shop))
    app.add_handler(CommandHandler('buy', buy))
    app.add_handler(CommandHandler('top', top))
    app.add_handler(CommandHandler('Catch', catch_cmd))

    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # start scheduler
    app.create_task(drop_scheduler(app))

    await app.run_polling()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info('Bot stopped')
