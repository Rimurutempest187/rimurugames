#!/usr/bin/env python3
# coding: utf-8
"""
Minimal Telegram bot using only Python stdlib.
Features:
 - Uses getUpdates long-polling (no external libs)
 - SQLite (stdlib) for persistence
 - Background text-only drops
 - Commands: /start /help /balance /daily /slots /Catch /setdrop (owner)
Set TELEGRAM_TOKEN env var before running.
Optional: set OWNER_ID env var (int).
"""

import os
import sys
import time
import json
import random
import sqlite3
import threading
from datetime import datetime, timedelta
from urllib import request, parse, error

# --- Config ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    print("Please set TELEGRAM_TOKEN environment variable.")
    sys.exit(1)

OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_FILE = os.getenv("DB_FILE") or "cards_min.db"
DROP_INTERVAL_SECONDS = int(os.getenv("DROP_INTERVAL_SECONDS") or 300)

API_URL = f"https://api.telegram.org/bot{TOKEN}/"

# --- HTTP helpers (stdlib) ---
def api_call(method, params=None, files=None):
    url = API_URL + method
    if files:
        # not used in this minimal version
        raise NotImplementedError("File upload not implemented in stdlib version.")
    data = None
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if params:
        data = parse.urlencode(params).encode()
    req = request.Request(url, data=data, headers=headers)
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def send_message(chat_id, text, parse_mode=None):
    params = {"chat_id": chat_id, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    return api_call("sendMessage", params)

# --- DB init ---
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER DEFAULT 0,
        last_daily TEXT
    );
    CREATE TABLE IF NOT EXISTS drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        chat_id INTEGER,
        dropped_at TEXT,
        caught_by INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()
    return conn

DB = init_db()
DB_LOCK = threading.Lock()

# --- Utilities ---
def ensure_user_row(user):
    if not user:
        return
    uid = user.get("id")
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("SELECT id FROM users WHERE id=?", (uid,))
        if not cur.fetchone():
            cur.execute("INSERT INTO users(id, username, balance, last_daily) VALUES (?,?,?,?)",
                        (uid, user.get("username") or "", 100, None))
            DB.commit()

def change_balance(user_id, amount):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
        DB.commit()

def get_balance(user_id):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("SELECT balance FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else 0

def set_setting(key, value):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, str(value)))
        DB.commit()

def get_setting(key):
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

# --- Drops (text-only) ---
def perform_drop(chat_id):
    # choose a random name from a small pool
    pool = ["Red Dragon","Blue Phoenix","Silver Knight","Golden Fox","Shadow Wolf"]
    name = random.choice(pool)
    now = datetime.utcnow().isoformat()
    with DB_LOCK:
        cur = DB.cursor()
        cur.execute("INSERT INTO drops(name, chat_id, dropped_at) VALUES (?,?,?)", (name, chat_id, now))
        DB.commit()
    text = f"🎴 Drop! A card appeared: *{name}*\nReply with /Catch or use `/Catch {name}` to catch!"
    send_message(chat_id, text, parse_mode="Markdown")

def drop_loop():
    while True:
        try:
            interval = int(get_setting("drop_interval") or DROP_INTERVAL_SECONDS)
        except Exception:
            interval = DROP_INTERVAL_SECONDS
        # target chats: read from settings 'drop_chats' as comma-separated ids
        raw = get_setting("drop_chats")
        targets = []
        if raw:
            try:
                targets = [int(x) for x in raw.split(",") if x.strip()]
            except:
                targets = []
        # if no targets, skip
        if targets:
            for cid in targets:
                try:
                    perform_drop(cid)
                except Exception as e:
                    print("perform_drop error:", e)
        time.sleep(interval)

# --- Update processing (long-polling) ---
OFFSET = None

def handle_update(u):
    global OFFSET
    OFFSET = max(OFFSET or 0, u["update_id"] + 1)
    msg = u.get("message") or u.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    user = msg.get("from")
    text = msg.get("text") or ""
    ensure_user_row(user)

    # simple command parsing
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lstrip("/").split("@")[0]
    args = parts[1:]

    # Owner check
    is_owner = (OWNER_ID and user and user.get("id") == OWNER_ID)

    if cmd.lower() == "start":
        send_message(chat_id, "Welcome to Minimal CardDrop Bot! Use /help to see commands.")
    elif cmd.lower() == "help":
        send_message(chat_id,
            "/start /help\n/balance - show coins\n/daily - claim daily\n/slots <bet> - play slots\n/Catch [name] - catch last drop or by name\n\nOwner: /setdrop <seconds> ; /adddropchat <chat_id>")
    elif cmd.lower() == "balance":
        bal = get_balance(user.get("id"))
        send_message(chat_id, f"💰 Balance: {bal} coins")
    elif cmd.lower() == "daily":
        with DB_LOCK:
            cur = DB.cursor()
            cur.execute("SELECT last_daily FROM users WHERE id=?", (user.get("id"),))
            row = cur.fetchone()
            last = row[0] if row else None
            if last:
                try:
                    dt = datetime.fromisoformat(last)
                    if datetime.utcnow() - dt < timedelta(hours=20):
                        send_message(chat_id, "Daily already claimed. Try later.")
                        return
                except:
                    pass
            reward = random.randint(50, 150)
            cur.execute("UPDATE users SET balance=balance+?, last_daily=? WHERE id=?", (reward, datetime.utcnow().isoformat(), user.get("id")))
            DB.commit()
        send_message(chat_id, f"✅ Daily claimed: {reward} coins")
    elif cmd.lower() == "slots":
        if not args:
            send_message(chat_id, "Usage: /slots <bet>")
            return
        try:
            bet = int(args[0])
        except:
            send_message(chat_id, "Invalid bet.")
            return
        bal = get_balance(user.get("id"))
        if bet <= 0 or bet > bal:
            send_message(chat_id, "Invalid bet or insufficient balance.")
            return
        reels = [random.randint(0,4) for _ in range(3)]
        symbols = ["🍒","🍋","🔔","⭐","💎"]
        display = " ".join(symbols[r] for r in reels)
        if reels[0] == reels[1] == reels[2]:
            win = bet * 5
            change_balance(user.get("id"), win)
            send_message(chat_id, f"JACKPOT! {display}\nYou won {win} coins!")
        elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
            win = int(bet * 1.5)
            change_balance(user.get("id"), win)
            send_message(chat_id, f"{display}\nYou won {win} coins!")
        else:
            change_balance(user.get("id"), -bet)
            send_message(chat_id, f"{display}\nYou lost {bet} coins.")
    elif cmd.lower() == "catch":
        # try reply message id or name
        name = " ".join(args).strip() if args else None
        with DB_LOCK:
            cur = DB.cursor()
            if name:
                cur.execute("SELECT id, name, caught_by FROM drops WHERE chat_id=? AND caught_by=0 AND lower(name)=? ORDER BY dropped_at DESC LIMIT 1", (chat_id, name.lower()))
            else:
                cur.execute("SELECT id, name, caught_by FROM drops WHERE chat_id=? AND caught_by=0 ORDER BY dropped_at DESC LIMIT 1", (chat_id,))
            row = cur.fetchone()
            if not row:
                send_message(chat_id, "No available drop to catch here.")
                return
            drop_id, cname, caught_by = row
            # simple success chance
            rarity_factor = 0  # minimal version: all same rarity
            success_chance = 0.8
            if random.random() <= success_chance:
                cur.execute("UPDATE drops SET caught_by=? WHERE id=?", (user.get("id"), drop_id))
                DB.commit()
                reward = random.randint(10, 50)
                change_balance(user.get("id"), reward)
                send_message(chat_id, f"🎉 You caught *{cname}*! Reward: {reward} coins.", parse_mode="Markdown")
            else:
                send_message(chat_id, "😢 Your attempt failed — someone else might still catch it.")
    elif cmd.lower() == "setdrop":
        if not is_owner:
            send_message(chat_id, "⛔ Owner-only command.")
            return
        if not args:
            send_message(chat_id, "Usage: /setdrop <seconds>")
            return
        try:
            sec = int(args[0])
            set_setting("drop_interval", sec)
            send_message(chat_id, f"✅ Drop interval set to {sec} seconds.")
        except:
            send_message(chat_id, "Invalid number.")
    elif cmd.lower() == "adddropchat":
        if not is_owner:
            send_message(chat_id, "⛔ Owner-only command.")
            return
        if not args:
            send_message(chat_id, "Usage: /adddropchat <chat_id>")
            return
        try:
            cid = int(args[0])
            raw = get_setting("drop_chats") or ""
            lst = [x for x in raw.split(",") if x.strip()]
            if str(cid) not in lst:
                lst.append(str(cid))
            set_setting("drop_chats", ",".join(lst))
            send_message(chat_id, f"✅ Added drop chat: {cid}")
        except:
            send_message(chat_id, "Invalid chat id.")
    else:
        # ignore other commands or send help
        if text.startswith("/"):
            send_message(chat_id, "Unknown command. Use /help to see available commands.")

def poll_loop():
    global OFFSET
    OFFSET = None
    while True:
        params = {}
        if OFFSET:
            params["offset"] = OFFSET
        params["timeout"] = 30
        resp = api_call("getUpdates", params)
        if not resp.get("ok"):
            print("getUpdates error:", resp)
            time.sleep(5)
            continue
        for u in resp.get("result", []):
            try:
                handle_update(u)
            except Exception as e:
                print("handle_update error:", e)
        # small sleep to avoid tight loop
        time.sleep(0.5)

# --- Start threads ---
if __name__ == "__main__":
    # start drop loop thread
    t = threading.Thread(target=drop_loop, daemon=True)
    t.start()
    print("Bot started (polling). Press Ctrl+C to stop.")
    try:
        poll_loop()
    except KeyboardInterrupt:
        print("Stopping bot...")
        sys.exit(0)
