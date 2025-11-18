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
from queue import Queue
from datetime import datetime, timedelta
from collections import deque

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN") 
ADMIN_ID = 6483088050
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

DATA_FILE = "bot_data.json"
NUMBERS_DIR = "numbers"
DB_FILE = "bot_database.db"
os.makedirs(NUMBERS_DIR, exist_ok=True)

# API Config
API_TOKEN = os.getenv("API_TOKEN")
BASE_URL = "http://147.135.212.197/crapi/s1t"
OTP_GROUP_ID = "-1002774502308"
BACKUP = "https://t.me/TricksMastarNumbar"
CHANNEL_LINK = "https://t.me/TRICKSMASTEROTP2_bot"

# ==================== FORCE SUBSCRIPTION CONFIG ====================
# Format: {"channel_username": "invite_link"} ya {"channel_id": "invite_link"}
# Public channels ke liye: @channelname
# Private channels ke liye: -100xxxxxxxxx (chat_id)
REQUIRED_CHANNELS = {
    "@EARNINGTRICKSMASTER1": "https://t.me/EARNINGTRICKSMASTER1",
    "@TricksMastarNumbar": "https://t.me/TricksMastarNumbar",
    "@tricsmastarotp": "https://t.me/tricsmastarotp",
    "-1003468954182": "https://t.me/+0Yq3rwKnhgE2ZGJl"
   
   
}

# ==================== QUEUES ====================
group_queue = Queue(maxsize=1000)
personal_queue = Queue(maxsize=5000)
start_command_queue = Queue(maxsize=1000)  # New queue for /start commands
seen_messages = deque(maxlen=50000)

# ==================== REGEX PATTERNS (PRE-COMPILED) ====================
KEYWORD_REGEX = re.compile(r"(otp|code|pin|password|verify)[^\d]{0,10}(\d[\d\-]{3,8})", re.I)
REVERSE_REGEX = re.compile(r"(\d[\d\-]{3,8})[^\w]{0,10}(otp|code|pin|password|verify)", re.I)
GENERIC_REGEX = re.compile(r"\d{2,4}[-]?\d{2,4}")
UNICODE_CLEAN = re.compile(r"[\u200f\u200e\u202a-\u202e]")

# ==================== DATABASE SETUP ====================
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_numbers
                 (number TEXT PRIMARY KEY, chat_id INTEGER, country TEXT, assigned_at REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_stats
                 (chat_id INTEGER PRIMARY KEY, total_otps INTEGER DEFAULT 0, 
                  last_otp REAL, joined_at REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS message_cache
                 (msg_id TEXT PRIMARY KEY, created_at REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS past_otps_cache
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  number TEXT,
                  sender TEXT,
                  message TEXT,
                  otp TEXT,
                  timestamp TEXT,
                  received_at REAL)''')
    
    c.execute('''CREATE INDEX IF NOT EXISTS idx_number ON past_otps_cache(number)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_received_at ON past_otps_cache(received_at)''')
    
    conn.commit()
    conn.close()

init_db()

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
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM user_numbers WHERE number=?", (number,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_number_by_chat(chat_id):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT number FROM user_numbers WHERE chat_id=? ORDER BY assigned_at DESC LIMIT 1", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def assign_number(number, chat_id, country):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_numbers VALUES (?, ?, ?, ?)",
              (number, chat_id, country, time.time()))
    conn.commit()
    conn.close()

def increment_user_stats(chat_id):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("""INSERT INTO user_stats (chat_id, total_otps, last_otp, joined_at) 
                 VALUES (?, 1, ?, ?) 
                 ON CONFLICT(chat_id) DO UPDATE SET 
                 total_otps = total_otps + 1, last_otp = ?""",
              (chat_id, time.time(), time.time(), time.time()))
    conn.commit()
    conn.close()

def cache_past_otp(number, sender, message, otp, timestamp):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
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
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
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
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
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
    return "🚀 OTP Bot v2.1 Running"

@app.route("/health")
def health():
    return Response(f"OK - Queue: G={group_queue.qsize()} P={personal_queue.qsize()} S={start_command_queue.qsize()}", status=200)

# ==================== FORCE SUBSCRIPTION CHECK ====================
def check_channel_membership(user_id):
    """
    Check if user is member of all required channels
    Returns: (is_member: bool, not_joined: list)
    """
    not_joined = []
    
    for channel_id, invite_link in REQUIRED_CHANNELS.items():
        try:
            member = bot.get_chat_member(channel_id, user_id)
            if member.status not in ["member", "creator", "administrator"]:
                not_joined.append((channel_id, invite_link))
        except Exception as e:
            print(f"❌ Error checking {channel_id}: {e}", flush=True)
            not_joined.append((channel_id, invite_link))
    
    return len(not_joined) == 0, not_joined

def create_force_sub_keyboard(not_joined_channels):
    """Create inline keyboard for force subscription"""
    markup = types.InlineKeyboardMarkup()
    
    for i, (channel_id, invite_link) in enumerate(not_joined_channels, 1):
        # Display name based on channel type
        if channel_id.startswith("@"):
            display_name = f"Join {channel_id}"
        else:
            display_name = f"Join Channel {i}"
        
        markup.add(types.InlineKeyboardButton(display_name, url=invite_link))
    
    markup.add(types.InlineKeyboardButton("✅ I Joined, Verify", callback_data="verify_join"))
    return markup

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
            return "Unknown", "🌍"
        country_obj = pycountry.countries.get(alpha_2=region)
        if not country_obj:
            return "Unknown", "🌍"
        flag = "".join([chr(127397 + ord(c)) for c in region])
        return country_obj.name, flag
    except:
        return "Unknown", "🌍"

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

# ==================== THREAD 1: OTP SCRAPER ====================
def otp_scraper_thread():
    print("🟢 OTP Scraper Started", flush=True)
    
    while True:
        try:
            response = requests.get(
                f"{BASE_URL}/viewstats",
                params={
                    "token": API_TOKEN,
                    "dt1": "1970-01-01 00:00:00",
                    "dt2": "2099-12-31 23:59:59",
                    "records": 10
                },
                timeout=8
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
                        
                        try:
                            group_queue.put_nowait((record, time.time()))
                        except:
                            print("⚠️ Group queue full!", flush=True)
                        
                        chat_id = get_chat_by_number(number)
                        if chat_id:
                            try:
                                personal_queue.put_nowait((record, chat_id, time.time()))
                            except:
                                print(f"⚠️ Personal queue full for {chat_id}!", flush=True)
            
            time.sleep(0.3)
            
        except Exception as e:
            print(f"❌ Scraper error: {e}", flush=True)
            time.sleep(2)

# ==================== THREAD 2: GROUP SENDER ====================
def group_sender_thread():
    print("🟢 Group Sender Started", flush=True)

    while True:
        try:
            if group_queue.qsize() > 50:
                dropped = group_queue.qsize()
                with group_queue.mutex:
                    group_queue.queue.clear()
                print(f"⚠️ Flushed group queue (removed {dropped} old messages)", flush=True)

            record, fetch_time = group_queue.get()
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
                timeout=5
            )

            if response.status_code == 200:
                delay = time.time() - fetch_time
                print(f"✅ Group sent (delay: {delay:.2f}s)", flush=True)
            elif response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 2)
                print(f"⏳ Rate limited, waiting {retry_after}s", flush=True)
                time.sleep(retry_after)
                group_queue.put((record, fetch_time))
            else:
                print(f"❌ Group send failed: {response.status_code}", flush=True)

            time.sleep(0.5)

        except Exception as e:
            print(f"❌ Group sender error: {e}", flush=True)
            time.sleep(1)

# ==================== THREAD 3: PERSONAL DM SENDER ====================
def personal_sender_thread():
    print("🟢 Personal Sender Started", flush=True)

    while True:
        try:
            if personal_queue.qsize() > 100:
                dropped = personal_queue.qsize()
                with personal_queue.mutex:
                    personal_queue.queue.clear()
                print(f"⚠️ Flushed personal queue (removed {dropped} old messages)", flush=True)

            record, chat_id, fetch_time = personal_queue.get()
            msg = format_personal_message(record)

            payload = {
                "chat_id": chat_id,
                "text": msg[:4000],
                "parse_mode": "HTML"
            }

            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=5
            )

            if response.status_code == 200:
                increment_user_stats(chat_id)
                delay = time.time() - fetch_time
                print(f"✅ DM sent to {chat_id} (delay: {delay:.2f}s)", flush=True)
            elif response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 1)
                time.sleep(retry_after)
                personal_queue.put((record, chat_id, fetch_time))
            else:
                print(f"❌ DM failed for {chat_id}: {response.status_code}", flush=True)

            time.sleep(0.2)

        except Exception as e:
            print(f"❌ Personal sender error: {e}", flush=True)
            time.sleep(1)

# ==================== THREAD 4: START COMMAND HANDLER ====================
def start_command_handler_thread():
    """Separate thread to handle /start commands quickly"""
    print("🟢 Start Command Handler Started", flush=True)
    
    while True:
        try:
            message = start_command_queue.get()
            handle_start_command(message)
            time.sleep(0.1)  # Small delay between responses
        except Exception as e:
            print(f"❌ Start handler error: {e}", flush=True)
            time.sleep(1)

def handle_start_command(message):
    """Process /start command"""
    chat_id = message.chat.id
    
    if message.from_user.id == ADMIN_ID:
        bot.send_message(chat_id, "👋 Welcome Admin! Use /adminhelp")
        return
    
    active_users.add(chat_id)
    
    # Check channel membership
    is_member, not_joined = check_channel_membership(message.from_user.id)
    
    if not is_member:
        markup = create_force_sub_keyboard(not_joined)
        bot.send_message(
            chat_id,
            "❌ <b>Join Required Channels First!</b>\n\n"
            "Please join all channels below to use the bot:",
            reply_markup=markup
        )
        return
    
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
    
    msg = bot.send_message(
        chat_id,
        "🌍 <b>Select Country:</b>\n\n"
        "⚡️ Fast delivery\n"
        "🔒 Secure numbers\n"
        "♻️ Change anytime",
        reply_markup=markup
    )
    user_messages[chat_id] = msg

# ==================== ADMIN COMMANDS ====================
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

📤 <b>Upload .txt file</b> - Add numbers
/setcountry &lt;name&gt; - Set current country
/deletecountry &lt;name&gt; - Delete country
/cleannumbers &lt;name&gt; - Clear numbers
/listcountries - View all countries
/broadcast - Send message to all users
/stats - Bot statistics
/usercount - Total users
/clearcache - Clear past OTP cache
"""
    bot.reply_to(message, help_text)

@bot.message_handler(commands=["stats"])
def bot_stats(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM past_otps_cache")
    cache_count = c.fetchone()[0]
    conn.close()
    
    stats_text = f"""
📊 <b>Bot Statistics:</b>

👥 Active Users: {len(active_users)}
📥 Group Queue: {group_queue.qsize()}
📨 Personal Queue: {personal_queue.qsize()}
🚀 Start Queue: {start_command_queue.qsize()}
💾 Cached Messages: {len(seen_messages)}
💿 Past OTPs Cache: {cache_count}
🌍 Countries: {len(numbers_by_country)}
📞 Total Numbers: {sum(len(v) for v in numbers_by_country.values())}
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
        except Exception as e:
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
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute("DELETE FROM past_otps_cache")
    deleted = c.rowcount
    conn.commit()
    conn.close()
    
    bot.reply_to(message, f"✅ Cleared {deleted} cached OTPs")

# ==================== USER COMMANDS ====================
@bot.message_handler(commands=["start"])
def start(message):
    """Queue the /start command for processing"""
    try:
        start_command_queue.put_nowait(message)
        print(f"📥 /start queued for {message.chat.id}", flush=True)
    except:
        # If queue is full, process immediately
        handle_start_command(message)

@bot.message_handler(commands=["mystats"])
def my_stats(message):
    chat_id = message.chat.id
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
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
    help_text = """
📚 <b>Bot Commands:</b>

/start - Get a new number
/mystats - View your statistics
/help - Show this help message

<b>Features:</b>
• Instant OTP delivery
• View past OTPs
• Change number anytime
• Multiple countries
"""
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
        types.InlineKeyboardButton("🌍 Change Country", callback_data="change_country")
    )
    markup.row(
        types.InlineKeyboardButton("📜 View Past OTPs", callback_data=f"view_past_{number}")
    )
    markup.row(
        types.InlineKeyboardButton("📢 OTP Group", url=f"https://t.me/+uyZ-WBeLvbViNGZl")
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
    """Fetch and display past OTPs for a number from API (last 2000 SMS)"""
    
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
            bot.send_message(chat_id, f"📭 <b>No past OTPs found for:</b>\n<code>{number}</code>")
            return
        
        country_info, flag = country_from_number(number)
        
        msg_text = f"{flag} <b>Past OTPs for {number}</b>\n"
        msg_text += f"<b>Country:</b> {country_info}\n"
        msg_text += f"<b>Total Messages Found:</b> {len(user_messages_list)}\n"
        msg_text += "━━━━━━━━━━━━━━━━━━\n\n"
        
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
    except requests.RequestException as e:
        print(f"❌ Error fetching past OTPs: {e}", flush=True)
        bot.send_message(chat_id, "❌ Network error. Please try again later.")
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
            "🌍 Select Country:",
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
        is_member, not_joined = check_channel_membership(call.from_user.id)
        
        if not is_member:
            bot.answer_callback_query(call.id, "❌ Still not joined all channels!")
            markup = create_force_sub_keyboard(not_joined)
            try:
                bot.edit_message_reply_markup(
                    chat_id,
                    call.message.message_id,
                    reply_markup=markup
                )
            except:
                pass
        else:
            bot.answer_callback_query(call.id, "✅ Verified! Welcome!")
            # Re-trigger start command
            handle_start_command(call.message)

@bot.message_handler(commands=["setcountry", "deletecountry", "cleannumbers", "listcountries", "usercount"])
def other_admin_commands(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    cmd = message.text.split()[0][1:]
    
    if cmd == "listcountries":
        if not numbers_by_country:
            return bot.reply_to(message, "❌ No countries")
        text = "🌍 <b>Countries:</b>\n\n"
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
    print(f"🚀 OTP Bot v2.1 Starting at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📊 Initial stats: {len(numbers_by_country)} countries loaded", flush=True)
    print(f"🔒 Force Sub: {len(REQUIRED_CHANNELS)} channels configured", flush=True)
    
    # Start all threads
    threading.Thread(target=run_bot, daemon=True, name="BotPoller").start()
    threading.Thread(target=otp_scraper_thread, daemon=True, name="OTPScraper").start()
    threading.Thread(target=group_sender_thread, daemon=True, name="GroupSender").start()
    threading.Thread(target=personal_sender_thread, daemon=True, name="PersonalSender").start()
    threading.Thread(target=start_command_handler_thread, daemon=True, name="StartHandler").start()
    threading.Thread(target=cleanup_thread, daemon=True, name="Cleaner").start()
    
    print("✅ All threads started successfully!", flush=True)
    
    # Start Flask server
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
