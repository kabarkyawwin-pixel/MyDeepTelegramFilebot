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

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------- Flask ----------
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
    result = file_store_collection.update_one(
        {"payload": payload},
        {"$set": {"file_id": file_id, "file_name": file_name}},
        upsert=True
    )
    logger.info(f"Saved to MongoDB: payload={payload}, file_id={file_id}, matched={result.matched_count}, modified={result.modified_count}")

def get_file_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    if doc:
        return {"file_id": doc["file_id"], "file_name": doc["file_name"]}
    return None

# ---------- Block helpers ----------
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
    logger.error("TELEGRAM_TOKEN environment variable not set!")
    sys.exit(1)

BOT_USERNAME = os.environ.get("BOT_USERNAME")
if not BOT_USERNAME:
    logger.error("BOT_USERNAME environment variable not set! Deep links will not work.")
    # သတိပေးချက်သာ၊ bot ကို မရပ်ပါနဲ့

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

async def create_telegraph_page(title: str, content_text: str) -> str:
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
            logger.info(f"Translated Burmese '{text}' -> '{english}'")
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

def format_movie_info_burmese(movie):
    try:
        rating = float(movie['imdb_rating'])
        stars = '⭐' * int(rating // 2) + ('✨' if rating % 2 >= 0.5 else '')
    except:
        stars = ''
    text = f"""🎬 **{movie['title']}** ({movie['year']})

📌 **အမျိုးအစား** – {movie['genre']}
🎭 **သရုပ်ဆောင်များ** – {movie['actors']}
🎥 **ဒါရိုက်တာ** – {movie['director']}
⏱️ **ကြာချိန်** – {movie['runtime']}
🌍 **နိုင်ငံ** – {movie['country']}
🗣️ **ဘာသာစကား** – {movie['language']}
⭐ **IMDb အဆင့်သတ်မှတ်ချက်** – {movie['imdb_rating']}/10 {stars}
🗳️ **မဲအရေအတွက်** – {movie['imdb_votes']}

📖 **ဇာတ်လမ်းအကျဉ်း –** {movie['plot']}
"""
    return text

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
        logger.error(f"Telegraph movie page error: {e}")
        return None

# ========== /movie Command ==========
async def movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ဥပမာ - `/movie Inception 2010` သို့ `အင်စက်ပရှင်` လို့လည်းရပါတယ်။", parse_mode="Markdown")
        return
    movie_input = ' '.join(context.args)
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ ရှာမတွေ့ပါ။ ကျေးဇူးပြု၍ အင်္ဂလိပ်အမည် သို့မဟုတ် မြန်မာအမည်ဖြင့် ထပ်စမ်းပါ။")
        return
    formatted = format_movie_info_burmese(movie)
    keyboard = []
    if len(movie['plot']) > 1024:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
        if telegraph_url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်", url=telegraph_url)])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await msg.edit_text(formatted, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)

# ========== /createpost Conversation ==========
CREATE_POSTER, CREATE_MOVIE_NAME, CREATE_VIDEO = range(3)

async def createpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ဇာတ်ကား Poster ပုံတစ်ပုံ ပို့ပေးပါ။\nCaption တွင် ဇာတ်ကားအမည် (မြန်မာ/အင်္ဂလိပ်) + ထုတ်ဝေနှစ် (ဥပမာ - Inception 2010) ထည့်နိုင်သည်။")
    return CREATE_POSTER

async def createpost_receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပေးပါ။")
        return CREATE_POSTER
    context.user_data['createpost_poster'] = update.message.photo[-1].file_id
    if update.message.caption:
        movie_input = update.message.caption.strip()
        context.user_data['createpost_movie_input'] = movie_input
        msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
        movie = get_movie_info(movie_input)
        if not movie:
            await msg.edit_text("❌ ရှာမတွေ့ပါ။ ကျေးဇူးပြု၍ အင်္ဂလိပ်/မြန်မာ အမည်အပြည့်အစုံ (နှစ်ပါ/မပါ) ထည့်ပါ။")
            return CREATE_POSTER
        context.user_data['createpost_movie_data'] = movie
        formatted = format_movie_info_burmese(movie)
        if len(movie['plot']) > 1024:
            telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
            if telegraph_url:
                formatted += f"\n\n📖 [ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်]({telegraph_url})"
        await msg.edit_text(f"**✅ ဇာတ်ကားအချက်အလက် တွေ့ရှိပါသည်။**\n\n{formatted}", parse_mode='Markdown', disable_web_page_preview=True)
        await update.message.reply_text("🎬 ယခု ဇာတ်ကား Video ဖိုင်ကို ပို့ပေးပါ။")
        return CREATE_VIDEO
    else:
        await update.message.reply_text("✍️ ဇာတ်ကားအမည် (မြန်မာ/အင်္ဂလိပ်) ကို စာသားအနေဖြင့် ပို့ပေးပါ။")
        return CREATE_MOVIE_NAME

async def createpost_receive_movie_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movie_input = update.message.text.strip()
    context.user_data['createpost_movie_input'] = movie_input
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ ရှာမတွေ့ပါ။ ကျေးဇူးပြု၍ အင်္ဂလိပ်/မြန်မာ အမည်အပြည့်အစုံ (နှစ်ပါ/မပါ) ထည့်ပါ။")
        return CREATE_MOVIE_NAME
    context.user_data['createpost_movie_data'] = movie
    formatted = format_movie_info_burmese(movie)
    if len(movie['plot']) > 1024:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
        if telegraph_url:
            formatted += f"\n\n📖 [ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်]({telegraph_url})"
    await msg.edit_text(f"**✅ ဇာတ်ကားအချက်အလက် တွေ့ရှိပါသည်။**\n\n{formatted}", parse_mode='Markdown', disable_web_page_preview=True)
    await update.message.reply_text("🎬 ယခု ဇာတ်ကား Video ဖိုင်ကို ပို့ပေးပါ။")
    return CREATE_VIDEO

async def createpost_receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document:
        doc = update.message.document
        mime = doc.mime_type or ''
        if mime.startswith('video/') or (doc.file_name and doc.file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))):
            video = doc
    if not video:
        await update.message.reply_text("❌ ကျေးဇူးပြု၍ Video ဖိုင် (mp4, mkv, avi, mov, webm) သာ ပို့ပေးပါ။")
        return CREATE_VIDEO

    if not BOT_USERNAME:
        await update.message.reply_text("❌ BOT_USERNAME မသတ်မှတ်ထားပါ။ Admin ကို ဆက်သွယ်ပါ။")
        return ConversationHandler.END

    payload = generate_payload()
    file_name = getattr(video, 'file_name', None) or f"movie_{payload[:8]}"
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    poster = context.user_data.get('createpost_poster')
    movie = context.user_data.get('createpost_movie_data')
    if not movie:
        await update.message.reply_text("❌ ဇာတ်ကားအချက်အလက် ပျောက်နေသည်။ /createpost ဖြင့် ထပ်မံစတင်ပါ။")
        return ConversationHandler.END

    formatted_info = format_movie_info_burmese(movie)
    keyboard = []
    keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)])
    if len(movie['plot']) > 1024:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
        if telegraph_url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်", url=telegraph_url)])
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_photo(
        photo=poster,
        caption=formatted_info,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    await update.message.reply_text("✅ **Post ပြင်ဆင်ပြီးပါပြီ။**\n\nဤ Post ကို သင့် Channel တွင် Forward လုပ်ပါ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_createpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ========== /start ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        payload = context.args[0]
        file_info = get_file_info(payload)
        if not file_info:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return
        if is_user_blocked(user_id):
            await update.message.reply_text("🔒 သင်သည် ချန်နယ်များကို မဝင်ဘဲ လင့်ကို ၁၀ ကြိမ်အထက်နှိပ်ထားသောကြောင့် block ခံထားရပါသည်။")
            return
        all_joined, _ = await check_all_channels(user_id, context)
        if not all_joined:
            attempts = get_attempt_count(user_id) + 1
            increment_attempts(user_id)
            if attempts >= 10:
                block_user(user_id)
                await update.message.reply_text("🚫 သင်သည် လိုအပ်သောချန်နယ်များကို မဝင်ဘဲ ၁၀ ကြိမ်အထက်နှိပ်ထားသောကြောင့် block ခံရပါသည်။")
                return
            msg = "🎬 ဇာတ်ကားဖိုင်ရယူရန် အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါ။\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• {ch['name']}: [ဝင်ရန်]({ch['invite']})\n"
            msg += f"\n⚠️ သင်သည် ဤလင့်ကို **{attempts}/10** ကြိမ် နှိပ်ပြီးဖြစ်သည်။"
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            return
        file_id = file_info["file_id"]
        file_name = file_info["file_name"]
        
        if not file_id:
            await update.message.reply_text("❌ ဖိုင် ID မှားယွင်းနေပါသည်။ Admin ကို ဆက်သွယ်ပါ။")
            return
        
        try:
            await update.message.reply_text(f"🎬 {file_name} ပို့ပေးနေပါပြီ...")
            video_msg = None
            for attempt in range(3):
                try:
                    video_msg = await context.bot.send_video(
                        chat_id=user_id,
                        video=file_id,
                        caption=f"🎬 {file_name}",
                        timeout=60
                    )
                    break
                except RetryAfter as e:
                    wait = e.retry_after
                    await update.message.reply_text(f"⏳ ဆာဗာက အလုပ်များနေလို့ {wait} စက္ကန့် စောင့်ပါ။")
                    await asyncio.sleep(wait)
                except Exception as e:
                    logger.error(f"Attempt {attempt+1} failed: {e}")
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2)
            if not video_msg:
                raise Exception("No video message after retries")
            
            warning_text = "⚠️ ဤဖိုင်ကို 5 မိနစ်အတွင်း ဖျက်ပါမည်။ Saved Messages သို့ Forward လုပ်ပြီး သိမ်းဆည်းပါ။"
            warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text)
            
            async def safe_delete():
                await asyncio.sleep(300)
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                    await context.bot.delete_message(chat_id=user_id, message_id=video_msg.message_id)
                except Exception as del_err:
                    logger.warning(f"Delete error: {del_err}")
            asyncio.create_task(safe_delete())
            add_user(user_id)
            increment_requests()
            reset_attempts(user_id)
        except RetryAfter as e:
            await update.message.reply_text(f"⏳ ဆာဗာအလုပ်များနေပါသည်။ {e.retry_after} စက္ကန့်ကြာပြီးမှ ထပ်စမ်းပါ။")
        except Exception as e:
            logger.exception(f"Start video send error: {e}")
            await update.message.reply_text(
                "❌ ဗီဒီယိုဖိုင် ပို့ရာတွင် အဆင်မပြေမှုရှိပါသည်။\n"
                "ဖိုင်သက်တမ်းကုန်သွားခြင်း သို့မဟုတ် Telegram ဆာဗာ အလုပ်များနေခြင်းဖြစ်နိုင်ပါသည်။\n"
                "မိနစ်အနည်းငယ်ကြာမှ ထပ်စမ်းပါ။"
            )
    else:
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text(
                "🎬 **မင်္ဂလာပါ**\n\n"
                "ဤ Bot သည် Channel များအတွက် ဇာတ်ကားများ ဖြန့်ဝေရန် သုံးပါသည်။\n"
                "ဇာတ်ကားရယူရန် Channel ရှိ Post အောက်က ခလုတ်ကို နှိပ်ပါ။\n"
                "ပထမဆုံး လိုအပ်သော Channel 4 ခုလုံးကို ဝင်ရောက်ထားရပါမည်။\n\n"
                "✨ `/movie` command ဖြင့် ဇာတ်ကားအချက်အလက်များ ရှာဖွေနိုင်ပါသည်။\n"
                "မြန်မာလိုလည်း ရိုက်ထည့်လို့ရပါသည်။",
                parse_mode="Markdown"
            )

# ---------- Admin Menu ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Post အသစ်ဖန်တီးရန်", callback_data="menu_createpost")],
        [InlineKeyboardButton("🔗 Deep Link အသစ်ထုတ်ရန်", callback_data="menu_newfile")],
        [InlineKeyboardButton("📦 Batch Link (အစုလိုက်)", callback_data="menu_batchlink")],
        [InlineKeyboardButton("📢 Channel သို့ တိုက်ရိုက်တင်ရန်", callback_data="menu_channelpost")],
        [InlineKeyboardButton("🔄 ဟောင်းများပြောင်းရန်", callback_data="menu_convert_old")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 Broadcast ပို့ရန်", callback_data="menu_broadcast")],
        [InlineKeyboardButton("🚫 Block စာရင်း", callback_data="menu_blocklist")],
        [InlineKeyboardButton("🔇 Bot ပိတ်ရန်", callback_data="menu_mute")],
        [InlineKeyboardButton("🔊 Bot ဖွင့်ရန်", callback_data="menu_unmute")],
    ]
    await update.message.reply_text("🤖 **Admin Menu**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    data = query.data
    if data == "menu_createpost":
        await query.edit_message_text("➕ `/createpost` command ကိုသုံးပါ။")
    elif data == "menu_newfile":
        await query.edit_message_text("🔗 `/newfile` command ကိုသုံးပါ။ (Video ပို့ပါက Deep Link ရမည်)")
    elif data == "menu_batchlink":
        await query.edit_message_text("📦 `/batchlink` command ကိုသုံးပါ။")
    elif data == "menu_channelpost":
        await query.edit_message_text("📢 `/channelpost` command ကိုသုံးပါ။")
    elif data == "menu_convert_old":
        await query.edit_message_text("🔄 `/convert_old` command ကိုသုံးပါ။")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_requests = get_total_requests()
        await query.edit_message_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n🎬 တောင်းဆိုမှုအရေအတွက်: {total_requests}", parse_mode="Markdown")
    elif data == "menu_broadcast":
        await query.edit_message_text("📢 `/broadcast <message>` ဖြင့် အသုံးပြုသူအားလုံးကို စာပို့နိုင်ပါသည်။")
    elif data == "menu_blocklist":
        blocked = get_blocked_users()
        if not blocked:
            await query.edit_message_text("📊 လောလောဆယ် block ထားသူ မရှိပါ။")
        else:
            msg = "🚫 **Blocked Users**\n\n" + "\n".join([f"• `{uid}`" for uid in blocked]) + "\n\n/unblock <user_id> ဖြင့် ပြန်ဖွင့်နိုင်ပါသည်။"
            await query.edit_message_text(msg, parse_mode="Markdown")
    elif data == "menu_mute":
        await context.bot.set_bot_data({'maintenance_mode': True})
        await query.edit_message_text("🔇 Bot ကို ယာယီပိတ်ထားပါသည်။")
    elif data == "menu_unmute":
        await context.bot.set_bot_data({'maintenance_mode': False})
        await query.edit_message_text("🔊 Bot ပုံမှန်အလုပ်လုပ်ပါပြီ။")

# ---------- Admin Command Implementations ----------
async def newfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    await update.message.reply_text("📤 Video ဖိုင်တစ်ခု ပို့ပေးပါ။ (Deep Link ထုတ်ပေးပါမည်)")
    context.user_data['waiting_for_newfile'] = True

async def handle_video_for_newfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get('waiting_for_newfile'):
        video = None
        if update.message.video:
            video = update.message.video
        elif update.message.document:
            doc = update.message.document
            mime = doc.mime_type or ''
            if mime.startswith('video/') or (doc.file_name and doc.file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))):
                video = doc
        if video:
            if not BOT_USERNAME:
                logger.error("BOT_USERNAME is not set!")
                await update.message.reply_text("❌ BOT_USERNAME environment variable မသတ်မှတ်ထားပါ။ ကျေးဇူးပြု၍ သတ်မှတ်ပါ။")
                context.user_data.pop('waiting_for_newfile', None)
                return
            payload = generate_payload()
            file_name = getattr(video, 'file_name', None) or "movie"
            save_file_info(payload, video.file_id, file_name)
            logger.info(f"Saved file: payload={payload}, file_id={video.file_id}, name={file_name}")
            deep_link = create_deep_linked_url(BOT_USERNAME, payload)
            logger.info(f"Generated deep link: {deep_link}")
            await update.message.reply_text(
                f"🔗 **Deep Link**\n\n{deep_link}\n\n"
                f"`{file_name}` အတွက်ဖြစ်ပါသည်။\n"
                f"(Channel 4 ခုလုံးဝင်ထားရန် လိုအပ်)",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Video ဖိုင် (MP4/MKV/AVI/MOV) တစ်ခု ပို့ပေးပါ။")
        context.user_data.pop('waiting_for_newfile', None)

async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await newfile_command(update, context)

async def batchlink_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    context.user_data['batch_videos'] = []
    await update.message.reply_text("📦 **Batch Deep Link Generator**\n\nVideo ဖိုင်များကို တစ်ခုချင်းစီ ဆက်တိုက်ပို့ပါ။ ပြီးပါက `/done` ရိုက်ပါ။ ဖျက်ရန် `/cancel` ရိုက်ပါ။")

async def batchlink_receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if 'batch_videos' not in context.user_data:
        await update.message.reply_text("/batchlink ဖြင့် စတင်ပါ။")
        return
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document:
        doc = update.message.document
        mime = doc.mime_type or ''
        if mime.startswith('video/') or (doc.file_name and doc.file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))):
            video = doc
    if not video:
        await update.message.reply_text("Video ဖိုင်တစ်ခု ပို့ပေးပါ။")
        return
    file_name = getattr(video, 'file_name', None) or "movie"
    context.user_data['batch_videos'].append({"file_id": video.file_id, "file_name": file_name})
    await update.message.reply_text(f"✅ ဖိုင် #{len(context.user_data['batch_videos'])}: `{file_name}` လက်ခံပြီး။\n(ဆက်ပို့ရန် သို့မဟုတ် `/done`)", parse_mode='Markdown')

async def batchlink_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    videos = context.user_data.get('batch_videos', [])
    if not videos:
        await update.message.reply_text("❌ Video များမရှိပါ။ /batchlink ဖြင့် ထပ်စတင်ပါ။")
        return
    if not BOT_USERNAME:
        await update.message.reply_text("❌ BOT_USERNAME မသတ်မှတ်ထားပါ။")
        return
    results = []
    for v in videos:
        payload = generate_payload()
        save_file_info(payload, v["file_id"], v["file_name"])
        deep_link = create_deep_linked_url(BOT_USERNAME, payload)
        results.append(f"• **{v['file_name']}**\n  {deep_link}")
    text = "📦 **Batch Deep Links**\n\n" + "\n\n".join(results)
    if len(text) > 4000:
        text = text[:4000] + "\n...(စာရင်းတိုသွားပါသည်)"
    await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)
    context.user_data.clear()

async def cancel_batchlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")

batchlink_handler = ConversationHandler(
    entry_points=[CommandHandler('batchlink', batchlink_start)],
    states={
        0: [MessageHandler(filters.VIDEO | filters.Document.ALL, batchlink_receive_video)],
    },
    fallbacks=[CommandHandler('done', batchlink_done), CommandHandler('cancel', cancel_batchlink)],
)

async def channelpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📸 ပုံနှင့် စာသားတစ်ခါတည်း ပို့ပေးပါ။ ထို့နောက် Video ဖိုင်ပို့ပါ။")
    context.user_data['channelpost_photo'] = None
    context.user_data['channelpost_caption'] = None
    return 0

async def channelpost_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပေးပါ။")
        return 0
    context.user_data['channelpost_photo'] = update.message.photo[-1].file_id
    context.user_data['channelpost_caption'] = update.message.caption or ""
    await update.message.reply_text("🎬 ယခု Video ဖိုင်ကို ပို့ပေးပါ။")
    return 1

async def channelpost_receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document:
        doc = update.message.document
        mime = doc.mime_type or ''
        if mime.startswith('video/') or (doc.file_name and doc.file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))):
            video = doc
    if not video:
        await update.message.reply_text("Video ဖိုင်တစ်ခု ပို့ပေးပါ။")
        return 1
    if not BOT_USERNAME:
        await update.message.reply_text("❌ BOT_USERNAME မသတ်မှတ်ထားပါ။")
        return ConversationHandler.END
    payload = generate_payload()
    file_name = getattr(video, 'file_name', None) or "movie"
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    button = InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)
    reply_markup = InlineKeyboardMarkup([[button]])
    photo_id = context.user_data.get('channelpost_photo')
    caption = context.user_data.get('channelpost_caption', '')
    if photo_id:
        for ch_id in POST_CHANNELS:
            try:
                await context.bot.send_photo(chat_id=ch_id, photo=photo_id, caption=caption, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Post to {ch_id} failed: {e}")
        await update.message.reply_text(f"✅ Post ကို Channel {len(POST_CHANNELS)} ခုသို့ တင်ခဲ့သည်။")
    else:
        await update.message.reply_text("ပုံမရှိပါ။")
    context.user_data.clear()
    return ConversationHandler.END

channelpost_handler = ConversationHandler(
    entry_points=[CommandHandler('channelpost', channelpost_start)],
    states={
        0: [MessageHandler(filters.PHOTO, channelpost_receive_photo)],
        1: [MessageHandler(filters.VIDEO | filters.Document.ALL, channelpost_receive_video)],
    },
    fallbacks=[CommandHandler('cancel', lambda u,c: u.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။"))],
)

async def convert_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    limit = context.args[0] if context.args else None
    try:
        limit = int(limit) if limit else None
    except:
        await update.message.reply_text("နံပါတ်တစ်ခုသာ ထည့်ပါ။ /convert_old 500")
        return
    if not os.path.exists('old_posts.json'):
        await update.message.reply_text("old_posts.json ဖိုင်မရှိပါ။")
        return
    with open('old_posts.json', 'r', encoding='utf-8') as f:
        posts = json.load(f)
    if limit:
        posts = posts[:limit]
    if not BOT_USERNAME:
        await update.message.reply_text("❌ BOT_USERNAME မသတ်မှတ်ထားပါ။")
        return
    success = 0
    for post in posts:
        try:
            file_id = post.get('file_id')
            photo_id = post.get('photo_id')
            caption = post.get('caption', '')
            channel_id = int(post.get('channel')) if isinstance(post.get('channel'), str) else post.get('channel')
            if not file_id or not channel_id:
                continue
            payload = generate_payload()
            save_file_info(payload, file_id, f"movie_{post.get('message_id')}")
            deep_link = create_deep_linked_url(BOT_USERNAME, payload)
            button = InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)
            reply_markup = InlineKeyboardMarkup([[button]])
            if photo_id:
                await context.bot.send_photo(chat_id=channel_id, photo=photo_id, caption=caption[:1024], reply_markup=reply_markup)
            else:
                await context.bot.send_message(chat_id=channel_id, text=f"{caption}\n\n👇 ဇာတ်ကားရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။", reply_markup=reply_markup)
            success += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Convert error: {e}")
    await update.message.reply_text(f"✅ ပြောင်းလဲခြင်း ပြီးဆုံးပါပြီ။ အောင်မြင်သည်: {success}/{len(posts)}")

async def test_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("Channel ID (နံပါတ်) ပို့ပါ။ ဥပမာ -1001234567890")
    context.user_data['test_channel'] = True

async def test_channel_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('test_channel'):
        try:
            cid = int(update.message.text.strip())
            await context.bot.send_message(chat_id=cid, text="✅ စမ်းသပ်မက်ဆေ့ချ် အောင်မြင်ပါသည်။")
            await update.message.reply_text(f"✅ Channel {cid} သို့ မက်ဆေ့ချ်ပို့နိုင်ပါသည်။")
        except:
            await update.message.reply_text("❌ မအောင်မြင်ပါ။ Bot သည် Channel တွင် Admin ဖြစ်မဖြစ် စစ်ဆေးပါ။")
        context.user_data.pop('test_channel', None)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n🎬 တောင်းဆိုမှုအရေအတွက်: {total_requests}", parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("📢 /broadcast <message>")
        return
    users = get_all_users()
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 ပြန်လွှင့်ပြီးပါပြီ။ လက်ခံသူ {count} ဦး။")

async def blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    blocked = get_blocked_users()
    if not blocked:
        await update.message.reply_text("📊 လောလောဆယ် block ထားသူ မရှိပါ။")
        return
    msg = "🚫 **Blocked Users**\n" + "\n".join([f"• `{uid}`" for uid in blocked])
    await update.message.reply_text(msg, parse_mode="Markdown")

async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("📌 /unblock <user_id>")
        return
    try:
        uid = int(context.args[0])
        if is_user_blocked(uid):
            unblock_user(uid)
            await update.message.reply_text(f"✅ User `{uid}` ကို unblock လုပ်လိုက်ပါသည်။", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"ℹ️ User `{uid}` သည် block မခံရသေးပါ။", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ User ID ဂဏန်းသာ ထည့်ပါ။")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await context.bot.set_bot_data({'maintenance_mode': True})
    await update.message.reply_text("🔇 Bot ကို ယာယီပိတ်ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await context.bot.set_bot_data({'maintenance_mode': False})
    await update.message.reply_text("🔊 Bot ပုံမှန်အလုပ်လုပ်ပါပြီ။")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    await show_menu(update, context)

# Placeholders (not implemented)
async def schedule(update, context): await update.message.reply_text("⏳ အချိန်ဇယား - လုပ်ဆောင်ဆဲ။")
async def listschedule(update, context): await update.message.reply_text("📋 အချိန်ဇယားစာရင်း - လုပ်ဆောင်ဆဲ။")
async def cancelschedule(update, context): await update.message.reply_text("❌ အချိန်ဇယားဖျက်ရန် - လုပ်ဆောင်ဆဲ။")
async def delete_file(update, context): await update.message.reply_text("🗑️ ဖိုင်ဖျက်ရန် - လုပ်ဆောင်ဆဲ။")
async def deleteall(update, context): await update.message.reply_text("⚠️ အားလုံးဖျက်ရန် - လုပ်ဆောင်ဆဲ။")

# ---------- Application Setup ----------
application = Application.builder().token(TOKEN).build()

createpost_handler = ConversationHandler(
    entry_points=[CommandHandler('createpost', createpost_start)],
    states={
        CREATE_POSTER: [MessageHandler(filters.PHOTO, createpost_receive_poster)],
        CREATE_MOVIE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_receive_movie_name)],
        CREATE_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.ALL, createpost_receive_video)],
    },
    fallbacks=[CommandHandler('cancel', cancel_createpost)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("movie", movie_command))
application.add_handler(createpost_handler)
application.add_handler(CommandHandler("newfile", newfile_command))
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video_for_newfile))
application.add_handler(CommandHandler("link", link_command))
application.add_handler(batchlink_handler)
application.add_handler(channelpost_handler)
application.add_handler(CommandHandler("convert_old", convert_old))
application.add_handler(CommandHandler("test_channel", test_channel))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, test_channel_receive))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("menu", menu_command))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))
application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

# ---------- Polling ----------
def run_bot():
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.info("Starting bot polling...")
            application.run_polling()
        except Exception as e:
            logger.exception(f"Bot crashed: {e}. Restarting in 10s")
            import time
            time.sleep(10)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
