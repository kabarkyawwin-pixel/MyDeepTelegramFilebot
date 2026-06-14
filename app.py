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
    level=logging.DEBUG,
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
    file_store_collection.update_one(
        {"payload": payload},
        {"$set": {"file_id": file_id, "file_name": file_name}},
        upsert=True
    )

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
BOT_USERNAME = os.environ.get("BOT_USERNAME")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

POST_CHANNELS = [ch.strip() for ch in os.environ.get("POST_CHANNELS", "").split(",") if ch.strip()] if os.environ.get("POST_CHANNELS") else []

REQUIRED_CHANNELS = [
    {"id": "-1003753299714", "name": "🎬 Movies channel main (HD Movies များ)", "invite": "https://t.me/wznmoviescollector"},
    {"id": "-1003899625672", "name": "🎬 Movies channel 2 (အရံချန်နယ်)", "invite": "https://t.me/moviesandseriesforallwzn"},
    {"id": "-1003792838735", "name": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ် (ကလေးများမဝင်ရ)", "invite": "https://t.me/everyboyhobby"},
    {"id": "-1003785717514", "name": "🎵 မြန်မာသီချင်းချန်နယ်", "invite": "https://t.me/wznmusiclibary"}
]

OTHER_CHANNELS = [link.strip() for link in os.environ.get("OTHER_CHANNELS", "").split(",") if link.strip() and link.strip().startswith("http")] if os.environ.get("OTHER_CHANNELS") else []
MUSIC_CHANNEL_LINK = os.environ.get("MUSIC_CHANNEL_LINK", "")
if MUSIC_CHANNEL_LINK and not MUSIC_CHANNEL_LINK.startswith("http"):
    MUSIC_CHANNEL_LINK = ""

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

maintenance_mode = False

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
            author_name="File Share Bot"
        )
        return response['url']
    except Exception as e:
        logger.error(f"Telegraph error: {e}")
        return None

# ---------- Translation ----------
translator = GoogleTranslator(source='en', target='my')

def translate_text(text):
    if not text or text == 'N/A':
        return text
    try:
        return translator.translate(text)
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text

# ---------- Movie Info with Year Support ----------
OMDB_API_KEY = "5025f95c"

def parse_movie_name_and_year(input_str):
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
        plot_my = translate_text(plot_en)
        return {
            'title': data.get('Title', 'N/A'),
            'year': data.get('Year', 'N/A'),
            'genre': translate_text(data.get('Genre', 'N/A')),
            'actors': translate_text(data.get('Actors', 'N/A')),
            'director': translate_text(data.get('Director', 'N/A')),
            'runtime': data.get('Runtime', 'N/A'),
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

📝 **အမျိုးအစား:** {movie['genre']}
🎭 **သရုပ်ဆောင်များ:** {movie['actors']}
🎥 **ဒါရိုက်တာ:** {movie['director']}
⏱️ **ကြာချိန်:** {movie['runtime']}
🌍 **နိုင်ငံ:** {movie['country']}
🗣️ **ဘာသာစကား:** {movie['language']}
⭐ **IMDb အဆင့်:** {movie['imdb_rating']}/10  {stars}
🗳️ **မဲအရေအတွက်:** {movie['imdb_votes']}

📖 **ဇာတ်လမ်းအကျဉ်း:**
{movie['plot']}

🔗 **IMDb Link:** https://www.imdb.com/title/{movie['imdb_id']}/
"""
    return text

async def create_telegraph_page_movie(title, content_text):
    try:
        html_content = content_text.replace('\n', '<br>')
        response = await asyncio.to_thread(
            telegraph.create_page,
            title=title,
            html_content=f"<p>{html_content}</p>",
            author_name="Kbkfilesend Bot"
        )
        return response['url']
    except Exception as e:
        logger.error(f"Telegraph movie page error: {e}")
        return None

async def send_with_retry(context, chat_id, **kwargs):
    try:
        return await context.bot.send_message(chat_id=chat_id, **kwargs)
    except RetryAfter as e:
        wait_time = e.retry_after
        logger.warning(f"Flood control exceeded. Retrying in {wait_time} seconds.")
        await asyncio.sleep(wait_time)
        return await context.bot.send_message(chat_id=chat_id, **kwargs)

# ========== /movie Command (User lookup) ==========
async def movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("ဥပမာ - `/movie Inception 2010` သို့ `Recollection (2025)`", parse_mode="Markdown")
        return
    movie_input = ' '.join(context.args)
    await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await update.message.reply_text("❌ ရှာမတွေ့ပါ။ ကျေးဇူးပြု၍ အင်္ဂလိပ်အမည်အပြည့်အစုံ သို့မဟုတ် Year ထည့်ပါ။")
        return
    formatted = format_movie_info_burmese(movie)
    keyboard = []
    if len(movie['plot']) > 1024:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
        if telegraph_url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်", url=telegraph_url)])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(formatted, parse_mode='Markdown', reply_markup=reply_markup, disable_web_page_preview=True)

# ========== /createpost Conversation ==========
CREATE_POSTER, CREATE_MOVIE_NAME, CREATE_VIDEO = range(3)

async def createpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ဇာတ်ကား Poster ပုံတစ်ပုံ ပို့ပေးပါ။\nCaption တွင် ဇာတ်ကားအမည် + Year (ဥပမာ - Inception 2010) ထည့်နိုင်သည်။")
    return CREATE_POSTER

async def createpost_receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပေးပါ။")
        return CREATE_POSTER
    context.user_data['createpost_poster'] = update.message.photo[-1].file_id
    if update.message.caption:
        movie_input = update.message.caption.strip()
        context.user_data['createpost_movie_input'] = movie_input
        await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
        movie = get_movie_info(movie_input)
        if not movie:
            await update.message.reply_text("❌ ရှာမတွေ့ပါ။ ကျေးဇူးပြု၍ အင်္ဂလိပ်အမည်အပြည့်အစုံ (Year ပါ/မပါ) ထည့်ပါ။")
            return CREATE_POSTER
        context.user_data['createpost_movie_data'] = movie
        formatted = format_movie_info_burmese(movie)
        if len(movie['plot']) > 1024:
            telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
            if telegraph_url:
                formatted += f"\n\n📖 [ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်]({telegraph_url})"
        await update.message.reply_text(f"**✅ ဇာတ်ကားအချက်အလက် တွေ့ရှိပါသည်။**\n\n{formatted}", parse_mode='Markdown', disable_web_page_preview=True)
        await update.message.reply_text("🎬 ဆက်လက်ရန် ဇာတ်ကား Video ဖိုင်ကို ပို့ပေးပါ။")
        return CREATE_VIDEO
    else:
        await update.message.reply_text("✍️ ဇာတ်ကားအမည် (ဥပမာ - Inception 2010) ကို စာသားအနေဖြင့် ပို့ပေးပါ။")
        return CREATE_MOVIE_NAME

async def createpost_receive_movie_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movie_input = update.message.text.strip()
    context.user_data['createpost_movie_input'] = movie_input
    await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await update.message.reply_text("❌ ရှာမတွေ့ပါ။ ကျေးဇူးပြု၍ အင်္ဂလိပ်အမည်အပြည့်အစုံ (Year ပါ/မပါ) ထည့်ပါ။")
        return CREATE_MOVIE_NAME
    context.user_data['createpost_movie_data'] = movie
    formatted = format_movie_info_burmese(movie)
    if len(movie['plot']) > 1024:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
        if telegraph_url:
            formatted += f"\n\n📖 [ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်]({telegraph_url})"
    await update.message.reply_text(f"**✅ ဇာတ်ကားအချက်အလက် တွေ့ရှိပါသည်။**\n\n{formatted}", parse_mode='Markdown', disable_web_page_preview=True)
    await update.message.reply_text("🎬 ဆက်လက်ရန် ဇာတ်ကား Video ဖိုင်ကို ပို့ပေးပါ။")
    return CREATE_VIDEO

async def createpost_receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document and update.message.document.mime_type.startswith('video/'):
        video = update.message.document
    if not video:
        await update.message.reply_text("❌ Video file တစ်ခု ပို့ပေးပါ။")
        return CREATE_VIDEO
    
    payload = generate_payload()
    file_name = getattr(video, 'file_name', None) or f"movie_{payload[:8]}"
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    
    poster = context.user_data.get('createpost_poster')
    movie = context.user_data.get('createpost_movie_data')
    if not movie:
        await update.message.reply_text("❌ ဇာတ်ကားအချက်အလက် ပျောက်နေသည်။ /createpost ကို ထပ်မံစတင်ပါ။")
        return ConversationHandler.END
    
    formatted_info = format_movie_info_burmese(movie)
    keyboard = []
    keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)])
    if len(movie['plot']) > 1024:
        telegraph_url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']}) - ဇာတ်ညွှန်းအပြည့်", movie['plot'])
        if telegraph_url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်ဖတ်ရန်", url=telegraph_url)])
    if OTHER_CHANNELS:
        for idx, link in enumerate(OTHER_CHANNELS, 1):
            if idx == 1:
                keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
            elif idx == 2:
                keyboard.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
            elif idx == 3:
                keyboard.append([InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url=link)])
            else:
                keyboard.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
    if MUSIC_CHANNEL_LINK:
        keyboard.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_photo(
        photo=poster,
        caption=formatted_info,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    await update.message.reply_text(
        f"✅ **Post ပြင်ဆင်ပြီးပါပြီ။**\n\n"
        f"Deep Link: {deep_link}\n"
        f"ဤ Post ကို သင့် Channel တွင် Forward လုပ်ပါ။"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_createpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- /start handler ----------
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
        try:
            await update.message.reply_text(f"🎬 {file_name} ပို့ပေးနေပါပြီ...")
            video_msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"🎬 {file_name}")
            warning_text = "⚠️ ဤဖိုင်ကို 5 မိနစ်အတွင်း ဖျက်ပါမည်။ Saved Messages သို့ Forward လုပ်ပါ။"
            warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text)
            async def delete_after():
                await asyncio.sleep(300)
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                    await context.bot.delete_message(chat_id=user_id, message_id=video_msg.message_id)
                except:
                    pass
            asyncio.create_task(delete_after())
            add_user(user_id)
            increment_requests()
            reset_attempts(user_id)
        except Exception as e:
            await context.bot.send_message(chat_id=user_id, text=f"❌ Video ပို့ရာတွင် အမှား: {str(e)}")
    else:
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text(
                "🎬 **မင်္ဂလာပါ**\n\n"
                "ဤ Bot သည် Channel များအတွက် ဇာတ်ကားများ ဖြန့်ဝေရန် သုံးပါသည်။\n"
                "ဇာတ်ကားရယူရန် Channel ရှိ Post အောက်က ခလုတ်ကို နှိပ်ပါ။\n"
                "ပထမဆုံး လိုအပ်သော Channel 4 ခုလုံးကို ဝင်ရောက်ထားရပါမည်။\n\n"
                "✨ `/movie` command ဖြင့် ဇာတ်ကားအချက်အလက်များ ရှာဖွေနိုင်ပါသည်။",
                parse_mode="Markdown"
            )

# ---------- Admin Menu ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🆕 New Post", callback_data="menu_newpost")],
        [InlineKeyboardButton("🔗 New File", callback_data="menu_newfile")],
        [InlineKeyboardButton("📢 Channel Post", callback_data="menu_channelpost")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="menu_broadcast")],
        [InlineKeyboardButton("🚫 Blocklist", callback_data="menu_blocklist")],
        [InlineKeyboardButton("🔇 Mute", callback_data="menu_mute")],
        [InlineKeyboardButton("🔊 Unmute", callback_data="menu_unmute")],
        [InlineKeyboardButton("📦 Batch Link", callback_data="menu_batchlink")],
        [InlineKeyboardButton("🔄 Convert Old", callback_data="menu_convert_old")],
        [InlineKeyboardButton("➕ Create Post", callback_data="menu_createpost")]
    ]
    await update.message.reply_text("🤖 **Admin Menu**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    data = query.data
    if data == "menu_newpost":
        await query.edit_message_text("📸 `/newpost`")
    elif data == "menu_newfile":
        await query.edit_message_text("🔗 `/newfile`")
    elif data == "menu_channelpost":
        await query.edit_message_text("📢 `/channelpost`")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_requests = get_total_requests()
        await query.edit_message_text(f"📊 Users: {total_users}\nRequests: {total_requests}")
    elif data == "menu_broadcast":
        await query.edit_message_text("📢 `/broadcast <message>`")
    elif data == "menu_blocklist":
        blocked = get_blocked_users()
        msg = "🚫 Blocked:\n" + "\n".join([f"`{uid}`" for uid in blocked]) if blocked else "No blocked users."
        await query.edit_message_text(msg, parse_mode="Markdown")
    elif data == "menu_mute":
        maintenance_mode = True
        await query.edit_message_text("🔇 Muted")
    elif data == "menu_unmute":
        maintenance_mode = False
        await query.edit_message_text("🔊 Unmuted")
    elif data == "menu_batchlink":
        await query.edit_message_text("📦 `/batchlink`")
    elif data == "menu_convert_old":
        await query.edit_message_text("🔄 `/convert_old <limit>`")
    elif data == "menu_createpost":
        await query.edit_message_text("➕ `/createpost` command ကိုသုံးပါ။")

# ---------- Placeholder for other admin commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(f"👥 Users: {total_users}\n🎬 Requests: {total_requests}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("📢 /broadcast <message>")
        return
    users = get_all_users()
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 Sent to {count} users.")

async def blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    blocked = get_blocked_users()
    msg = "🚫 Blocked:\n" + "\n".join([f"`{uid}`" for uid in blocked]) if blocked else "No blocked users."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = context.args
    if not args:
        await update.message.reply_text("📌 /unblock <user_id>")
        return
    try:
        user_id = int(args[0])
        if is_user_blocked(user_id):
            unblock_user(user_id)
            await update.message.reply_text(f"✅ Unblocked `{user_id}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"ℹ️ `{user_id}` not blocked.", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ Invalid user_id.")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = True
    await update.message.reply_text("🔇 Muted.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = False
    await update.message.reply_text("🔊 Unmuted.")

async def newfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔗 /newfile - placeholder")
async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔗 /link - placeholder")
async def batchlink_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📦 /batchlink - placeholder")
async def channelpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 /channelpost - placeholder")
async def convert_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 /convert_old - placeholder")
async def test_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📡 /test_channel - placeholder")
async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Schedule - placeholder")
async def listschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 List schedule - placeholder")
async def cancelschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancel schedule - placeholder")
async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗑️ Delete file - placeholder")
async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ Delete all - placeholder")
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

# ---------- Application Setup ----------
application = Application.builder().token(TOKEN).build()

# Conversation Handlers
createpost_handler = ConversationHandler(
    entry_points=[CommandHandler('createpost', createpost_start)],
    states={
        CREATE_POSTER: [MessageHandler(filters.PHOTO, createpost_receive_poster)],
        CREATE_MOVIE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_receive_movie_name)],
        CREATE_VIDEO: [
            MessageHandler(filters.VIDEO, createpost_receive_video),
            MessageHandler(filters.Document.ALL, createpost_receive_video)
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_createpost)],
)

# Add all handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("movie", movie_command))
application.add_handler(createpost_handler)
application.add_handler(CommandHandler("newfile", newfile_command))
application.add_handler(CommandHandler("link", link_command))
application.add_handler(CommandHandler("menu", menu_command))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))
application.add_handler(CommandHandler("batchlink", batchlink_start))
application.add_handler(CommandHandler("channelpost", channelpost_start))
application.add_handler(CommandHandler("convert_old", convert_old))
application.add_handler(CommandHandler("test_channel", test_channel))
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
            logger.exception(f"Bot polling crashed: {e}. Restarting in 10s")
            import time
            time.sleep(10)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
