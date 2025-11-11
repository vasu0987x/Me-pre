import telebot
from telebot import types
import json
import os
import random
from flask import Flask, Response
import threading
import requests
import re
import html
import phonenumbers
import pycountry
import time
import sqlite3
from queue import Queue, PriorityQueue
from datetime import datetime, timedelta
from collections import deque
import hashlib
import itertools

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN") 
ADMIN_ID = 6483088050
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", num_threads=4)

DATA_FILE = "bot_data.json"
NUMBERS_DIR = "numbers"
DB_FILE = "bot_database.db"
os.makedirs(NUMBERS_DIR, exist_ok=True)

# API Config
API_TOKEN = os.getenv("API_TOKEN") 
BASE_URL = "http://147.135.212.197/crapi/s1t"
OTP_GROUP_ID = "-1002784314709"
BACKUP = "https://t.me/TricksMastarNumbar"
CHANNEL_LINK = "https://t.me/TRICKSMASTEROTP2_bot"

# Referral Config
REFERRAL_REWARD = 0.02
MIN_WITHDRAWAL = 0.50
REFERRAL_SYSTEM_ENABLED = True  # Admin can toggle this

# ==================== OPTIMIZED QUEUES ====================
# Priority queue for faster processing (lower number = higher priority)
group_queue = PriorityQueue(maxsize=2000)
personal_queue = PriorityQueue(maxsize=10000)
seen_messages = deque(maxlen=100000)  # Increased cache

# Multiple sender threads for parallel processing
NUM_GROUP_SENDERS = 2
NUM_PERSONAL_SENDERS = 2

# Counter for queue ordering (prevents comparison errors)
queue_counter = itertools.count()

# ==================== REGEX PATTERNS ====================
KEYWORD_REGEX = re.compile(r"(otp|code|pin|password|verify)[^\d]{0,10}(\d[\d\-]{3,8})", re.I)
REVERSE_REGEX = re.compile(r"(\d[\d\-]{3,8})[^\w]{0,10}(otp|code|pin|password|verify)", re.I)
GENERIC_REGEX = re.compile(r"\d{2,4}[-]?\d{2,4}")
UNICODE_CLEAN = re.compile(r"[\u200f\u200e\u202a-\u202e]")

# ==================== DATABASE SETUP ====================
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)

    c = conn.cursor()
    
    # User numbers mapping
    c.execute('''CREATE TABLE IF NOT EXISTS user_numbers
                 (number TEXT PRIMARY KEY, chat_id INTEGER, country TEXT, assigned_at REAL)''')
    
    # User stats
    c.execute('''CREATE TABLE IF NOT EXISTS user_stats
                 (chat_id INTEGER PRIMARY KEY, total_otps INTEGER DEFAULT 0, 
                  last_otp REAL, joined_at REAL)''')
    
    # Message cache
    c.execute('''CREATE TABLE IF NOT EXISTS message_cache
                 (msg_id TEXT PRIMARY KEY, created_at REAL)''')
    
    # Past OTPs cache
    c.execute('''CREATE TABLE IF NOT EXISTS past_otps_cache
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  number TEXT,
                  sender TEXT,
                  message TEXT,
                  otp TEXT,
                  timestamp TEXT,
                  received_at REAL)''')
    
    # Referral system tables
    c.execute('''CREATE TABLE IF NOT EXISTS user_referrals
                 (chat_id INTEGER PRIMARY KEY,
                  ref_code TEXT UNIQUE,
                  referred_by INTEGER,
                  balance REAL DEFAULT 0.0,
                  total_referrals INTEGER DEFAULT 0,
                  total_earned REAL DEFAULT 0.0,
                  wallet_address TEXT,
                  created_at REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS referral_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  referrer_id INTEGER,
                  referred_id INTEGER,
                  reward REAL,
                  created_at REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawal_requests
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER,
                  amount REAL,
                  wallet_address TEXT,
                  status TEXT DEFAULT 'pending',
                  request_time REAL,
                  processed_time REAL,
                  processed_by INTEGER,
                  txn_id TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS balance_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER,
                  amount REAL,
                  type TEXT,
                  description TEXT,
                  created_at REAL)''')
    
    # System settings table
    c.execute('''CREATE TABLE IF NOT EXISTS system_settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Initialize referral system setting
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('referral_enabled', 'true')")
    
    # Create indexes
    c.execute('''CREATE INDEX IF NOT EXISTS idx_number ON past_otps_cache(number)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_received_at ON past_otps_cache(received_at)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_referrer ON referral_history(referrer_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_referred ON referral_history(referred_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_withdrawal_status ON withdrawal_requests(status)''')
    
    conn.commit()
    conn.close()

init_db()

# ==================== SYSTEM SETTINGS ====================
def is_referral_enabled():
    """Check if referral system is enabled"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("SELECT value FROM system_settings WHERE key='referral_enabled'")
    result = c.fetchone()
    conn.close()
    return result and result[0] == 'true'

def set_referral_system(enabled):
    """Enable or disable referral system"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("UPDATE system_settings SET value=? WHERE key='referral_enabled'", 
              ('true' if enabled else 'false',))
    conn.commit()
    conn.close()
    global REFERRAL_SYSTEM_ENABLED
    REFERRAL_SYSTEM_ENABLED = enabled

# Load referral system state
REFERRAL_SYSTEM_ENABLED = is_referral_enabled()

# ==================== DATA STORAGE ====================
data = {}
numbers_by_country = {}
current_country = None
user_messages = {}
user_current_country = {}
temp_uploads = {}
last_change_time = {}
active_users = set()
past_otp_fetch_cooldown = {}
pending_referrals = {}
REQUIRED_CHANNELS = ["@EARNINGTRICKSMASTER1", "@TricksMastarBackup"]

# ==================== REFERRAL FUNCTIONS ====================
def generate_ref_code(chat_id):
    raw = f"{chat_id}_{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()[:8].upper()

def init_user_referral(chat_id, referred_by=None):
    if not REFERRAL_SYSTEM_ENABLED:
        return False
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    
    c.execute("SELECT chat_id FROM user_referrals WHERE chat_id=?", (chat_id,))
    if c.fetchone():
        conn.close()
        return False
    
    ref_code = generate_ref_code(chat_id)
    
    c.execute("""INSERT INTO user_referrals 
                 (chat_id, ref_code, referred_by, created_at)
                 VALUES (?, ?, ?, ?)""",
              (chat_id, ref_code, referred_by, time.time()))
    
    if referred_by:
        c.execute("SELECT chat_id FROM user_referrals WHERE chat_id=?", (referred_by,))
        if c.fetchone():
            c.execute("""UPDATE user_referrals 
                        SET balance = balance + ?,
                            total_referrals = total_referrals + 1,
                            total_earned = total_earned + ?
                        WHERE chat_id = ?""",
                     (REFERRAL_REWARD, REFERRAL_REWARD, referred_by))
            
            c.execute("""INSERT INTO referral_history 
                        (referrer_id, referred_id, reward, created_at)
                        VALUES (?, ?, ?, ?)""",
                     (referred_by, chat_id, REFERRAL_REWARD, time.time()))
            
            c.execute("""INSERT INTO balance_history
                        (chat_id, amount, type, description, created_at)
                        VALUES (?, ?, 'credit', 'Referral reward', ?)""",
                     (referred_by, REFERRAL_REWARD, time.time()))
            
            try:
                bot.send_message(
                    referred_by,
                    f"🎉 <b>New Referral!</b>\n\n"
                    f"💰 You earned <code>${REFERRAL_REWARD:.2f}</code>\n"
                    f"👤 User ID: {chat_id}\n\n"
                    f"Keep sharing your referral link to earn more!"
                )
            except:
                pass
    
    conn.commit()
    conn.close()
    return True

def get_user_ref_data(chat_id):
    if not REFERRAL_SYSTEM_ENABLED:
        return None
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("""SELECT ref_code, balance, total_referrals, total_earned, wallet_address
                 FROM user_referrals WHERE chat_id=?""", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result

def get_chat_by_ref_code(ref_code):
    if not REFERRAL_SYSTEM_ENABLED:
        return None
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM user_referrals WHERE ref_code=?", (ref_code.upper(),))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def set_wallet_address(chat_id, wallet):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("UPDATE user_referrals SET wallet_address=? WHERE chat_id=?", (wallet, chat_id))
    conn.commit()
    conn.close()

def create_withdrawal_request(chat_id, amount, wallet):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    
    twenty_four_hours_ago = time.time() - 86400
    c.execute("""SELECT request_time FROM withdrawal_requests 
                 WHERE chat_id=? AND request_time > ?
                 ORDER BY request_time DESC LIMIT 1""", 
              (chat_id, twenty_four_hours_ago))
    last_withdrawal = c.fetchone()
    
    if last_withdrawal:
        time_left = 86400 - (time.time() - last_withdrawal[0])
        hours_left = int(time_left // 3600)
        minutes_left = int((time_left % 3600) // 60)
        conn.close()
        return False, f"Daily limit reached! Wait {hours_left}h {minutes_left}m"
    
    c.execute("SELECT balance FROM user_referrals WHERE chat_id=?", (chat_id,))
    result = c.fetchone()
    if not result or result[0] < amount:
        conn.close()
        return False, "Insufficient balance"
    
    c.execute("UPDATE user_referrals SET balance = balance - ? WHERE chat_id=?", (amount, chat_id))
    
    c.execute("""INSERT INTO withdrawal_requests
                 (chat_id, amount, wallet_address, request_time)
                 VALUES (?, ?, ?, ?)""",
              (chat_id, amount, wallet, time.time()))
    
    c.execute("""INSERT INTO balance_history
                 (chat_id, amount, type, description, created_at)
                 VALUES (?, ?, 'debit', 'Withdrawal request', ?)""",
              (chat_id, amount, time.time()))
    
    conn.commit()
    request_id = c.lastrowid
    conn.close()
    
    try:
        bot.send_message(
            ADMIN_ID,
            f"💸 <b>New Withdrawal Request</b>\n\n"
            f"🆔 Request ID: <code>{request_id}</code>\n"
            f"👤 User ID: <code>{chat_id}</code>\n"
            f"💰 Amount: <code>${amount:.2f}</code>\n"
            f"🔗 Wallet: <code>{wallet}</code>\n\n"
            f"Use /withdrawals to manage"
        )
    except:
        pass
    
    return True, request_id

def get_user_referrals(chat_id):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("""SELECT referred_id, reward, created_at 
                 FROM referral_history 
                 WHERE referrer_id=? 
                 ORDER BY created_at DESC""", (chat_id,))
    results = c.fetchall()
    conn.close()
    return results

# ==================== DATA FUNCTIONS ====================
def load_data():
    global data, numbers_by_country, current_country
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            numbers_by_country = data.get("numbers_by_country", {})
            current_country = data.get("current_country")
    else:
        data = {"numbers_by_country": {}, "current_country": None}
        numbers_by_country = {}
        current_country = None

def save_data():
    data["numbers_by_country"] = numbers_by_country
    data["current_country"] = current_country
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

load_data()

# ==================== DATABASE HELPERS ====================
def get_chat_by_number(number):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM user_numbers WHERE number=?", (number,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_number_by_chat(chat_id):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("SELECT number FROM user_numbers WHERE chat_id=? ORDER BY assigned_at DESC LIMIT 1", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def assign_number(number, chat_id, country):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_numbers VALUES (?, ?, ?, ?)",
              (number, chat_id, country, time.time()))
    conn.commit()
    conn.close()

def increment_user_stats(chat_id):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("""INSERT INTO user_stats (chat_id, total_otps, last_otp, joined_at) 
                 VALUES (?, 1, ?, ?) 
                 ON CONFLICT(chat_id) DO UPDATE SET 
                 total_otps = total_otps + 1, last_otp = ?""",
              (chat_id, time.time(), time.time(), time.time()))
    conn.commit()
    conn.close()

def cache_past_otp(number, sender, message, otp, timestamp):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO past_otps_cache 
                     (number, sender, message, otp, timestamp, received_at)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (number, sender, message, otp, timestamp, time.time()))
        conn.commit()
    except:
        pass
    conn.close()

def get_cached_past_otps(number, limit=50):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("""SELECT sender, message, otp, timestamp 
                 FROM past_otps_cache 
                 WHERE number=? 
                 ORDER BY received_at DESC 
                 LIMIT ?""", (number, limit))
    results = c.fetchall()
    conn.close()
    return results

def is_message_seen(msg_id):
    if msg_id in seen_messages:
        return True
    seen_messages.append(msg_id)
    return False

def clean_old_cache():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    cutoff = time.time() - 86400
    c.execute("DELETE FROM message_cache WHERE created_at < ?", (cutoff,))
    otp_cutoff = time.time() - (7 * 86400)
    c.execute("DELETE FROM past_otps_cache WHERE received_at < ?", (otp_cutoff,))
    conn.commit()
    conn.close()

# ==================== FLASK ====================
app = Flask(__name__)

@app.route("/")
def index():
    return "🚀 OTP Bot v2.0 ULTRA FAST"

@app.route("/health")
def health():
    return Response(f"OK - Queue: G={group_queue.qsize()} P={personal_queue.qsize()}", status=200)

# ==================== OTP EXTRACTION ====================
def extract_otp(message: str) -> str | None:
    message = UNICODE_CLEAN.sub("", message)
    match = KEYWORD_REGEX.search(message)
    if match:
        return re.sub(r"\D", "", match.group(2))
    match = REVERSE_REGEX.search(message)
    if match:
        return re.sub(r"\D", "", match.group(1))
    match = GENERIC_REGEX.findall(message)
    if match:
        return re.sub(r"\D", "", match[0])
    return None

def mask_number(number: str) -> str:
    number = number.strip()
    if len(number) < 10:
        return number
    return number[:6] + "**" + number[-4:]

def country_from_number(number: str) -> tuple[str, str]:
    try:
        parsed = phonenumbers.parse("+" + number)
        region = phonenumbers.region_code_for_number(parsed)
        if not region:
            return "Unknown", "🌐"
        country_obj = pycountry.countries.get(alpha_2=region)
        if not country_obj:
            return "Unknown", "🌐"
        flag = "".join([chr(127397 + ord(c)) for c in region])
        return country_obj.name, flag
    except:
        return "Unknown", "🌐"

# ==================== MESSAGE FORMATTERS ====================
def format_group_message(record):
    number = record.get("num") or "Unknown"
    sender = record.get("cli") or "Unknown"
    message = record.get("message") or ""
    dt = record.get("dt") or ""
    
    country, flag = country_from_number(number)
    otp = extract_otp(message)
    otp_line = f"<blockquote> <b>OTP:</b> <code>{html.escape(otp)}</code></blockquote>\n" if otp else ""
    
    formatted = (
        f"{flag} <b>New {html.escape(sender)} OTP Received</b>\n\n"
        f"<blockquote> <b>Time:</b> {html.escape(str(dt))}</blockquote>\n"
        f"<blockquote> <b>Country:</b> {html.escape(country)} {flag}</blockquote>\n"
        f"<blockquote> <b>Service:</b> {html.escape(sender)}</blockquote>\n"
        f"<blockquote> <b>Number:</b> {html.escape(mask_number(number))}</blockquote>\n"
        f"{otp_line}"
        f"<blockquote>✉️ <b>Message:</b></blockquote>\n"
        f"<blockquote><code>{html.escape(message[:300])}</code></blockquote>\n\n"
    )
    
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🚀 Panel", url=CHANNEL_LINK),
        types.InlineKeyboardButton("📢 Channel", url=BACKUP)
    )
    
    return formatted, kb

def format_personal_message(record):
    number = record.get("num") or "Unknown"
    sender = record.get("cli") or "Unknown"
    message = record.get("message") or ""
    
    otp = extract_otp(message)
    otp_display = f"<b>🎯 OTP:</b> <code>{html.escape(otp)}</code>\n\n" if otp else ""
    
    formatted = (
        f"📨 <b>New OTP Received!</b>\n\n"
        f"{otp_display}"
        f"<b>📱 Service:</b> {html.escape(sender)}\n"
        f"<b>📞 Number:</b> <code>{number}</code>\n\n"
        f"<b>💬 Full Message:</b>\n"
        f"<blockquote>{html.escape(message)}</blockquote>"
    )
    
    return formatted

# ==================== OPTIMIZED THREAD 1: FASTER OTP SCRAPER ====================
def otp_scraper_thread():
    print("🟢 ULTRA FAST OTP Scraper Started", flush=True)
    
    while True:
        try:
            response = requests.get(
                f"{BASE_URL}/viewstats",
                params={
                    "token": API_TOKEN,
                    "dt1": "1970-01-01 00:00:00",
                    "dt2": "2099-12-31 23:59:59",
                    "records": 20  # Increased from 10 to 20
                },
                timeout=5  # Reduced timeout
            )
            
            if response.status_code == 200:
                stats = response.json()
                
                if stats.get("status") == "success":
                    for record in stats["data"]:
                        msg_id = f"{record.get('dt')}_{record.get('num')}_{record.get('message')[:50]}"
                        
                        if is_message_seen(msg_id):
                            continue
                        
                        number = str(record.get("num", "")).lstrip("0").lstrip("+")
                        sender = record.get("cli", "Unknown")
                        message = record.get("message", "")
                        timestamp = record.get("dt", "")
                        otp = extract_otp(message)
                        
                        cache_past_otp(number, sender, message, otp, timestamp)
                        
                        # Priority: 0 = highest priority, counter ensures unique ordering
                        try:
                            group_queue.put_nowait((0, next(queue_counter), record))
                        except:
                            pass
                        
                        chat_id = get_chat_by_number(number)
                        if chat_id:
                            try:
                                personal_queue.put_nowait((0, next(queue_counter), record, chat_id))
                            except:
                                pass
            
            time.sleep(0.15)  # Reduced from 0.3 to 0.15 seconds
            
        except Exception as e:
            print(f"❌ Scraper error: {e}", flush=True)
            time.sleep(1)

# ==================== OPTIMIZED THREAD 2: PARALLEL GROUP SENDERS ====================
def group_sender_thread(thread_id):
    print(f"🟢 Group Sender #{thread_id} Started", flush=True)
    
    # Staggered start to avoid all threads hitting rate limit together
    time.sleep(thread_id * 0.1)
    
    while True:
        try:
            priority, counter, record = group_queue.get()
            
            msg, kb = format_group_message(record)
            
            payload = {
                "chat_id": OTP_GROUP_ID,
                "text": msg[:4000],
                "parse_mode": "HTML",
                "reply_markup": kb.to_json()
            }
            
            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=3
            )
            
            if response.status_code == 200:
                print(f"✅ Group sent by #{thread_id}", flush=True)
            elif response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 1)
                time.sleep(retry_after)
                group_queue.put((priority, counter, record))
            
            time.sleep(0.35)  # Reduced delay between messages
            
        except Exception as e:
            print(f"❌ Group sender #{thread_id} error: {e}", flush=True)
            time.sleep(0.5)

# ==================== OPTIMIZED THREAD 3: PARALLEL PERSONAL SENDERS ====================
def personal_sender_thread(thread_id):
    print(f"🟢 Personal Sender #{thread_id} Started", flush=True)
    
    # Staggered start
    time.sleep(thread_id * 0.05)
    
    while True:
        try:
            priority, counter, record, chat_id = personal_queue.get()
            
            msg = format_personal_message(record)
            
            payload = {
                "chat_id": chat_id,
                "text": msg[:4000],
                "parse_mode": "HTML"
            }
            
            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=3
            )
            
            if response.status_code == 200:
                increment_user_stats(chat_id)
                print(f"✅ DM sent by #{thread_id} to {chat_id}", flush=True)
            elif response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 1)
                time.sleep(retry_after)
                personal_queue.put((priority, counter, record, chat_id))
            
            time.sleep(0.08)  # Very short delay for personal messages
            
        except Exception as e:
            print(f"❌ Personal sender #{thread_id} error: {e}", flush=True)
            time.sleep(0.3)

# ==================== ADMIN REFERRAL TOGGLE ====================
@bot.message_handler(commands=["togglerefer"])
def toggle_referral_system(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    current_state = is_referral_enabled()
    new_state = not current_state
    set_referral_system(new_state)
    
    status = "✅ ENABLED" if new_state else "❌ DISABLED"
    bot.reply_to(
        message,
        f"🔄 <b>Referral System Status Changed</b>\n\n"
        f"New Status: <b>{status}</b>\n\n"
        f"{'Users can now use /refer command' if new_state else 'Referral commands are now disabled for users'}"
    )

@bot.message_handler(commands=["referstatus"])
def referral_status(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    status = "✅ ENABLED" if is_referral_enabled() else "❌ DISABLED"
    bot.reply_to(
        message,
        f"💼 <b>Referral System Status</b>\n\n"
        f"Current Status: <b>{status}</b>\n\n"
        f"Use /togglerefer to change"
    )

# ==================== USER REFERRAL COMMANDS ====================
@bot.message_handler(commands=["refer"])
def refer_command(message):
    if not REFERRAL_SYSTEM_ENABLED:
        bot.reply_to(message, "❌ Referral system is currently disabled by admin.")
        return
    
    chat_id = message.chat.id
    
    ref_data = get_user_ref_data(chat_id)
    if not ref_data:
        init_user_referral(chat_id)
        ref_data = get_user_ref_data(chat_id)
    
    if not ref_data:
        bot.reply_to(message, "❌ Error initializing referral account.")
        return
    
    ref_code, balance, total_refs, total_earned, wallet = ref_data
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    twenty_four_hours_ago = time.time() - 86400
    c.execute("""SELECT request_time FROM withdrawal_requests 
                 WHERE chat_id=? AND request_time > ?
                 ORDER BY request_time DESC LIMIT 1""", 
              (chat_id, twenty_four_hours_ago))
    last_withdrawal = c.fetchone()
    conn.close()
    
    withdrawal_status = "✅ Available"
    if last_withdrawal:
        time_left = 86400 - (time.time() - last_withdrawal[0])
        hours_left = int(time_left // 3600)
        minutes_left = int((time_left % 3600) // 60)
        withdrawal_status = f"⏳ Wait {hours_left}h {minutes_left}m"
    
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={ref_code}"
    
    text = f"""
💰 <b>Referral Program</b>

<b>Your Stats:</b>
💵 Balance: <code>${balance:.2f}</code>
👥 Total Referrals: <code>{total_refs}</code>
💸 Total Earned: <code>${total_earned:.2f}</code>

<b>Your Referral Link:</b>
<code>{ref_link}</code>

<b>How it works:</b>
• Share your link with friends
• Earn <code>${REFERRAL_REWARD:.2f}</code> per referral
• Minimum withdrawal: <code>${MIN_WITHDRAWAL:.2f}</code>
• Daily limit: 1 withdrawal per 24 hours

<b>Wallet Address:</b>
{f'<code>{wallet}</code>' if wallet else '❌ Not set'}

<b>Withdrawal Status:</b> {withdrawal_status}
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("💳 Set Wallet", callback_data="set_wallet"),
        types.InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_menu")
    )
    markup.row(
        types.InlineKeyboardButton("👥 My Referrals", callback_data="my_referrals"),
        types.InlineKeyboardButton("📊 History", callback_data="balance_history")
    )
    markup.row(
        types.InlineKeyboardButton(
            "💸 Share & Earn 💰",
            url=f"https://t.me/share/url?url={ref_link}&text=🎯 Get UNLIMITED FREE virtual numbers for OTPs + earn ${REFERRAL_REWARD} for every friend you invite! 🚀 Join now!"
        )
    )
    
    bot.reply_to(message, text, reply_markup=markup, disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda call: call.data == "set_wallet")
def set_wallet_callback(call):
    if not REFERRAL_SYSTEM_ENABLED:
        bot.answer_callback_query(call.id, "❌ Referral system is disabled")
        return
    
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "💳 <b>Set TRC20 Wallet Address</b>\n\n"
        "Please send your TRC20 (USDT) wallet address:\n\n"
        "⚠️ Make sure it's correct! We won't be responsible for wrong addresses."
    )
    bot.register_next_step_handler(msg, process_wallet_address)

def process_wallet_address(message):
    chat_id = message.chat.id
    wallet = message.text.strip()
    
    if not wallet.startswith("T") or len(wallet) != 34:
        bot.reply_to(message, "❌ Invalid TRC20 address! Must start with 'T' and be 34 characters.")
        return
    
    set_wallet_address(chat_id, wallet)
    bot.reply_to(message, f"✅ Wallet address saved!\n\n<code>{wallet}</code>")

@bot.callback_query_handler(func=lambda call: call.data == "withdraw_menu")
def withdraw_menu(call):
    if not REFERRAL_SYSTEM_ENABLED:
        bot.answer_callback_query(call.id, "❌ Referral system is disabled")
        return
    
    chat_id = call.message.chat.id
    
    ref_data = get_user_ref_data(chat_id)
    if not ref_data:
        bot.answer_callback_query(call.id, "❌ Account not found")
        return
    
    ref_code, balance, total_refs, total_earned, wallet = ref_data
    
    if not wallet:
        bot.answer_callback_query(call.id, "❌ Set wallet address first!", show_alert=True)
        return
    
    if balance < MIN_WITHDRAWAL:
        bot.answer_callback_query(call.id, f"❌ Minimum withdrawal: ${MIN_WITHDRAWAL:.2f}", show_alert=True)
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    twenty_four_hours_ago = time.time() - 86400
    c.execute("""SELECT request_time FROM withdrawal_requests 
                 WHERE chat_id=? AND request_time > ?
                 ORDER BY request_time DESC LIMIT 1""", 
              (chat_id, twenty_four_hours_ago))
    last_withdrawal = c.fetchone()
    conn.close()
    
    if last_withdrawal:
        time_left = 86400 - (time.time() - last_withdrawal[0])
        hours_left = int(time_left // 3600)
        minutes_left = int((time_left % 3600) // 60)
        bot.answer_callback_query(
            call.id, 
            f"⏳ Daily limit reached! Wait {hours_left}h {minutes_left}m", 
            show_alert=True
        )
        return
    
    text = f"""
💸 <b>Withdrawal</b>

💵 Available Balance: <code>${balance:.2f}</code>
💳 Wallet: <code>{wallet}</code>

<b>Minimum:</b> ${MIN_WITHDRAWAL:.2f}
<b>Daily Limit:</b> 1 withdrawal per 24 hours

Enter amount to withdraw:
"""
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(call.message, process_withdrawal_amount, balance, wallet)

def process_withdrawal_amount(message, balance, wallet):
    chat_id = message.chat.id
    
    try:
        amount = float(message.text.strip())
    except:
        bot.reply_to(message, "❌ Invalid amount! Please enter a number.")
        return
    
    if amount < MIN_WITHDRAWAL:
        bot.reply_to(message, f"❌ Minimum withdrawal is ${MIN_WITHDRAWAL:.2f}")
        return
    
    if amount > balance:
        bot.reply_to(message, f"❌ Insufficient balance! You have ${balance:.2f}")
        return
    
    success, result = create_withdrawal_request(chat_id, amount, wallet)
    
    if success:
        bot.reply_to(
            message,
            f"✅ <b>Withdrawal Request Created!</b>\n\n"
            f"🆔 Request ID: <code>{result}</code>\n"
            f"💰 Amount: <code>${amount:.2f}</code>\n"
            f"💳 Wallet: <code>{wallet}</code>\n\n"
            f"⏳ Your request is pending admin approval.\n"
            f"You'll be notified once processed."
        )
    else:
        bot.reply_to(message, f"❌ Error: {result}")

@bot.callback_query_handler(func=lambda call: call.data == "my_referrals")
def my_referrals_callback(call):
    if not REFERRAL_SYSTEM_ENABLED:
        bot.answer_callback_query(call.id, "❌ Referral system is disabled")
        return
    
    chat_id = call.message.chat.id
    
    referrals = get_user_referrals(chat_id)
    
    if not referrals:
        bot.answer_callback_query(call.id, "❌ No referrals yet!")
        return
    
    text = f"👥 <b>Your Referrals ({len(referrals)})</b>\n\n"
    
    for i, (referred_id, reward, created_at) in enumerate(referrals[:20], 1):
        date = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M")
        text += f"{i}. User <code>{referred_id}</code>\n"
        text += f"   💰 ${reward:.2f} • {date}\n\n"
    
    if len(referrals) > 20:
        text += f"\n<i>Showing 20 of {len(referrals)} referrals</i>"
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "balance_history")
def balance_history_callback(call):
    if not REFERRAL_SYSTEM_ENABLED:
        bot.answer_callback_query(call.id, "❌ Referral system is disabled")
        return
    
    chat_id = call.message.chat.id
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("""SELECT amount, type, description, created_at 
                 FROM balance_history 
                 WHERE chat_id=? 
                 ORDER BY created_at DESC 
                 LIMIT 20""", (chat_id,))
    history = c.fetchall()
    conn.close()
    
    if not history:
        bot.answer_callback_query(call.id, "❌ No history found!")
        return
    
    text = "📊 <b>Balance History</b>\n\n"
    
    for amount, typ, desc, created_at in history:
        date = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M")
        symbol = "+" if typ == "credit" else "-"
        emoji = "💰" if typ == "credit" else "💸"
        text += f"{emoji} {symbol}${abs(amount):.2f}\n"
        text += f"   {desc} • {date}\n\n"
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id)

# ==================== ADMIN REFERRAL COMMANDS ====================
@bot.message_handler(commands=["withdrawals"])
def view_withdrawals(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("""SELECT id, chat_id, amount, wallet_address, status, request_time
                 FROM withdrawal_requests 
                 ORDER BY request_time DESC 
                 LIMIT 20""")
    requests_list = c.fetchall()
    conn.close()
    
    if not requests_list:
        bot.reply_to(message, "🔭 No withdrawal requests")
        return
    
    text = "💸 <b>Withdrawal Requests</b>\n\n"
    
    for req_id, chat_id, amount, wallet, status, req_time in requests_list:
        date = datetime.fromtimestamp(req_time).strftime("%Y-%m-%d %H:%M")
        status_emoji = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(status, "❓")
        
        text += f"{status_emoji} <b>ID {req_id}</b> • {status.upper()}\n"
        text += f"   👤 User: <code>{chat_id}</code>\n"
        text += f"   💰 Amount: <code>${amount:.2f}</code>\n"
        text += f"   💳 Wallet: <code>{wallet}</code>\n"
        text += f"   🕐 {date}\n\n"
    
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Approve", callback_data="admin_approve"),
        types.InlineKeyboardButton("❌ Reject", callback_data="admin_reject")
    )
    markup.row(
        types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_refresh_withdrawals")
    )
    
    bot.reply_to(message, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "admin_approve")
def admin_approve_callback(call):
    if call.from_user.id != ADMIN_ID:
        return
    
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "✅ <b>Approve Withdrawal</b>\n\n"
        "Send: <code>Request_ID TXN_ID</code>\n\n"
        "Example: <code>1 abc123xyz456</code>"
    )
    bot.register_next_step_handler(msg, process_approve_withdrawal)

def process_approve_withdrawal(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        parts = message.text.strip().split()
        req_id = int(parts[0])
        txn_id = parts[1] if len(parts) > 1 else "N/A"
    except:
        bot.reply_to(message, "❌ Invalid format! Use: <code>Request_ID TXN_ID</code>")
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    
    c.execute("SELECT chat_id, amount, status FROM withdrawal_requests WHERE id=?", (req_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        bot.reply_to(message, "❌ Request not found!")
        return
    
    chat_id, amount, status = result
    
    if status != "pending":
        conn.close()
        bot.reply_to(message, f"❌ Request already {status}!")
        return
    
    c.execute("""UPDATE withdrawal_requests 
                 SET status='approved', processed_time=?, processed_by=?, txn_id=?
                 WHERE id=?""",
              (time.time(), ADMIN_ID, txn_id, req_id))
    conn.commit()
    conn.close()
    
    try:
        bot.send_message(
            chat_id,
            f"✅ <b>Withdrawal Approved!</b>\n\n"
            f"🆔 Request ID: <code>{req_id}</code>\n"
            f"💰 Amount: <code>${amount:.2f}</code>\n"
            f"🔗 Transaction ID: <code>{txn_id}</code>\n\n"
            f"Your payment has been processed!"
        )
    except:
        pass
    
    bot.reply_to(message, f"✅ Request #{req_id} approved and user notified!")

@bot.callback_query_handler(func=lambda call: call.data == "admin_reject")
def admin_reject_callback(call):
    if call.from_user.id != ADMIN_ID:
        return
    
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "❌ <b>Reject Withdrawal</b>\n\n"
        "Send Request ID to reject:"
    )
    bot.register_next_step_handler(msg, process_reject_withdrawal)

def process_reject_withdrawal(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        req_id = int(message.text.strip())
    except:
        bot.reply_to(message, "❌ Invalid Request ID!")
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    
    c.execute("SELECT chat_id, amount, status FROM withdrawal_requests WHERE id=?", (req_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        bot.reply_to(message, "❌ Request not found!")
        return
    
    chat_id, amount, status = result
    
    if status != "pending":
        conn.close()
        bot.reply_to(message, f"❌ Request already {status}!")
        return
    
    c.execute("UPDATE user_referrals SET balance = balance + ? WHERE chat_id=?", (amount, chat_id))
    
    c.execute("""UPDATE withdrawal_requests 
                 SET status='rejected', processed_time=?, processed_by=?
                 WHERE id=?""",
              (time.time(), ADMIN_ID, req_id))
    
    c.execute("""INSERT INTO balance_history
                 (chat_id, amount, type, description, created_at)
                 VALUES (?, ?, 'credit', 'Withdrawal rejected - refund', ?)""",
              (chat_id, amount, time.time()))
    
    conn.commit()
    conn.close()
    
    try:
        bot.send_message(
            chat_id,
            f"❌ <b>Withdrawal Rejected</b>\n\n"
            f"🆔 Request ID: <code>{req_id}</code>\n"
            f"💰 Amount: <code>${amount:.2f}</code>\n\n"
            f"Your balance has been refunded.\n"
            f"Contact admin for more details."
        )
    except:
        pass
    
    bot.reply_to(message, f"✅ Request #{req_id} rejected and balance refunded!")

@bot.message_handler(commands=["addbalance"])
def add_balance_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    msg = bot.reply_to(
        message,
        "💰 <b>Add Balance</b>\n\n"
        "Send: <code>USER_ID AMOUNT</code>\n\n"
        "Example: <code>123456789 5.00</code>"
    )
    bot.register_next_step_handler(msg, process_add_balance)

def process_add_balance(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        parts = message.text.strip().split()
        user_id = int(parts[0])
        amount = float(parts[1])
    except:
        bot.reply_to(message, "❌ Invalid format! Use: <code>USER_ID AMOUNT</code>")
        return
    
    if amount <= 0:
        bot.reply_to(message, "❌ Amount must be positive!")
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    
    c.execute("SELECT chat_id FROM user_referrals WHERE chat_id=?", (user_id,))
    if not c.fetchone():
        init_user_referral(user_id)
    
    c.execute("""UPDATE user_referrals 
                 SET balance = balance + ?, total_earned = total_earned + ?
                 WHERE chat_id=?""",
              (amount, amount, user_id))
    
    c.execute("""INSERT INTO balance_history
                 (chat_id, amount, type, description, created_at)
                 VALUES (?, ?, 'credit', 'Admin credit', ?)""",
              (user_id, amount, time.time()))
    
    conn.commit()
    conn.close()
    
    try:
        bot.send_message(
            user_id,
            f"💰 <b>Balance Added!</b>\n\n"
            f"Amount: <code>${amount:.2f}</code>\n"
            f"Added by admin"
        )
    except:
        pass
    
    bot.reply_to(message, f"✅ Added ${amount:.2f} to user {user_id}")

@bot.message_handler(commands=["removebalance"])
def remove_balance_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    msg = bot.reply_to(
        message,
        "💸 <b>Remove Balance</b>\n\n"
        "Send: <code>USER_ID AMOUNT</code>\n\n"
        "Example: <code>123456789 2.00</code>"
    )
    bot.register_next_step_handler(msg, process_remove_balance)

def process_remove_balance(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        parts = message.text.strip().split()
        user_id = int(parts[0])
        amount = float(parts[1])
    except:
        bot.reply_to(message, "❌ Invalid format! Use: <code>USER_ID AMOUNT</code>")
        return
    
    if amount <= 0:
        bot.reply_to(message, "❌ Amount must be positive!")
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    
    c.execute("SELECT balance FROM user_referrals WHERE chat_id=?", (user_id,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        bot.reply_to(message, "❌ User not found!")
        return
    
    current_balance = result[0]
    
    if amount > current_balance:
        conn.close()
        bot.reply_to(message, f"❌ User only has ${current_balance:.2f}")
        return
    
    c.execute("UPDATE user_referrals SET balance = balance - ? WHERE chat_id=?",
              (amount, user_id))
    
    c.execute("""INSERT INTO balance_history
                 (chat_id, amount, type, description, created_at)
                 VALUES (?, ?, 'debit', 'Admin deduction', ?)""",
              (user_id, amount, time.time()))
    
    conn.commit()
    conn.close()
    
    try:
        bot.send_message(
            user_id,
            f"💸 <b>Balance Deducted</b>\n\n"
            f"Amount: <code>${amount:.2f}</code>\n"
            f"Deducted by admin"
        )
    except:
        pass
    
    bot.reply_to(message, f"✅ Removed ${amount:.2f} from user {user_id}")

@bot.message_handler(commands=["userbalance"])
def user_balance_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        user_id = int(message.text.split()[1])
    except:
        bot.reply_to(message, "Usage: /userbalance USER_ID")
        return
    
    ref_data = get_user_ref_data(user_id)
    
    if not ref_data:
        bot.reply_to(message, "❌ User not found!")
        return
    
    ref_code, balance, total_refs, total_earned, wallet = ref_data
    
    text = f"""
👤 <b>User Balance Info</b>

🆔 User ID: <code>{user_id}</code>
🔗 Ref Code: <code>{ref_code}</code>

💰 Current Balance: <code>${balance:.2f}</code>
👥 Total Referrals: <code>{total_refs}</code>
💸 Total Earned: <code>${total_earned:.2f}</code>

💳 Wallet: {f'<code>{wallet}</code>' if wallet else '❌ Not set'}
"""
    
    bot.reply_to(message, text)

@bot.message_handler(commands=["refstats"])
def ref_stats_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM user_referrals")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM referral_history")
    total_referrals = c.fetchone()[0]
    
    c.execute("SELECT SUM(balance) FROM user_referrals")
    total_balance = c.fetchone()[0] or 0
    
    c.execute("SELECT SUM(total_earned) FROM user_referrals")
    total_earned = c.fetchone()[0] or 0
    
    c.execute("SELECT COUNT(*), SUM(amount) FROM withdrawal_requests WHERE status='pending'")
    pending_count, pending_amount = c.fetchone()
    pending_amount = pending_amount or 0
    
    c.execute("""SELECT chat_id, total_referrals, total_earned 
                 FROM user_referrals 
                 ORDER BY total_referrals DESC 
                 LIMIT 10""")
    top_referrers = c.fetchall()
    
    conn.close()
    
    status = "✅ ENABLED" if REFERRAL_SYSTEM_ENABLED else "❌ DISABLED"
    
    text = f"""
📊 <b>Referral System Statistics</b>

🔄 System Status: <b>{status}</b>

👥 Total Users: <code>{total_users}</code>
🔗 Total Referrals: <code>{total_referrals}</code>
💰 Total Balance: <code>${total_balance:.2f}</code>
💸 Total Earned: <code>${total_earned:.2f}</code>

⏳ Pending Withdrawals: <code>{pending_count}</code>
💵 Pending Amount: <code>${pending_amount:.2f}</code>

<b>🏆 Top 10 Referrers:</b>
"""
    
    for i, (chat_id, refs, earned) in enumerate(top_referrers, 1):
        text += f"\n{i}. User <code>{chat_id}</code>"
        text += f"\n   {refs} refs • ${earned:.2f}\n"
    
    bot.reply_to(message, text)

# ==================== OTHER ADMIN COMMANDS ====================
@bot.message_handler(content_types=["document"])
def handle_document(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ Not authorized")
    
    if not message.document.file_name.endswith(".txt"):
        return bot.reply_to(message, "❌ Upload .txt file only")
    
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    numbers = [line.strip().lstrip("0").lstrip("+") 
               for line in downloaded_file.decode("utf-8").splitlines() if line.strip()]
    
    if not numbers:
        return bot.reply_to(message, "❌ File is empty")
    
    temp_uploads[message.from_user.id] = numbers
    
    markup = types.InlineKeyboardMarkup()
    for country in sorted(numbers_by_country.keys()):
        markup.add(types.InlineKeyboardButton(country, callback_data=f"addto_{country}"))
    markup.add(types.InlineKeyboardButton("➕ New Country", callback_data="addto_new"))
    
    bot.reply_to(message, f"📂 Received {len(numbers)} numbers. Select country:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("addto_"))
def callback_addto(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "❌ Not authorized")
    
    numbers = temp_uploads.get(call.from_user.id, [])
    if not numbers:
        return bot.answer_callback_query(call.id, "❌ No numbers found")
    
    choice = call.data[6:]
    
    if choice == "new":
        bot.send_message(call.message.chat.id, "✏️ Send new country name:")
        bot.register_next_step_handler(call.message, save_new_country, numbers)
    else:
        existing = numbers_by_country.get(choice, [])
        merged = list(set(existing + numbers))
        numbers_by_country[choice] = merged
        save_data()
        
        file_path = os.path.join(NUMBERS_DIR, f"{choice}.txt")
        with open(file_path, "w") as f:
            f.write("\n".join(merged))
        
        bot.edit_message_text(
            f"✅ Added {len(numbers)} numbers to <b>{choice}</b>",
            call.message.chat.id,
            call.message.message_id
        )
        temp_uploads.pop(call.from_user.id, None)

def save_new_country(message, numbers):
    country = message.text.strip()
    if not country:
        return bot.reply_to(message, "❌ Invalid country name")
    
    numbers_by_country[country] = numbers
    save_data()
    
    file_path = os.path.join(NUMBERS_DIR, f"{country}.txt")
    with open(file_path, "w") as f:
        f.write("\n".join(numbers))
    
    bot.reply_to(message, f"✅ Saved {len(numbers)} numbers under <b>{country}</b>")
    temp_uploads.pop(message.from_user.id, None)

@bot.message_handler(commands=["adminhelp"])
def admin_help(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    help_text = """
🔧 <b>Admin Commands:</b>

<b>📤 Number Management:</b>
• Upload .txt file - Add numbers
• /setcountry &lt;name&gt; - Set current country
• /deletecountry &lt;name&gt; - Delete country
• /cleannumbers &lt;name&gt; - Clear numbers
• /listcountries - View all countries

<b>💰 Referral Management:</b>
• /togglerefer - Enable/Disable referral system
• /referstatus - Check referral system status
• /withdrawals - View withdrawal requests
• /addbalance - Add balance to user
• /removebalance - Remove balance from user
• /userbalance &lt;id&gt; - Check user balance
• /refstats - Referral statistics

<b>📊 General:</b>
• /broadcast - Send message to all users
• /stats - Bot statistics
• /usercount - Total users
• /clearcache - Clear past OTP cache
"""
    bot.reply_to(message, help_text)

@bot.message_handler(commands=["stats"])
def bot_stats(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM past_otps_cache")
    cache_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM user_referrals")
    ref_users = c.fetchone()[0]
    conn.close()
    
    ref_status = "✅ ON" if REFERRAL_SYSTEM_ENABLED else "❌ OFF"
    
    stats_text = f"""
📊 <b>Bot Statistics:</b>

👥 Active Users: {len(active_users)}
🔗 Referral Users: {ref_users}
🎯 Referral System: {ref_status}
📥 Group Queue: {group_queue.qsize()}
📨 Personal Queue: {personal_queue.qsize()}
💾 Cached Messages: {len(seen_messages)}
💿 Past OTPs Cache: {cache_count}
🌐 Countries: {len(numbers_by_country)}
📞 Total Numbers: {sum(len(v) for v in numbers_by_country.values())}

⚡ <b>Performance:</b>
🔥 Group Senders: {NUM_GROUP_SENDERS} threads
🚀 Personal Senders: {NUM_PERSONAL_SENDERS} threads
"""
    bot.reply_to(message, stats_text)

@bot.message_handler(commands=["broadcast"])
def broadcast_start(message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(message, "✉️ Send broadcast message:")
    bot.register_next_step_handler(msg, broadcast_message)

def broadcast_message(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    text = message.text.strip()
    if not text:
        return bot.reply_to(message, "❌ Broadcast message cannot be empty.")

    users = list(active_users)
    total = len(users)
    if total == 0:
        return bot.reply_to(message, "❌ No active users found.")

    bot.reply_to(message, f"📢 Starting broadcast to <b>{total}</b> users...")

    sent = 0
    failed = 0
    start_time = time.time()

    progress_msg = bot.send_message(
        message.chat.id,
        f"📤 <b>Broadcast Progress:</b>\n\n✅ Sent: {sent}/{total}\n❌ Failed: {failed}"
    )

    for i, user_id in enumerate(users, start=1):
        try:
            bot.send_message(user_id, f"📢 <b>Broadcast:</b>\n\n{text}")
            sent += 1
        except:
            failed += 1

        if i % 10 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📤 <b>Broadcast Progress:</b>\n\n✅ Sent: {sent}/{total}\n❌ Failed: {failed}",
                    message.chat.id,
                    progress_msg.message_id
                )
            except:
                pass
        
        time.sleep(0.1)

    elapsed = time.time() - start_time
    bot.edit_message_text(
        f"✅ <b>Broadcast Complete!</b>\n\n📨 Sent: {sent}/{total}\n❌ Failed: {failed}\n⏱️ Time: {elapsed:.1f}s",
        message.chat.id,
        progress_msg.message_id
    )

@bot.message_handler(commands=["clearcache"])
def clear_cache(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("DELETE FROM past_otps_cache")
    deleted = c.rowcount
    conn.commit()
    conn.close()
    
    bot.reply_to(message, f"✅ Cleared {deleted} cached OTPs")

# ==================== USER COMMANDS ====================
@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id
    
    # Handle referral code
    ref_code_from_link = None
    if len(message.text.split()) > 1 and REFERRAL_SYSTEM_ENABLED:
        ref_code_from_link = message.text.split()[1].upper()
        referrer_id = get_chat_by_ref_code(ref_code_from_link)
        
        if referrer_id and referrer_id != chat_id:
            existing_user = get_user_ref_data(chat_id)
            if not existing_user:
                pending_referrals[chat_id] = referrer_id
                bot.send_message(
                    chat_id,
                    f"🎉 <b>Welcome!</b>\n\n"
                    f"You've been invited by a friend!\n"
                    f"Join the required channels to use the bot.\n\n"
                    f"Both you and your referrer will benefit! 💰"
                )
            else:
                init_user_referral(chat_id)
        else:
            init_user_referral(chat_id)
    else:
        if REFERRAL_SYSTEM_ENABLED:
            init_user_referral(chat_id)
    
    if message.from_user.id == ADMIN_ID:
        bot.send_message(chat_id, "👋 Welcome Admin! Use /adminhelp")
        return
    
    active_users.add(chat_id)
    
    # Check channel membership
    not_joined = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = bot.get_chat_member(channel, chat_id)
            if member.status not in ["member", "creator", "administrator"]:
                not_joined.append(channel)
        except:
            not_joined.append(channel)
    
    if not_joined:
        markup = types.InlineKeyboardMarkup()
        for ch in not_joined:
            markup.add(types.InlineKeyboardButton(f"Join {ch}", url=f"https://t.me/{ch[1:]}"))
        markup.add(types.InlineKeyboardButton("✅ Verify", callback_data="verify_join"))
        
        join_msg = "❌ <b>Join required channels first:</b>\n\n"
        if chat_id in pending_referrals and REFERRAL_SYSTEM_ENABLED:
            join_msg += "🎁 <b>Referral bonus waiting!</b>\n"
            join_msg += f"Join channels to earn unlimited numbers\n\n"
        
        bot.send_message(chat_id, join_msg, reply_markup=markup)
        return
    
    # User has joined - process referral if pending
    if chat_id in pending_referrals and REFERRAL_SYSTEM_ENABLED:
        referrer_id = pending_referrals[chat_id]
        if init_user_referral(chat_id, referrer_id):
            bot.send_message(
                chat_id,
                f"✅ <b>Referral Activated!</b>\n\n"
                f"🎉 Your referrer earned ${REFERRAL_REWARD:.2f}!\n"
                f"💰 Start referring friends with /refer to earn money too!\n\n"
                f"Thank you for joining!"
            )
        del pending_referrals[chat_id]
    
    if not numbers_by_country:
        bot.send_message(chat_id, "❌ No countries available")
        return
    
    # Show country selection
    markup = types.InlineKeyboardMarkup()
    for country in sorted(numbers_by_country.keys()):
        count = len(numbers_by_country[country])
        markup.add(types.InlineKeyboardButton(
            f"{country} ({count} numbers)", 
            callback_data=f"user_select_{country}"
        ))
    
    refer_text = "\n💰 Earn with /refer" if REFERRAL_SYSTEM_ENABLED else ""
    
    msg = bot.send_message(
        chat_id,
        f"🌐 <b>Select Country:</b>\n\n"
        f"⚡️ Fast delivery \n"
        f"🔒 Secure numbers\n"
        f"♻️ Change anytime{refer_text}",
        reply_markup=markup
    )
    user_messages[chat_id] = msg

@bot.message_handler(commands=["mystats"])
def my_stats(message):
    chat_id = message.chat.id
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c = conn.cursor()
    c.execute("SELECT total_otps, last_otp FROM user_stats WHERE chat_id=?", (chat_id,))
    result = c.fetchone()
    conn.close()
    
    if result:
        total, last = result
        last_time = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
        stats_text = f"""
📊 <b>Your Statistics:</b>

📩 Total OTPs: {total}
🕐 Last OTP: {last_time}
⚡️ Status: Active
"""
    else:
        stats_text = "📊 No OTPs received yet!"
    
    bot.reply_to(message, stats_text)

@bot.message_handler(commands=["help"])
def help_command(message):
    refer_line = "• /refer - Referral program & earn money\n" if REFERRAL_SYSTEM_ENABLED else ""
    
    help_text = f"""
📚 <b>Bot Commands:</b>

/start - Get a new number
{refer_line}/mystats - View your statistics
/help - Show this help message

<b>Features:</b>
• ⚡ Ultra-fast OTP delivery
• 📜 View past OTPs
• 🔄 Change number anytime
• 🌐 Multiple countries
"""
    if REFERRAL_SYSTEM_ENABLED:
        help_text += "• 💰 Earn money by referring friends"
    
    bot.reply_to(message, help_text)

def send_random_number(chat_id, country=None, edit=False):
    """Assign random number to user"""
    now = time.time()
    
    # Rate limiting
    if chat_id in last_change_time and now - last_change_time[chat_id] < 10:
        wait = 10 - int(now - last_change_time[chat_id])
        bot.send_message(chat_id, f"⏳ Wait {wait}s before changing number")
        return
    
    last_change_time[chat_id] = now
    
    if country is None:
        country = user_current_country.get(chat_id)
        if not country:
            bot.send_message(chat_id, "❌ No country selected")
            return
    
    numbers = numbers_by_country.get(country, [])
    if not numbers:
        bot.send_message(chat_id, f"❌ No numbers for {country}")
        return
    
    number = random.choice(numbers).lstrip("0").lstrip("+")
    user_current_country[chat_id] = country
    assign_number(number, chat_id, country)
    
    country_info, flag = country_from_number(number)
    
    text = f"""
{flag} <b>Your Number ({country}):</b>

📞 <code>{number}</code>

⏳ <b>Waiting for OTP...</b>
🔔 You'll get notified instantly!
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("🔄 Change Number", callback_data="change_number"),
        types.InlineKeyboardButton("🌐 Change Country", callback_data="change_country")
    )
    markup.row(
        types.InlineKeyboardButton("📜 View Past OTPs", callback_data=f"view_past_{number}")
    )
    markup.row(
        types.InlineKeyboardButton("📢 OTP Group", url=f"https://t.me/tricksmasterotp")
    )

    if chat_id in user_messages and edit:
        try:
            bot.edit_message_text(
                text,
                chat_id,
                user_messages[chat_id].message_id,
                reply_markup=markup
            )
        except:
            msg = bot.send_message(chat_id, text, reply_markup=markup)
            user_messages[chat_id] = msg
    else:
        msg = bot.send_message(chat_id, text, reply_markup=markup)
        user_messages[chat_id] = msg

def fetch_past_otps(chat_id, number):
    """Fetch and display past OTPs for a number from API"""
    
    now = time.time()
    if chat_id in past_otp_fetch_cooldown:
        time_passed = now - past_otp_fetch_cooldown[chat_id]
        if time_passed < 3:
            wait_time = int(3 - time_passed)
            bot.send_message(chat_id, f"⏳ Please wait {wait_time}s before fetching past OTPs again.")
            return
    
    past_otp_fetch_cooldown[chat_id] = now
    
    try:
        loading_msg = bot.send_message(chat_id, "⏳ <b>Fetching past OTPs...</b>\n\nThis may take a few seconds.")
        
        cached_otps = get_cached_past_otps(number, 50)
        
        response = requests.get(
            f"{BASE_URL}/viewstats",
            params={
                "token": API_TOKEN,
                "dt1": "1970-01-01 00:00:00",
                "dt2": "2099-12-31 23:59:59",
                "records": 2000
            },
            timeout=15
        )
        
        bot.delete_message(chat_id, loading_msg.message_id)
        
        if response.status_code != 200:
            bot.send_message(chat_id, "❌ Failed to fetch past OTPs. Try again later.")
            return
        
        data = response.json()
        
        if data.get("status") != "success":
            bot.send_message(chat_id, "❌ No past OTPs found in API response.")
            return
        
        user_messages_list = []
        for record in data.get("data", []):
            record_number = str(record.get("num", "")).lstrip("0").lstrip("+")
            if record_number == number:
                user_messages_list.append(record)
        
        if not user_messages_list and not cached_otps:
            bot.send_message(chat_id, f"🔭 <b>No past OTPs found for:</b>\n<code>{number}</code>")
            return
        
        country_info, flag = country_from_number(number)
        
        msg_text = f"{flag} <b>Past OTPs for {number}</b>\n"
        msg_text += f"<b>Country:</b> {country_info}\n"
        msg_text += f"<b>Total Messages Found:</b> {len(user_messages_list)}\n"
        msg_text += "━━━━━━━━━━━━━━━━━\n\n"
        
        display_count = min(50, len(user_messages_list))
        
        if display_count == 0 and cached_otps:
            msg_text += "<i>📦 Showing cached data:</i>\n\n"
            for i, (sender, message, otp, timestamp) in enumerate(cached_otps[:30], 1):
                otp_display = f"🎯 <code>{html.escape(otp)}</code>" if otp else "❌ No OTP"
                
                msg_text += f"<b>{i}. {html.escape(sender)}</b>\n"
                msg_text += f"   {otp_display}\n"
                msg_text += f"   🕐 {html.escape(timestamp)}\n"
                msg_text += f"   📩 {html.escape(message[:80])}\n\n"
                
                if len(msg_text) > 3500:
                    bot.send_message(chat_id, msg_text, disable_web_page_preview=True)
                    msg_text = ""
        else:
            for i, record in enumerate(user_messages_list[:display_count], 1):
                sender = record.get("cli", "Unknown")
                message = record.get("message", "")
                dt = record.get("dt", "")
                
                otp = extract_otp(message)
                otp_display = f"🎯 <code>{html.escape(otp)}</code>" if otp else "❌ No OTP"
                
                msg_text += f"<b>{i}. {html.escape(sender)}</b>\n"
                msg_text += f"   {otp_display}\n"
                msg_text += f"   🕐 {html.escape(str(dt))}\n"
                msg_text += f"   📩 {html.escape(message[:100])}\n\n"
                
                if len(msg_text) > 3500:
                    bot.send_message(chat_id, msg_text, disable_web_page_preview=True)
                    msg_text = ""
        
        if msg_text:
            if len(user_messages_list) > display_count:
                msg_text += f"\n<i>Showing {display_count} of {len(user_messages_list)} messages</i>"
            bot.send_message(chat_id, msg_text, disable_web_page_preview=True)
        
        summary = f"""
📊 <b>Summary:</b>

✅ Found {len(user_messages_list)} messages
📱 Service providers: {len(set(r.get('cli', 'Unknown') for r in user_messages_list))}
🔑 OTPs extracted: {sum(1 for r in user_messages_list if extract_otp(r.get('message', '')))}
"""
        bot.send_message(chat_id, summary)
        
    except requests.Timeout:
        bot.send_message(chat_id, "❌ Request timeout. API is taking too long. Try again later.")
    except Exception as e:
        print(f"❌ Error fetching past OTPs: {e}", flush=True)
        bot.send_message(chat_id, "❌ Error fetching past OTPs. Please try again later.")

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    
    if call.from_user.id != ADMIN_ID:
        active_users.add(chat_id)
    
    if call.data.startswith("user_select_"):
        country = call.data[12:]
        user_current_country[chat_id] = country
        send_random_number(chat_id, country, edit=True)
    
    elif call.data == "change_number":
        send_random_number(chat_id, user_current_country.get(chat_id), edit=True)
    
    elif call.data == "change_country":
        markup = types.InlineKeyboardMarkup()
        for country in sorted(numbers_by_country.keys()):
            markup.add(types.InlineKeyboardButton(
                country, 
                callback_data=f"user_select_{country}"
            ))
        bot.edit_message_text(
            "🌐 Select Country:",
            chat_id,
            user_messages[chat_id].message_id,
            reply_markup=markup
        )
    
    elif call.data.startswith("view_past_"):
        number = call.data[10:]
        assigned_number = get_number_by_chat(chat_id)
        if assigned_number != number:
            bot.answer_callback_query(call.id, "❌ This is not your current number!")
            return
        
        bot.answer_callback_query(call.id, "⏳ Fetching past OTPs...")
        fetch_past_otps(chat_id, number)
    
    elif call.data == "verify_join":
        not_joined = []
        for channel in REQUIRED_CHANNELS:
            try:
                member = bot.get_chat_member(channel, chat_id)
                if member.status not in ["member", "creator", "administrator"]:
                    not_joined.append(channel)
            except:
                not_joined.append(channel)
        
        if not_joined:
            bot.answer_callback_query(call.id, "❌ Still not joined all channels!")
        else:
            bot.answer_callback_query(call.id, "✅ Verified!")
            
            # Process pending referral
            if chat_id in pending_referrals and REFERRAL_SYSTEM_ENABLED:
                referrer_id = pending_referrals[chat_id]
                if init_user_referral(chat_id, referrer_id):
                    bot.send_message(
                        chat_id,
                        f"✅ <b>Referral Activated!</b>\n\n"
                        f"🎉 Your referrer earned ${REFERRAL_REWARD:.2f}!\n"
                        f"💰 Start referring friends with /refer to earn money too!\n\n"
                        f"Thank you for joining!"
                    )
                del pending_referrals[chat_id]
            
            start(call.message)
    
    elif call.data == "admin_refresh_withdrawals":
        if call.from_user.id != ADMIN_ID:
            return
        bot.delete_message(chat_id, call.message.message_id)
        view_withdrawals(call.message)

@bot.message_handler(commands=["setcountry", "deletecountry", "cleannumbers", "listcountries", "usercount"])
def other_admin_commands(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    cmd = message.text.split()[0][1:]
    
    if cmd == "listcountries":
        if not numbers_by_country:
            return bot.reply_to(message, "❌ No countries")
        text = "🌐 <b>Countries:</b>\n\n"
        for country, nums in sorted(numbers_by_country.items()):
            text += f"• {country}: {len(nums)} numbers\n"
        bot.reply_to(message, text)
    
    elif cmd == "usercount":
        bot.reply_to(message, f"👥 Active users: {len(active_users)}")
    
    elif cmd == "setcountry":
        global current_country
        if len(message.text.split()) > 1:
            current_country = " ".join(message.text.split()[1:])
            save_data()
            bot.reply_to(message, f"✅ Current country: {current_country}")
        else:
            bot.reply_to(message, "Usage: /setcountry <name>")
    
    elif cmd == "deletecountry":
        if len(message.text.split()) > 1:
            country = " ".join(message.text.split()[1:])
            if country in numbers_by_country:
                del numbers_by_country[country]
                save_data()
                bot.reply_to(message, f"✅ Deleted {country}")
            else:
                bot.reply_to(message, "❌ Country not found")
        else:
            bot.reply_to(message, "Usage: /deletecountry <name>")
    
    elif cmd == "cleannumbers":
        if len(message.text.split()) > 1:
            country = " ".join(message.text.split()[1:])
            if country in numbers_by_country:
                numbers_by_country[country] = []
                save_data()
                bot.reply_to(message, f"✅ Cleared {country}")
            else:
                bot.reply_to(message, "❌ Country not found")
        else:
            bot.reply_to(message, "Usage: /cleannumbers <name>")

# ==================== CLEANUP THREAD ====================
def cleanup_thread():
    """Clean old cache every hour"""
    while True:
        time.sleep(3600)
        try:
            clean_old_cache()
            print("🧹 Cleaned old message cache", flush=True)
        except Exception as e:
            print(f"❌ Cleanup error: {e}", flush=True)

# ==================== BOT POLLING ====================
def run_bot():
    """Run Telegram bot with auto-reconnect"""
    while True:
        try:
            print("🤖 Bot polling started...", flush=True)
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"❌ Polling error: {e}", flush=True)
            time.sleep(5)

# ==================== MAIN ====================
if __name__ == "__main__":
    print(f"🚀 OTP Bot v2.0 ULTRA FAST Starting at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📊 Initial stats: {len(numbers_by_country)} countries loaded", flush=True)
    print(f"⚡ Performance: {NUM_GROUP_SENDERS} group senders, {NUM_PERSONAL_SENDERS} personal senders", flush=True)
    print(f"🎯 Referral System: {'ENABLED' if REFERRAL_SYSTEM_ENABLED else 'DISABLED'}", flush=True)
    
    # Start all threads
    threading.Thread(target=run_bot, daemon=True, name="BotPoller").start()
    threading.Thread(target=otp_scraper_thread, daemon=True, name="OTPScraper").start()
    
    # Start multiple group sender threads
    for i in range(NUM_GROUP_SENDERS):
        threading.Thread(target=group_sender_thread, args=(i+1,), daemon=True, name=f"GroupSender-{i+1}").start()
    
    # Start multiple personal sender threads
    for i in range(NUM_PERSONAL_SENDERS):
        threading.Thread(target=personal_sender_thread, args=(i+1,), daemon=True, name=f"PersonalSender-{i+1}").start()
    
    threading.Thread(target=cleanup_thread, daemon=True, name="Cleaner").start()
    
    print("✅ All threads started successfully!", flush=True)
    
    # Start Flask server
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
