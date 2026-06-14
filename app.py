import os
import asyncio
import threading
import logging
import sys
import secrets
import json
import re
from datetime import datetime
from flask import Flask
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.helpers import create_deep_linked_url
from telegram.error import RetryAfter
from pymongo import MongoClient
from telegraph import Telegraph
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "File Share Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI environment variable not set!")
    sys.exit(1)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_share_bot_v2"]
file_store_collection = db["file_store"]
users_collection = db["users"]
stats_collection = db["stats"]
blocked_collection = db["blocked_users"]

def init_stats():
    if stats_collection.count_documents({"_id": "total_requests"}) == 0:
        stats_collection.insert_one({"_id": "total_requests", "count": 0})
init_stats()

def get_total_requests():
    doc = stats_collection.find_one({"_id": "total_requests"})
    return doc["count"] if doc else 0

def increment_requests():
    stats_collection.update_one({"_id": "total_requests"}, {"$inc": {"count": 1}}, upsert=True)

def add_user(user_id):
    if not users_collection.find_one({"user_id": user_id}):
        users_collection.insert_one({"user_id": user_id, "first_seen": datetime.now(), "attempts": 0})

def get_all_users():
    return [doc["user_id"] for doc in users_collection.find({}, {"user_id": 1})]

def save_file_info(payload, file_id, file_name):
    file_store_collection.update_one(
        {"payload": payload},
        {"$set": {"file_id": file_id, "file_name": file_name}},
        upsert=True
    )
    logger.info(f"Saved: payload={payload}, file_id={file_id}, name={file_name}")

def get_file_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    if doc:
        return {"file_id": doc["file_id"], "file_name": doc["file_name"]}
    return None

def is_user_blocked(user_id: int) -> bool:
    return blocked_collection.find_one({"user_id": user_id}) is not None

def block_user(user_id: int):
    if not is_user_blocked(user_id):
        blocked_collection.insert_one({"user_id": user_id, "blocked_at": datetime.now()})

def unblock_user(user_id: int):
    blocked_collection.delete_one({"user_id": user_id})

def get_blocked_users():
    return [doc["user_id"] for doc in blocked_collection.find({}, {"user_id": 1})]

def get_attempt_count(user_id: int) -> int:
    doc = users_collection.find_one({"user_id": user_id})
    return doc.get("attempts", 0) if doc else 0

def increment_attempts(user_id: int):
    users_collection.update_one(
        {"user_id": user_id},
        {"$inc": {"attempts": 1}},
        upsert=True
    )

def reset_attempts(user_id: int):
    users_collection.update_one({"user_id": user_id}, {"$set": {"attempts": 0}}, upsert=True)

# ---------- Telegram Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_TOKEN not set")
    sys.exit(1)

BOT_USERNAME = os.environ.get("BOT_USERNAME")
if not BOT_USERNAME:
    logger.error("BOT_USERNAME not set! Deep links will not work.")

ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

REQUIRED_CHANNELS = [
    {"id": "-1003753299714", "name": "🎬 ဇာတ်ကားချန်နယ် (ပင်မ)", "invite": "https://t.me/wznmoviescollector"},
    {"id": "-1003899625672", "name": "🎬 ဇာတ်ကားချန်နယ် (အရံ)", "invite": "https://t.me/moviesandseriesforallwzn"},
    {"id": "-1003792838735", "name": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ်", "invite": "https://t.me/everyboyhobby"},
    {"id": "-1003785717514", "name": "🎵 မြန်မာသီချင်းချန်နယ်", "invite": "https://t.me/wznmusiclibary"}
]

POST_CHANNELS = []
OTHER_CHANNELS = []
MUSIC_CHANNEL_LINK = ""

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def generate_payload():
    return secrets.token_urlsafe(16)

async def is_member_of_channel(user_id: int, channel_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def check_all_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    missing = []
    for ch in REQUIRED_CHANNELS:
        if not await is_member_of_channel(user_id, ch["id"], context):
            missing.append(ch)
    return (len(missing) == 0, missing)

# ---------- Telegraph ----------
telegraph = Telegraph()
try:
    telegraph.create_account(short_name=BOT_USERNAME or 'FileShareBot')
except:
    pass

async def create_telegraph_page_movie(title, content_text):
    try:
        html_content = content_text.replace('\n', '<br>')
        response = await asyncio.to_thread(
            telegraph.create_page,
            title=title,
            html_content=f"<p>{html_content}</p>",
            author_name="ရုပ်ရှင်အချက်အလက်"
        )
        return response['url']
    except Exception as e:
        logger.error(f"Telegraph error: {e}")
        return None

# ---------- Translation ----------
translator_en_to_my = GoogleTranslator(source='en', target='my')
translator_my_to_en = GoogleTranslator(source='my', target='en')

def translate_text(text, source='en', target='my'):
    if not text or text == 'N/A':
        return text
    try:
        if source == 'en' and target == 'my':
            return translator_en_to_my.translate(text)
        elif source == 'my' and target == 'en':
            return translator_my_to_en.translate(text)
        else:
            return GoogleTranslator(source=source, target=target).translate(text)
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text

def contains_burmese(text):
    return bool(re.search(r'[\u1000-\u109F]', text))

def normalize_movie_name(text):
    if contains_burmese(text):
        try:
            english = translator_my_to_en.translate(text)
            logger.info(f"Translated '{text}' -> '{english}'")
            return english.strip()
        except Exception as e:
            logger.error(f"Translation failed: {e}")
            return text
    return text

# ---------- Movie Info ----------
OMDB_API_KEY = "5025f95c"

def parse_movie_name_and_year(input_str):
    input_str = normalize_movie_name(input_str)
    year_match = re.search(r'[\(\[]?(\d{4})[\)\]]?', input_str)
    if year_match:
        year = year_match.group(1)
        name = re.sub(r'[\(\[]?\d{4}[\)\]]?\s*$', '', input_str).strip()
        return name, year
    return input_str, None

def get_movie_info(movie_input):
    name, year = parse_movie_name_and_year(movie_input)
    params = {'t': name, 'apikey': OMDB_API_KEY, 'plot': 'full'}
    if year:
        params['y'] = year
    try:
        response = requests.get("http://www.omdbapi.com/", params=params, timeout=10)
        data = response.json()
        if data.get('Response') == 'False':
            if year:
                params.pop('y')
                response = requests.get("http://www.omdbapi.com/", params=params, timeout=10)
                data = response.json()
                if data.get('Response') == 'False':
                    return None
            else:
                return None
        plot_en = data.get('Plot', 'N/A')
        plot_my = translate_text(plot_en) if len(plot_en) < 5000 else plot_en
        runtime_raw = data.get('Runtime', 'N/A')
        runtime_my = runtime_raw
        if runtime_raw != 'N/A' and 'min' in runtime_raw:
            try:
                minutes = int(runtime_raw.split()[0])
                hours = minutes // 60
                mins = minutes % 60
                if hours > 0:
                    runtime_my = f"{hours} နာရီ {mins} မိနစ်"
                else:
                    runtime_my = f"{mins} မိနစ်"
            except:
                runtime_my = runtime_raw
        return {
            'title': data.get('Title', 'N/A'),
            'year': data.get('Year', 'N/A'),
            'genre': translate_text(data.get('Genre', 'N/A')),
            'actors': translate_text(data.get('Actors', 'N/A')),
            'director': translate_text(data.get('Director', 'N/A')),
            'runtime': runtime_my,
            'country': translate_text(data.get('Country', 'N/A')),
            'language': translate_text(data.get('Language', 'N/A')),
            'imdb_rating': data.get('imdbRating', 'N/A'),
            'imdb_votes': data.get('imdbVotes', 'N/A'),
            'plot': plot_my,
            'poster': data.get('Poster', 'N/A'),
            'imdb_id': data.get('imdbID', 'N/A'),
        }
    except Exception as e:
        logger.error(f"OMDb error: {e}")
        return None

# Plain text formatting (no markdown errors)
def format_movie_info_plain(movie):
    text = f"""🎬 {movie['title']} ({movie['year']})

📌 အမျိုးအစား – {movie['genre']}
🎭 သရုပ်ဆောင်များ – {movie['actors']}
🎥 ဒါရိုက်တာ – {movie['director']}
⏱️ ကြာချိန် – {movie['runtime']}
🌍 နိုင်ငံ – {movie['country']}
🗣️ ဘာသာစကား – {movie['language']}
⭐ IMDb အဆင့်သတ်မှတ်ချက် – {movie['imdb_rating']}/10
🗳️ မဲအရေအတွက် – {movie['imdb_votes']}

📖 ဇာတ်လမ်းအကျဉ်း – {movie['plot']}"""
    return text

# ========== /movie Command ==========
async def movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ဥပမာ - /movie Inception 2010 သို့မဟုတ် အင်စက်ပရှင်")
        return
    movie_input = ' '.join(context.args)
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ ရှာမတွေ့ပါ။ အင်္ဂလိပ်အမည် သို့မဟു မြန်မာအမည်ဖြင့် ထပ်စမ်းပါ။")
        return
    text = format_movie_info_plain(movie)
    keyboard = []
    if len(movie['plot']) > 800:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - အပြည့်", movie['plot'])
        if telegraph_url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=telegraph_url)])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await msg.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)

# ========== /createpost Conversation ==========
CREATE_POSTER, CREATE_MOVIE_NAME, CREATE_VIDEO = range(3)

async def createpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 Poster ပုံတစ်ပုံ ပို့ပါ။\nCaption တွင် ဇာတ်ကားအမည် (နှစ်ပါလျှင် ထည့်နိုင်သည်)။")
    return CREATE_POSTER

async def createpost_receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပါ။")
        return CREATE_POSTER
    context.user_data['createpost_poster'] = update.message.photo[-1].file_id
    if update.message.caption:
        movie_input = update.message.caption.strip()
        context.user_data['createpost_movie_input'] = movie_input
        msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာနေသည်...")
        movie = get_movie_info(movie_input)
        if not movie:
            await msg.edit_text("❌ ရှာမတွေ့။ အင်္ဂလိပ်/မြန်မာ အမည်အပြည့်ထည့်ပါ။")
            return CREATE_POSTER
        context.user_data['createpost_movie_data'] = movie
        await msg.edit_text(f"✅ ဇာတ်ကားအချက်အလက် တွေ့ရှိပါသည်။\n\n{format_movie_info_plain(movie)}")
        await update.message.reply_text("🎬 ယခု Video ဖိုင်ကို ပို့ပါ။")
        return CREATE_VIDEO
    else:
        await update.message.reply_text("✍️ ဇာတ်ကားအမည် (မြန်မာ/အင်္ဂလိပ်) ကို စာသားပို့ပါ။")
        return CREATE_MOVIE_NAME

async def createpost_receive_movie_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movie_input = update.message.text.strip()
    context.user_data['createpost_movie_input'] = movie_input
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာနေသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ ရှာမတွေ့။ အင်္ဂလိပ်/မြန်မာ အမည်အပြည့်ထည့်ပါ။")
        return CREATE_MOVIE_NAME
    context.user_data['createpost_movie_data'] = movie
    await msg.edit_text(f"✅ ဇာတ်ကားအချက်အလက် တွေ့ရှိပါသည်။\n\n{format_movie_info_plain(movie)}")
    await update.message.reply_text("🎬 ယခု Video ဖိုင်ကို ပို့ပါ။")
    return CREATE_VIDEO

async def createpost_receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = None
    file_name = None
    
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or f"movie_{video.file_unique_id}"
        logger.info(f"Video received: {video.file_id}")
    elif update.message.document:
        doc = update.message.document
        mime = doc.mime_type or ''
        if mime.startswith('video/') or (doc.file_name and doc.file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))):
            video = doc
            file_name = doc.file_name or f"movie_{doc.file_unique_id}"
            logger.info(f"Document video received: {doc.file_id}")
        else:
            await update.message.reply_text("❌ Video ဖိုင် (mp4, mkv, avi, mov, webm) သာ ပို့ပါ။")
            return CREATE_VIDEO
    
    if not video:
        await update.message.reply_text("❌ Video ဖိုင်တစ်ခု ပို့ပါ။")
        return CREATE_VIDEO
    
    if not BOT_USERNAME:
        await update.message.reply_text("❌ BOT_USERNAME မသတ်မှတ်ထား။ Admin ကို ဆက်သွယ်ပါ။")
        return ConversationHandler.END
    
    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    
    poster = context.user_data.get('createpost_poster')
    movie = context.user_data.get('createpost_movie_data')
    if not movie:
        await update.message.reply_text("❌ ဇာတ်ကားအချက်အလက် ပျောက်နေ။ /createpost ဖြင့် ပြန်စပါ။")
        return ConversationHandler.END
    
    caption_text = format_movie_info_plain(movie)
    keyboard = []
    keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)])
    if len(movie['plot']) > 800:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - အပြည့်", movie['plot'])
        if telegraph_url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=telegraph_url)])
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_photo(
        photo=poster,
        caption=caption_text,
        reply_markup=reply_markup
    )
    await update.message.reply_text("✅ Post ပြင်ဆင်ပြီး။ သင့် Channel တွင် Forward လုပ်ပါ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_createpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီး။")
    context.user_data.clear()
    return ConversationHandler.END

# ========== /start ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        payload = context.args[0]
        file_info = get_file_info(payload)
        if not file_info:
            await update.message.reply_text("❌ လင့်မမှန်ကန်ပါ။")
            return
        if is_user_blocked(user_id):
            await update.message.reply_text("🔒 သင်သည် block ခံထားရပါသည်။")
            return
        all_joined, _ = await check_all_channels(user_id, context)
        if not all_joined:
            attempts = get_attempt_count(user_id) + 1
            increment_attempts(user_id)
            if attempts >= 10:
                block_user(user_id)
                await update.message.reply_text("🚫 ၁၀ ကြိမ်အထက် မအောင်မြင်သောကြောင့် block ခံရပါသည်။")
                return
            msg = "အောက်ပါ Channel များအားလုံးကို ဝင်ပါ:\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• {ch['name']}: {ch['invite']}\n"
            msg += f"\n⚠️ အကြိမ်ရေ: {attempts}/10"
            await update.message.reply_text(msg, disable_web_page_preview=True)
            return
        file_id = file_info["file_id"]
        file_name = file_info["file_name"]
        try:
            await update.message.reply_text(f"🎬 {file_name} ပို့နေပါပြီ...")
            await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"🎬 {file_name}")
            add_user(user_id)
            increment_requests()
            reset_attempts(user_id)
        except Exception as e:
            logger.exception("Send video error")
            await update.message.reply_text(f"❌ မပို့နိုင်ပါ: {str(e)}")
    else:
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text(
                "🎬 မင်္ဂလာပါ။\nဤ Bot သည် Channel များမှ ဇာတ်ကားများ ဖြန့်ဝေရန်ဖြစ်ပါသည်။\nဇာတ်ကားရယူရန် Channel ရှိ Post အောက်က ခလုတ်ကိုနှိပ်ပါ။\n\n/movie ဖြင့် ရှာဖွေနိုင်ပါသည်။"
            )

# ---------- Admin Menu (simplified) ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Post အသစ်", callback_data="menu_createpost")],
        [InlineKeyboardButton("🔗 Deep Link အသစ်", callback_data="menu_newfile")],
        [InlineKeyboardButton("📦 Batch Link", callback_data="menu_batchlink")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("🚫 Block စာရင်း", callback_data="menu_blocklist")],
        [InlineKeyboardButton("🔇 ပိတ်/ဖွင့်", callback_data="menu_mute_unmute")],
    ]
    await update.message.reply_text("🤖 Admin Menu", reply_markup=InlineKeyboardMarkup(keyboard))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ ခွင့်မပြုပါ။")
        return
    data = query.data
    if data == "menu_createpost":
        await query.edit_message_text("/createpost သုံးပါ။")
    elif data == "menu_newfile":
        await query.edit_message_text("/newfile သုံးပါ။")
    elif data == "menu_batchlink":
        await query.edit_message_text("/batchlink သုံးပါ။")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_requests = get_total_requests()
        await query.edit_message_text(f"👥 သုံးစွဲသူ: {total_users}\n🎬 တောင်းဆိုမှု: {total_requests}")
    elif data == "menu_blocklist":
        blocked = get_blocked_users()
        msg = "Blocked users:\n" + "\n".join([str(uid) for uid in blocked]) if blocked else "ဘယ်သူမှမရှိပါ။"
        await query.edit_message_text(msg)
    elif data == "menu_mute_unmute":
        current = context.bot_data.get('maintenance_mode', False)
        new = not current
        await context.bot.set_bot_data({'maintenance_mode': new})
        await query.edit_message_text("🔇 Bot ပိတ်ထားပြီ" if new else "🔊 Bot ဖွင့်ထားပြီ")

# ---------- Admin commands ----------
async def newfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📤 Video ဖိုင်တစ်ခု ပို့ပါ။")
    context.user_data['waiting_newfile'] = True

async def handle_newfile_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_newfile'):
        return
    video = None
    file_name = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "video"
    if not video:
        await update.message.reply_text("Video ဖိုင်တစ်ခု ပို့ပါ။")
        return
    if not BOT_USERNAME:
        await update.message.reply_text("BOT_USERNAME missing.")
        return
    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    link = create_deep_linked_url(BOT_USERNAME, payload)
    await update.message.reply_text(f"Deep Link:\n{link}")
    context.user_data.pop('waiting_newfile', None)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        total_users = users_collection.count_documents({})
        total_requests = get_total_requests()
        await update.message.reply_text(f"👥 {total_users} users\n🎬 {total_requests} requests")

async def batchlink_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data['batch_videos'] = []
    await update.message.reply_text("Video များ တစ်ခုချင်းပို့ပါ။ ပြီးလျှင် /done ရိုက်ပါ။")

async def batchlink_receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if 'batch_videos' not in context.user_data:
        return
    video = None
    file_name = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "video"
    if not video:
        await update.message.reply_text("Video ပို့ပါ။")
        return
    context.user_data['batch_videos'].append({"file_id": video.file_id, "file_name": file_name})
    await update.message.reply_text(f"✅ ဖိုင် #{len(context.user_data['batch_videos'])}: {file_name}")

async def batchlink_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    videos = context.user_data.get('batch_videos', [])
    if not videos:
        await update.message.reply_text("ဘာမှမရှိပါ။")
        return
    if not BOT_USERNAME:
        await update.message.reply_text("BOT_USERNAME missing")
        return
    results = []
    for v in videos:
        payload = generate_payload()
        save_file_info(payload, v["file_id"], v["file_name"])
        link = create_deep_linked_url(BOT_USERNAME, payload)
        results.append(f"{v['file_name']}: {link}")
    text = "Batch Links:\n" + "\n".join(results)
    if len(text) > 4000:
        text = text[:4000] + "..."
    await update.message.reply_text(text)
    context.user_data.clear()

async def cancel_batchlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")

batchlink_handler = ConversationHandler(
    entry_points=[CommandHandler('batchlink', batchlink_start)],
    states={0: [MessageHandler(filters.VIDEO | filters.Document.ALL, batchlink_receive_video)]},
    fallbacks=[CommandHandler('done', batchlink_done), CommandHandler('cancel', cancel_batchlink)],
)

async def blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    blocked = get_blocked_users()
    msg = "Blocked:\n" + "\n".join([str(uid) for uid in blocked]) if blocked else "Empty"
    await update.message.reply_text(msg)

async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("/unblock user_id")
        return
    try:
        uid = int(context.args[0])
        if is_user_blocked(uid):
            unblock_user(uid)
            await update.message.reply_text(f"Unblocked {uid}")
        else:
            await update.message.reply_text(f"{uid} not blocked")
    except:
        await update.message.reply_text("Invalid ID")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

conv_createpost = ConversationHandler(
    entry_points=[CommandHandler('createpost', createpost_start)],
    states={
        CREATE_POSTER: [MessageHandler(filters.PHOTO, createpost_receive_poster)],
        CREATE_MOVIE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_receive_movie_name)],
        CREATE_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.ALL, createpost_receive_video)],
    },
    fallbacks=[CommandHandler('cancel', cancel_createpost)],
)

application.add_handler(conv_createpost)
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("movie", movie_command))
application.add_handler(CommandHandler("newfile", newfile_command))
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL & ~filters.COMMAND, handle_newfile_video))
application.add_handler(CommandHandler("stats", stats_command))
application.add_handler(batchlink_handler)
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("menu", show_menu))
application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

# Placeholders for other commands
application.add_handler(CommandHandler("link", lambda u,c: newfile_command(u,c)))
application.add_handler(CommandHandler("channelpost", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("convert_old", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("test_channel", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("schedule", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("listschedule", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("cancelschedule", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("delete", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("deleteall", lambda u,c: u.message.reply_text("Not implemented")))
application.add_handler(CommandHandler("mute", lambda u,c: u.message.reply_text("Use /menu")))
application.add_handler(CommandHandler("unmute", lambda u,c: u.message.reply_text("Use /menu")))

def run_bot():
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.info("Bot started polling...")
            application.run_polling()
        except Exception as e:
            logger.exception(f"Crashed: {e}. Restarting in 10s")
            import time
            time.sleep(10)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
