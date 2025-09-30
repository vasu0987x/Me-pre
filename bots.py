import os
import time
import json
import random
import queue
import threading
import requests
import re
import unicodedata
import html
from datetime import datetime

import telebot
from telebot import types

from pymongo import MongoClient
import phonenumbers
import pycountry

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "6910226896:AAGwWdHzZXhLUUDwhjMDIYH7oOMApzLXTlc"
API_TOKEN = os.getenv("API_TOKEN") or "SFdYRDRSQkhTeG9nXGJsiolXjVRbVWhphldja0GUZ4N_goN7Q3Z4"
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://vasubot:vasubot@cluster0.fpchqfc.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

ADMIN_ID = int(os.getenv("ADMIN_ID", "6102951142"))

bot = telebot.TeleBot(BOT_TOKEN)

OTP_GROUP_ID = os.getenv("OTP_GROUP_ID", "-1002866392973")
BACKUP = "https://t.me/TricksMastarNumbar"
CHANNEL_LINK = "https://t.me/TRICKSMASTEROTP2_bot"

# ---------------- QUEUE / IN-MEM ----------------
message_queue = queue.Queue()
seen_messages = set()
temp_uploads = {}      # admin_id -> list of numbers (in-memory)
user_messages = {}     # chat_id -> telebot message object (in-memory)
last_change_time = {}  # chat_id -> timestamp (cooldown)
active_users = set()   # in-memory active users set

# ---------------- MONGO SETUP ----------------
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["otp_bot"]

countries_col = db["countries"]     # documents: { country: "India", numbers: [...] }
allocations_col = db["allocations"] # documents: { number: "91999...", chat_id: 12345, assigned_at: datetime, country: "India" }
users_col = db["users"]             # documents: { chat_id: 12345, number: "91999...", country: "India", last_assigned: datetime }
settings_col = db["settings"]       # single doc key-value: { _id: "meta", current_country: "India" }

# create useful indexes
countries_col.create_index("country", unique=True)
allocations_col.create_index("number", unique=True)
users_col.create_index("chat_id", unique=True)

# ---------------- HELPERS: DB OPS ----------------
def add_numbers_to_country(country: str, numbers: list[str]):
    nums = [n.strip() for n in numbers if n and n.strip()]
    if not nums:
        return 0
    res = countries_col.update_one(
        {"country": country},
        {"$addToSet": {"numbers": {"$each": nums}}},
        upsert=True
    )
    # Return number of numbers attempted to add (not exact newly inserted count)
    return len(nums)

def get_numbers_by_country(country: str) -> list:
    doc = countries_col.find_one({"country": country})
    return doc.get("numbers", []) if doc else []

def get_all_countries() -> list:
    docs = countries_col.find({}, {"country": 1}).sort("country", 1)
    return [d["country"] for d in docs]

def delete_country_db(country: str) -> bool:
    doc = countries_col.find_one_and_delete({"country": country})
    if doc:
        numbers = doc.get("numbers", [])
        if numbers:
            allocations_col.delete_many({"number": {"$in": numbers}})
        # clear setting if matches
        meta = settings_col.find_one({"_id": "meta"})
        if meta and meta.get("current_country") == country:
            settings_col.update_one({"_id":"meta"}, {"$unset":{"current_country":""}})
        return True
    return False

def clear_country_numbers_db(country: str) -> bool:
    doc = countries_col.find_one({"country": country})
    if not doc:
        return False
    numbers = doc.get("numbers", [])
    countries_col.update_one({"country": country}, {"$set": {"numbers": []}})
    if numbers:
        allocations_col.delete_many({"number": {"$in": numbers}})
    return True

def set_current_country(country: str):
    settings_col.update_one({"_id":"meta"}, {"$set":{"current_country":country}}, upsert=True)

def get_current_country():
    doc = settings_col.find_one({"_id":"meta"})
    return doc.get("current_country") if doc else None

def assign_number_to_user(number: str, chat_id: int, country: str | None = None):
    allocations_col.update_one(
        {"number": number},
        {"$set": {"chat_id": chat_id, "assigned_at": datetime.utcnow(), "country": country}},
        upsert=True
    )
    users_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"number": number, "country": country, "last_assigned": datetime.utcnow()}},
        upsert=True
    )

def find_allocation(number: str):
    return allocations_col.find_one({"number": number})

# ---------------- FLASK (health endpoint) ----------------
from flask import Flask, Response
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is running"

@app.route("/health")
def health():
    return Response("OK", status=200)

# ---------------- TELEGRAM SENDER (via requests) ----------------
def send_to_telegram(msg, chat_id=OTP_GROUP_ID, kb=None):
    payload = {
        "chat_id": chat_id,
        "text": msg[:3900],
        "parse_mode": "HTML"
    }
    if kb:
        try:
            payload["reply_markup"] = kb.to_json()
        except:
            # fallback: no reply_markup attached
            pass
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            time.sleep(1)
    return False

def sender_worker():
    while True:
        item = message_queue.get()
        if len(item) == 3:
            msg, chat_ids, kb = item
        else:
            msg, chat_ids = item
            kb = None
        for chat_id in chat_ids:
            send_to_telegram(msg, chat_id, kb)
        message_queue.task_done()
        time.sleep(0.15)

# ---------------- FORMATTING & UTIL ----------------
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
    if len(number) <= 4:
        return number
    mid = len(number)//2
    start = number[:mid-1]
    end = number[mid+1:]
    return start + "**" + end

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
    except Exception:
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

    return formatted, kb, number

# ---------------- MAIN LOOP: fetch OTPs and route ----------------
BASE_URL = os.getenv("BASE_URL", "http://147.135.212.197/crapi/s1t")

def safe_request(url, params):
    try:
        response = requests.get(url, params=params, timeout=15)
        return response.json()
    except Exception:
        return None

def main_loop():
    print("🚀 OTP Monitor Started...")
    while True:
        stats = safe_request(f"{BASE_URL}/viewstats", {
            "token": API_TOKEN,
            "dt1":"1970-01-01 00:00:00",
            "dt2":"2099-12-31 23:59:59",
            "records":10
        }) or {}

        if stats.get("status") == "success":
            for record in stats.get("data", []):
                uid = f"{record.get('dt')}_{record.get('num')}_{record.get('message')}"
                if uid in seen_messages:
                    continue
                seen_messages.add(uid)

                # Group message
                msg_group, kb, num = format_message(record, personal=False)
                message_queue.put((msg_group, [OTP_GROUP_ID], kb))

                # Personal message if allocation exists
                alloc = find_allocation(num)
                if alloc and alloc.get("chat_id"):
                    msg_personal, kb2, _ = format_message(record, personal=True)
                    message_queue.put((msg_personal, [alloc["chat_id"]], None))

        time.sleep(0.15)

# ---------------- BOT HANDLERS ----------------
@bot.message_handler(content_types=["document"])
def handle_document(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if not message.document.file_name.lower().endswith(".txt"):
        return bot.reply_to(message, "❌ Please upload a .txt file.")

    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    numbers = [line.strip() for line in downloaded_file.decode("utf-8", errors="ignore").splitlines() if line.strip()]

    if not numbers:
        return bot.reply_to(message, "❌ File is empty.")

    temp_uploads[message.from_user.id] = numbers

    # build country buttons from DB
    countries = get_all_countries()
    markup = types.InlineKeyboardMarkup()
    for country in countries:
        markup.add(types.InlineKeyboardButton(country, callback_data=f"addto_{country}"))
    markup.add(types.InlineKeyboardButton("➕ New Country", callback_data="addto_new"))

    bot.reply_to(message, "📂 File received. Select country to add numbers:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("addto_"))
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
        added = add_numbers_to_country(choice, numbers)
        bot.edit_message_text(f"✅ Added {added} numbers to *{choice}*",
                              call.message.chat.id, call.message.message_id, parse_mode="Markdown")
        temp_uploads.pop(call.from_user.id, None)

def save_new_country(message, numbers):
    country = message.text.strip()
    if not country:
        return bot.reply_to(message, "❌ Invalid country name.")
    added = add_numbers_to_country(country, numbers)
    set_current_country(country)
    bot.reply_to(message, f"✅ Saved {added} numbers under *{country}*", parse_mode="Markdown")
    temp_uploads.pop(message.from_user.id, None)

def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 403:  # user blocked bot
            print(f"❌ User {chat_id} blocked the bot. Skipping.")
            active_users.discard(chat_id)
            users_col.update_one({"chat_id": chat_id}, {"$set": {"blocked": True}})
            return None
        else:
            raise  # baaki errors same throw hone dena


@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id

    if message.from_user.id == ADMIN_ID:
        safe_send(chat_id, "👋 Welcome Admin!\nUse /adminhelp for commands.")
        return

    active_users.add(chat_id)

    REQUIRED_CHANNELS = os.getenv("REQUIRED_CHANNELS", "@EARNINGTRICKSMASTER1,@day1chennel").split(",")
    not_joined = []
    for channel in REQUIRED_CHANNELS:
        channel = channel.strip()
        if not channel:
            continue
        try:
            member = bot.get_chat_member(channel, chat_id)
            if member.status not in ["member", "creator", "administrator"]:
                not_joined.append(channel)
        except Exception:
            not_joined.append(channel)

    if not_joined:
        markup = types.InlineKeyboardMarkup()
        for ch in not_joined:
            markup.add(types.InlineKeyboardButton(f"🚀 Join {ch}", url=f"https://t.me/{ch.lstrip('@')}"))
        safe_send(chat_id, "❌ You must join all required channels to use the bot.", reply_markup=markup)
        return

    countries = get_all_countries()
    if not countries:
        safe_send(chat_id, "❌ No countries available yet.")
        return

    markup = types.InlineKeyboardMarkup()
    for country in countries:
        markup.add(types.InlineKeyboardButton(country, callback_data=f"user_select_{country}"))
    msg = safe_send(chat_id, "🌍 Choose a country:", reply_markup=markup)
    if msg:
        user_messages[chat_id] = msg


# single callback handler for user actions
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    data_str = call.data

    if data_str.startswith("user_select_"):
        country = data_str[12:]
        # store user's selected country in users_col
        users_col.update_one({"chat_id": chat_id}, {"$set": {"country": country}}, upsert=True)
        send_random_number(chat_id, country, edit=True)
    elif data_str == "change_number":
        # get user's country from DB
        u = users_col.find_one({"chat_id": chat_id})
        country = u.get("country") if u else None
        send_random_number(chat_id, country, edit=True)
    elif data_str == "change_country":
        countries = get_all_countries()
        markup = types.InlineKeyboardMarkup()
        for country in countries:
            markup.add(types.InlineKeyboardButton(country, callback_data=f"user_select_{country}"))
        if chat_id in user_messages:
            bot.edit_message_text("🌍 Select a country:", chat_id, user_messages[chat_id].message_id, reply_markup=markup)

@bot.message_handler(commands=["broadcast"])
def broadcast_start(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    msg = bot.reply_to(message, "✉️ Send the message you want to broadcast to all users:")
    bot.register_next_step_handler(msg, broadcast_message)

def broadcast_message(message):
    text = message.text
    success_count = 0
    fail_count = 0

    for user_id in list(active_users):
        try:
            bot.send_message(user_id, f"📢 Broadcast Message:\n\n{text}")
            success_count += 1
        except Exception:
            fail_count += 1
        time.sleep(0.1)

    bot.reply_to(message, f"✅ Broadcast sent!\nSuccess: {success_count}\nFailed: {fail_count}")

# ---------------- Admin commands (DB-backed) ----------------
@bot.message_handler(commands=["setcountry"])
def set_country(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if len(message.text.split()) > 1:
        country = " ".join(message.text.split()[1:]).strip()
        # ensure exists
        countries_col.update_one({"country": country}, {"$setOnInsert": {"numbers": []}}, upsert=True)
        set_current_country(country)
        bot.reply_to(message, f"✅ Current country set to: {country}")
    else:
        bot.reply_to(message, "Usage: /setcountry <country name>")

@bot.message_handler(commands=["deletecountry"])
def delete_country(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    if len(message.text.split()) > 1:
        country = " ".join(message.text.split()[1:]).strip()
        if delete_country_db(country):
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
        if clear_country_numbers_db(country):
            bot.reply_to(message, f"✅ Cleared numbers for {country}.")
        else:
            bot.reply_to(message, f"❌ Country '{country}' not found.")
    else:
        bot.reply_to(message, "Usage: /cleannumbers <country name>")

@bot.message_handler(commands=["listcountries"])
def list_countries(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    docs = list(countries_col.find({}))
    if not docs:
        return bot.reply_to(message, "❌ No countries available.")
    text = "🌍 Available countries and number counts:\n"
    for d in sorted(docs, key=lambda x: x.get("country","")):
        nums = d.get("numbers", [])
        text += f"- {d.get('country')}: {len(nums)} numbers\n"
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
- /usercount : Show users
"""
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=["usercount"])
def user_count(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not the admin.")
    count = len(active_users)
    bot.reply_to(message, f"👥 Total users using the bot: {count}")

# ---------------- Number assignment & sending (user flow) ----------------
def send_random_number(chat_id, country=None, edit=False):
    now = time.time()
    cooldown = 10
    if chat_id in last_change_time and now - last_change_time[chat_id] < cooldown:
        wait = cooldown - int(now - last_change_time[chat_id])
        if chat_id in user_messages:
            old_msg = user_messages[chat_id].text
            if "⏳ Please wait" in old_msg:
                new_text = re.sub(r"⏳ Please wait.*", f"⏳ Please wait {wait} sec before changing number again.", old_msg)
            else:
                new_text = old_msg + f"\n\n⏳ Please wait {wait} sec before changing number again."
            try:
                bot.edit_message_text(
                    new_text,
                    chat_id,
                    user_messages[chat_id].message_id,
                    reply_markup=user_messages[chat_id].reply_markup,
                    parse_mode="Markdown"
                )
            except Exception:
                try:
                    bot.send_message(chat_id, f"⏳ Please wait {wait} sec before changing number again.")
                except:
                    pass
        else:
            try:
                bot.send_message(chat_id, f"⏳ Please wait {wait} sec before changing number again.")
            except:
                pass
        return

    last_change_time[chat_id] = now

    if country is None:
        u = users_col.find_one({"chat_id": chat_id})
        country = u.get("country") if u else None
        if not country:
            bot.send_message(chat_id, "❌ No country selected.")
            return

    numbers = get_numbers_by_country(country)
    if not numbers:
        bot.send_message(chat_id, f"❌ No numbers for {country}.")
        return

    number = random.choice(numbers)
    # assign in DB
    assign_number_to_user(number, chat_id, country)

    # Message text
    text = f"📞 Number for *{country}*:\n`{number}`\n\n⏳ Waiting For OTP...📱"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔄 Change Number", callback_data="change_number"))
    markup.add(types.InlineKeyboardButton("🌍 Change Country", callback_data="change_country"))

    if chat_id in user_messages and edit:
        try:
            bot.edit_message_text(
                text,
                chat_id,
                user_messages[chat_id].message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
            user_messages[chat_id].text = text
            user_messages[chat_id].reply_markup = markup
        except Exception:
            # fallback: send new message
            msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
            user_messages[chat_id] = msg
    else:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        user_messages[chat_id] = msg

# ---------------- STARTUP ----------------
def run_bot():
    bot.infinity_polling()

def start_background_tasks():
    threading.Thread(target=run_bot, daemon=True).start()
    threading.Thread(target=sender_worker, daemon=True).start()
    threading.Thread(target=main_loop, daemon=True).start()

if __name__ == "__main__":
    start_background_tasks()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
