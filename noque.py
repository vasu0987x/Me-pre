import telebot
from telebot import types
import json
import os
import random
from flask import Flask, Response
import threading
import requests
import re
import unicodedata
import html
import phonenumbers
import pycountry
import time

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 6483088050
bot = telebot.TeleBot(BOT_TOKEN)

DATA_FILE = "bot_data.json"
NUMBERS_DIR = "numbers"
os.makedirs(NUMBERS_DIR, exist_ok=True)

# OTP API Config
API_TOKEN = os.getenv("API_TOKEN")
BASE_URL = "http://147.135.212.197/crapi/s1t"
OTP_GROUP_ID = "-1002784314709"
BACKUP = "https://t.me/TricksMastarNumbar"
CHANNEL_LINK = "https://t.me/TRICKSMASTEROTP2_bot"

# ---------------- DATA STORAGE ----------------
data = {}
numbers_by_country = {}
current_country = None
user_messages = {}         # chat_id -> message object
user_current_country = {}  # chat_id -> selected country
temp_uploads = {}          # admin_id -> list of numbers
user_numbers = {}          # number -> chat_id
seen_messages = set()

# ---------------- DATA FUNCTIONS ----------------
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

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is running"

@app.route("/health")
def health():
    return Response("OK", status=200)

# ---------------- TELEGRAM SENDER ----------------
def send_to_telegram(msg, chat_id=OTP_GROUP_ID, kb=None):
    payload = {
        "chat_id": chat_id,
        "text": msg[:3900],
        "parse_mode": "HTML"
    }
    if kb:
        payload["reply_markup"] = kb.to_json()
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload, timeout=10)
            if r.status_code == 200:
                return True
            elif r.status_code == 429:
                wait_time = 1.5 * (2 ** attempt)  # 1.5s, 3s, 6s
                print(f"429 Rate limit hit for chat {chat_id}. Waiting {wait_time}s...", flush=True)
                time.sleep(wait_time)
        except Exception as e:
            print(f"Send error: {e}, attempt {attempt + 1}/3", flush=True)
            time.sleep(0.2)
    print(f"Failed to send to {chat_id} after 3 attempts", flush=True)
    return False

# ---------------- ADMIN FILE UPLOAD ----------------
@bot.message_handler(content_types=["document"])
def handle_document(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if not message.document.file_name.endswith(".txt"):
        return bot.reply_to(message, "❌ Please upload a .txt file.")

    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    numbers = [line.strip() for line in downloaded_file.decode("utf-8").splitlines() if line.strip()]

    if not numbers:
        return bot.reply_to(message, "❌ File is empty.")

    temp_uploads[message.from_user.id] = numbers

    markup = types.InlineKeyboardMarkup()
    for country in sorted(numbers_by_country.keys()):
        markup.add(types.InlineKeyboardButton(country, callback_data=f"addto_{country}"))
    markup.add(types.InlineKeyboardButton("➕ New Country", callback_data="addto_new"))

    bot.reply_to(message, "📂 File received. Select country to add numbers:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("addto_"))
def callback_addto(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "❌ Not authorized")
    numbers = temp_uploads.get(call.from_user.id, [])
    if not numbers:
        return bot.answer_callback_query(call.id, "❌ No uploaded numbers found")

    choice = call.data[6:]
    if choice == "new":
        bot.send_message(call.message.chat.id, "✍️ Send new country name:")
        bot.register_next_step_handler(call.message, save_new_country, numbers)
    else:
        existing = numbers_by_country.get(choice, [])
        merged = list(set(existing + numbers))
        numbers_by_country[choice] = merged
        save_data()
        file_path = os.path.join(NUMBERS_DIR, f"{choice}.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(merged))
        bot.edit_message_text(f"✅ Added {len(numbers)} numbers to *{choice}*",
                              call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        temp_uploads.pop(call.from_user.id, None)

def save_new_country(message, numbers):
    country = message.text.strip()
    if not country:
        return bot.reply_to(message, "❌ Invalid country name.")
    numbers_by_country[country] = numbers
    save_data()
    file_path = os.path.join(NUMBERS_DIR, f"{country}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(numbers))
    bot.reply_to(message, f"✅ Saved {len(numbers)} numbers under *{country}*", parse_mode="Markdown")
    temp_uploads.pop(message.from_user.id, None)

# ---------------- OTP FETCHER ----------------
def safe_request(url, params):
    try:
        response = requests.get(url, params=params, timeout=15)
        return response.json()
    except:
        return None

def extract_otp(message: str) -> str | None:
    message = unicodedata.normalize("NFKD", message)
    message = re.sub(r"[\u200f\u200e\u202a-\u202e]", "", message)
    keyword_regex = re.search(r"(otp|code|pin|password)[^\d]{0,10}(\d[\d\-]{3,8})", message, re.I)
    if keyword_regex:
        return re.sub(r"\D", "", keyword_regex.group(2))
    reverse_regex = re.search(r"(\d[\d\-]{3,8})[^\w]{0,10}(otp|code|pin|password)", message, re.I)
    if reverse_regex:
        return re.sub(r"\D", "", reverse_regex.group(1))
    generic_regex = re.findall(r"\d{2,4}[-]?\d{2,4}", message)
    if generic_regex:
        return re.sub(r"\D", "", generic_regex[0])
    return None

def mask_number(number: str) -> str:
    if len(number) <= 4: return number
    mid = len(number)//2
    start = number[:mid-1]
    end = number[mid+1:]
    return start + "**" + end

def country_from_number(number: str) -> tuple[str, str]:
    try:
        parsed = phonenumbers.parse("+" + number)
        region = phonenumbers.region_code_for_number(parsed)
        if not region: return "Unknown", "🌍"
        country_obj = pycountry.countries.get(alpha_2=region)
        if not country_obj: return "Unknown", "🌍"
        flag = "".join([chr(127397 + ord(c)) for c in region])
        return country_obj.name, flag
    except:
        return "Unknown", "🌍"

def format_message(record, personal=False):
    number = record.get("num") or "Unknown"
    sender = record.get("cli") or "Unknown"
    message = record.get("message") or ""
    dt = record.get("dt") or ""
    payout = record.get("payout", "0")
    country, flag = country_from_number(number)
    otp = extract_otp(message)
    otp_line = f"<b>OTP:</b> <code>{html.escape(otp)}</code>\n" if otp else ""
    receive_time = time.time()

    kb = None
    if personal:
        formatted = (
            f"📲 <b>Your OTP Received</b>\n"
            f"<b>Number:</b> {number}\n"
            f"<b>Service:</b> {sender}\n"
            f"{otp_line}"
            f"<b>Full Message:</b>\n<code>{html.escape(message)}</code>"
        )
    else:
        formatted = (
            f"<blockquote>{flag} <b>New {sender} OTP Received</b></blockquote>\n"
            f"<blockquote><b>Time:</b> {dt}</blockquote>\n"
            f"<blockquote><b>Country:</b> {country} {flag}</blockquote>\n"
            f"<blockquote><b>Service:</b> {sender}</blockquote>\n"
            f"<blockquote><b>Number:</b> {mask_number(number)}</blockquote>\n"
            f"<blockquote>{otp_line}</blockquote>"
            f"<blockquote><b>Full Message:</b></blockquote>\n"
            f"<blockquote><code>{html.escape(message)}</code></blockquote>"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("🚀 Panel", url=CHANNEL_LINK),
            types.InlineKeyboardButton("📢 Channel", url=BACKUP)
        )

    return formatted, kb, number, receive_time

def broadcast_message(message):
    text = message.text
    success_count = 0
    fail_count = 0
    for user_id in active_users:
        try:
            bot.send_message(user_id, f"📢 Broadcast Message:\n\n{text}")
            success_count += 1
            time.sleep(0.1)
        except:
            fail_count += 1
    bot.reply_to(message, f"✅ Broadcast sent!\nSuccess: {success_count}\nFailed: {fail_count}")

def main_loop():
    print("🚀 OTP Monitor Started...")
    while True:
        stats = safe_request(f"{BASE_URL}/viewstats", {
            "token": API_TOKEN,
            "dt1": "1970-01-01 00:00:00",
            "dt2": "2099-12-31 23:59:59",
            "records": 100  # Increased for bursts
        }) or {}
        if stats.get("status") == "success":
            for record in stats["data"]:
                uid = f"{record.get('dt')}_{record.get('num')}_{record.get('message')}"
                if uid in seen_messages:
                    continue
                seen_messages.add(uid)
                if len(seen_messages) > 100000:
                    seen_messages.clear()
                    print("Cleared seen_messages to free memory", flush=True)
                number = record.get("num")
                print(f"Received OTP for {number} at {time.time()}", flush=True)
                msg_group, kb, _, receive_time = format_message(record, personal=False)
                success = send_to_telegram(msg_group, OTP_GROUP_ID, kb)
                if success:
                    print(f"Sent group msg to {OTP_GROUP_ID} at {time.time()}, delay: {time.time() - receive_time}s", flush=True)
                time.sleep(1.1)  # Respect group rate limit
                chat_id = user_numbers.get(number)
                if chat_id:
                    msg_personal, _, _, receive_time = format_message(record, personal=True)
                    success = send_to_telegram(msg_personal, chat_id)
                    if success:
                        print(f"Sent personal msg to {chat_id} at {time.time()}, delay: {time.time() - receive_time}s", flush=True)
                    time.sleep(0.1)  # Faster for personal chats
        time.sleep(0.5)  # Poll API every 0.5 seconds

# ---------------- USER BOT FUNCTIONS ----------------
last_change_time = {}
active_users = set()
REQUIRED_CHANNELS = ["@EARNINGTRICKSMASTER1", "@day1chennel"]

@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id
    if message.from_user.id == ADMIN_ID:
        bot.send_message(chat_id, "👋 Welcome Admin!\nUse /adminhelp for commands.")
        return

    active_users.add(chat_id)
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
            markup.add(types.InlineKeyboardButton(f"🚀 Join {ch}", url=f"https://t.me/{ch[1:]}"))
        bot.send_message(chat_id, "❌ You must join all required channels to use the bot.", reply_markup=markup)
        return

    if not numbers_by_country:
        bot.send_message(chat_id, "❌ No countries available yet.")
        return

    markup = types.InlineKeyboardMarkup()
    for country in sorted(numbers_by_country.keys()):
        markup.add(types.InlineKeyboardButton(country, callback_data=f"user_select_{country}"))
    msg = bot.send_message(chat_id, "🌍 Choose a country:", reply_markup=markup)
    user_messages[chat_id] = msg

@bot.message_handler(commands=["broadcast"])
def broadcast_start(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    msg = bot.reply_to(message, "✉️ Send the message you want to broadcast to all users:")
    bot.register_next_step_handler(msg, broadcast_message)

@bot.message_handler(commands=["queuestatus"])
def queue_status(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    bot.reply_to(message, "📥 No queues in use (direct sending enabled).")

def send_random_number(chat_id, country=None, edit=False):
    now = time.time()
    if chat_id in last_change_time and now - last_change_time[chat_id] < 10:
        wait = 10 - int(now - last_change_time[chat_id])
        if chat_id in user_messages:
            old_msg = user_messages[chat_id].text
            if "⏳ Please wait" in old_msg:
                new_text = re.sub(r"⏳ Please wait.*", f"⏳ Please wait {wait} sec before changing number again.", old_msg)
            else:
                new_text = old_msg + f"\n\n⏳ Please wait {wait} sec before changing number again."
            bot.edit_message_text(
                new_text,
                chat_id,
                user_messages[chat_id].message_id,
                reply_markup=user_messages[chat_id].reply_markup,
                parse_mode="Markdown"
            )
        else:
            bot.send_message(chat_id, f"⏳ Please wait {wait} sec before changing number again.")
        return

    last_change_time[chat_id] = now
    if country is None:
        country = user_current_country.get(chat_id)
        if not country:
            bot.send_message(chat_id, "❌ No country selected.")
            return

    numbers = numbers_by_country.get(country, [])
    if not numbers:
        bot.send_message(chat_id, f"❌ No numbers for {country}.")
        return

    number = random.choice(numbers)
    user_current_country[chat_id] = country
    user_numbers[number] = chat_id

    text = f"📞 Number for *{country}*:\n`{number}`\n\n⏳ Waiting For OTP...📱"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔄 Change Number", callback_data="change_number"))
    markup.add(types.InlineKeyboardButton("🌍 Change Country", callback_data="change_country"))

    if chat_id in user_messages:
        bot.edit_message_text(
            text,
            chat_id,
            user_messages[chat_id].message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        user_messages[chat_id].text = text
        user_messages[chat_id].reply_markup = markup
    else:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        user_messages[chat_id] = msg

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    if call.from_user.id != ADMIN_ID:
        active_users.add(chat_id)
    data_str = call.data
    if data_str.startswith("user_select_"):
        country = data_str[12:]
        user_current_country[chat_id] = country
        send_random_number(chat_id, country, edit=True)
    elif data_str == "change_number":
        send_random_number(chat_id, user_current_country.get(chat_id), edit=True)
    elif data_str == "change_country":
        markup = types.InlineKeyboardMarkup()
        for country in sorted(numbers_by_country.keys()):
            markup.add(types.InlineKeyboardButton(country, callback_data=f"user_select_{country}"))
        if chat_id in user_messages:
            bot.edit_message_text("🌍 Select a country:", chat_id, user_messages[chat_id].message_id, reply_markup=markup)

@bot.message_handler(commands=["usercount"])
def user_count(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    count = len(active_users)
    bot.reply_to(message, f"👥 Total users using the bot: {count}")

@bot.message_handler(commands=["setcountry"])
def set_country(message):
    global current_country
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if len(message.text.split()) > 1:
        current_country = " ".join(message.text.split()[1:]).strip()
        if current_country not in numbers_by_country:
            numbers_by_country[current_country] = []
        save_data()
        bot.reply_to(message, f"✅ Current country set to: {current_country}")
    else:
        bot.reply_to(message, "Usage: /setcountry <country name>")

@bot.message_handler(commands=["deletecountry"])
def delete_country(message):
    global current_country
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if len(message.text.split()) > 1:
        country = " ".join(message.text.split()[1:]).strip()
        if country in numbers_by_country:
            del numbers_by_country[country]
            if current_country == country:
                current_country = None
            file_path = os.path.join(NUMBERS_DIR, f"{country}.txt")
            if os.path.exists(file_path):
                os.remove(file_path)
            save_data()
            bot.reply_to(message, f"✅ Deleted country: {country}")
        else:
            bot.reply_to(message, f"❌ Country '{country}' not found.")
    else:
        bot.reply_to(message, "Usage: /deletecountry <country name>")

@bot.message_handler(commands=["cleannumbers"])
def clear_numbers(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if len(message.text.split()) > 1:
        country = " ".join(message.text.split()[1:]).strip()
        if country in numbers_by_country:
            numbers_by_country[country] = []
            file_path = os.path.join(NUMBERS_DIR, f"{country}.txt")
            open(file_path, "w").close()
            save_data()
            bot.reply_to(message, f"✅ Cleared numbers for {country}.")
        else:
            bot.reply_to(message, f"❌ Country '{country}' not found.")
    else:
        bot.reply_to(message, "Usage: /cleannumbers <country name>")

@bot.message_handler(commands=["listcountries"])
def list_countries(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if not numbers_by_country:
        return bot.reply_to(message, "❌ No countries available.")
    text = "🌍 Available countries and number counts:\n"
    for country, nums in sorted(numbers_by_country.items()):
        text += f"- {country}: {len(nums)} numbers\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=["adminhelp"])
def admin_help(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    help_text = """
🔧 *Admin Commands*:
- /setcountry <country>: Set current country for uploading `.txt`.
- Upload `.txt`: Add numbers (bot will ask country).
- /deletecountry <country>: Delete a country and its numbers.
- /cleannumbers <country>: Clear numbers for a country (keep country).
- /listcountries: View all countries and number counts.
- /adminhelp: Show this help.
- /usercount: Show users
- /queuestatus: Show queue status (direct sending enabled)
"""
    bot.reply_to(message, help_text, parse_mode="Markdown")

# ---------------- START BOTH ----------------
def run_bot():
    while True:
        try:
            bot.infinity_polling()
        except Exception as e:
            print(f"Polling error: {e}", flush=True)
            time.sleep(5)

def start_background_tasks():
    threading.Thread(target=run_bot, daemon=True).start()
    threading.Thread(target=main_loop, daemon=True).start()

if __name__ == "__main__":
    print(f"Bot starting at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", flush=True)
    start_background_tasks()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
