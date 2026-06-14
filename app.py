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
from pymongo import MongoClient
from telegraph import Telegraph
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

@app.route('/health')
def health():
    return "OK", 200

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI missing")
    sys.exit(1)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_share_bot_v2"]
file_store_collection = db["file_store"]
users_collection = db["users"]
stats_collection = db["stats"]
blocked_collection = db["blocked_users"]

def save_file_info(payload, file_id, file_name):
    file_store_collection.update_one({"payload": payload}, {"$set": {"file_id": file_id, "file_name": file_name}}, upsert=True)

def get_file_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    return {"file_id": doc["file_id"], "file_name": doc["file_name"]} if doc else None

def add_user(user_id):
    if not users_collection.find_one({"user_id": user_id}):
        users_collection.insert_one({"user_id": user_id, "first_seen": datetime.now(), "attempts": 0})

def get_all_users():
    return [doc["user_id"] for doc in users_collection.find({}, {"user_id": 1})]

def get_total_requests():
    doc = stats_collection.find_one({"_id": "total_requests"})
    return doc["count"] if doc else 0

def increment_requests():
    stats_collection.update_one({"_id": "total_requests"}, {"$inc": {"count": 1}}, upsert=True)

def is_user_blocked(user_id):
    return blocked_collection.find_one({"user_id": user_id}) is not None

def block_user(user_id):
    if not is_user_blocked(user_id):
        blocked_collection.insert_one({"user_id": user_id, "blocked_at": datetime.now()})

def unblock_user(user_id):
    blocked_collection.delete_one({"user_id": user_id})

def get_blocked_users():
    return [doc["user_id"] for doc in blocked_collection.find({}, {"user_id": 1})]

def get_attempt_count(user_id):
    doc = users_collection.find_one({"user_id": user_id})
    return doc.get("attempts", 0) if doc else 0

def increment_attempts(user_id):
    users_collection.update_one({"user_id": user_id}, {"$inc": {"attempts": 1}}, upsert=True)

def reset_attempts(user_id):
    users_collection.update_one({"user_id": user_id}, {"$set": {"attempts": 0}}, upsert=True)

# ---------- Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_TOKEN missing")
    sys.exit(1)

BOT_USERNAME = os.environ.get("BOT_USERNAME")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_ID", "").split(",") if x.strip()]

REQUIRED_CHANNELS = [
    {"id": "-1003753299714", "name": "🎬 ဇာတ်ကားချန်နယ် (ပင်မ)", "invite": "https://t.me/wznmoviescollector"},
    {"id": "-1003899625672", "name": "🎬 ဇာတ်ကားချန်နယ် (အရံ)", "invite": "https://t.me/moviesandseriesforallwzn"},
    {"id": "-1003792838735", "name": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ်", "invite": "https://t.me/everyboyhobby"},
    {"id": "-1003785717514", "name": "🎵 မြန်မာသီချင်းချန်နယ်", "invite": "https://t.me/wznmusiclibary"}
]

def is_admin(user_id):
    return user_id in ADMIN_IDS

def generate_payload():
    return secrets.token_urlsafe(16)

async def is_member_of_channel(user_id, channel_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def check_all_channels(user_id, context):
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

async def create_telegraph_page_movie(title, content):
    try:
        resp = await asyncio.to_thread(telegraph.create_page, title=title, html_content=f"<p>{content.replace(chr(10), '<br>')}</p>")
        return resp['url']
    except:
        return None

# ---------- Movie Info ----------
OMDB_API_KEY = "5025f95c"
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
    except:
        return text

def get_movie_info(movie_input):
    try:
        params = {'t': movie_input, 'apikey': OMDB_API_KEY, 'plot': 'full'}
        resp = requests.get("http://www.omdbapi.com/", params=params, timeout=10).json()
        if resp.get('Response') == 'False':
            return None
        plot_en = resp.get('Plot', 'N/A')
        plot_my = translate_text(plot_en) if len(plot_en) < 5000 else plot_en
        runtime_raw = resp.get('Runtime', 'N/A')
        runtime_my = runtime_raw
        if 'min' in runtime_raw:
            try:
                minutes = int(runtime_raw.split()[0])
                hours = minutes // 60
                mins = minutes % 60
                runtime_my = f"{hours}နာရီ {mins}မိနစ်" if hours else f"{mins}မိနစ်"
            except:
                pass
        return {
            'title': resp.get('Title', 'N/A'),
            'year': resp.get('Year', 'N/A'),
            'genre': translate_text(resp.get('Genre', 'N/A')),
            'actors': translate_text(resp.get('Actors', 'N/A')),
            'director': translate_text(resp.get('Director', 'N/A')),
            'runtime': runtime_my,
            'country': translate_text(resp.get('Country', 'N/A')),
            'language': translate_text(resp.get('Language', 'N/A')),
            'imdb_rating': resp.get('imdbRating', 'N/A'),
            'imdb_votes': resp.get('imdbVotes', 'N/A'),
            'plot': plot_my,
            'poster': resp.get('Poster', 'N/A'),
        }
    except Exception as e:
        logger.error(f"OMDb error: {e}")
        return None

def format_movie_info_plain(movie):
    return f"""🎬 {movie['title']} ({movie['year']})

📌 အမျိုးအစား – {movie['genre']}
🎭 သရုပ်ဆောင်များ – {movie['actors']}
🎥 ဒါရိုက်တာ – {movie['director']}
⏱️ ကြာချိန် – {movie['runtime']}
🌍 နိုင်ငံ – {movie['country']}
🗣️ ဘာသာစကား – {movie['language']}
⭐ IMDb – {movie['imdb_rating']}/10
🗳️ မဲ – {movie['imdb_votes']}

📖 ဇာတ်လမ်းအကျဉ်း – {movie['plot']}"""

# ========== /movie ==========
async def movie_command(update, context):
    if not context.args:
        await update.message.reply_text("ဥပမာ - /movie Inception 2010")
        return
    movie_input = ' '.join(context.args)
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကိုရှာနေသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ မတွေ့ပါ။ အင်္ဂလိပ်အမည်ဖြင့်ထပ်စမ်းပါ။")
        return
    text = format_movie_info_plain(movie)
    keyboard = []
    if len(movie['plot']) > 800:
        url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']})", movie['plot'])
        if url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

# ========== /createpost conversation ==========
CREATE_POSTER, CREATE_MOVIE_NAME, CREATE_VIDEO = range(3)

async def createpost_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only")
        return ConversationHandler.END
    await update.message.reply_text("📸 Poster ပုံတစ်ပုံပို့ပါ။ (Caption တွင်ဇာတ်ကားအမည်ထည့်နိုင်သည်)")
    return CREATE_POSTER

async def createpost_receive_poster(update, context):
    if not update.message.photo:
        await update.message.reply_text("ပုံပို့ပါ")
        return CREATE_POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    if update.message.caption:
        context.user_data['movie_name'] = update.message.caption.strip()
        movie = get_movie_info(context.user_data['movie_name'])
        if movie:
            context.user_data['movie_data'] = movie
            await update.message.reply_text(f"✅ တွေ့ပါသည်။\n{format_movie_info_plain(movie)}")
            await update.message.reply_text("🎬 Video ဖိုင်ပို့ပါ။")
            return CREATE_VIDEO
        else:
            await update.message.reply_text("ဇာတ်ကားအမည်အတိအကျထည့်ပါ။ /cancel")
            return CREATE_MOVIE_NAME
    else:
        await update.message.reply_text("ဇာတ်ကားအမည်ကိုစာသားပို့ပါ။")
        return CREATE_MOVIE_NAME

async def createpost_receive_movie_name(update, context):
    movie_input = update.message.text.strip()
    movie = get_movie_info(movie_input)
    if not movie:
        await update.message.reply_text("မတွေ့ပါ။ အင်္ဂလိပ်အမည်ထည့်ပါ။ /cancel")
        return CREATE_MOVIE_NAME
    context.user_data['movie_data'] = movie
    await update.message.reply_text(f"✅ {format_movie_info_plain(movie)}")
    await update.message.reply_text("🎬 Video ဖိုင်ပို့ပါ။")
    return CREATE_VIDEO

async def createpost_receive_video(update, context):
    video = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "movie"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "movie"
    if not video:
        await update.message.reply_text("Video ဖိုင် (mp4/mkv) ပို့ပါ။")
        return CREATE_VIDEO

    if not BOT_USERNAME:
        await update.message.reply_text("BOT_USERNAME missing")
        return ConversationHandler.END

    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    poster = context.user_data.get('poster')
    movie = context.user_data.get('movie_data')
    if not poster or not movie:
        await update.message.reply_text("Poster သို့မဟုတ် ဇာတ်ကားအချက်အလက်ပျောက်နေ။ /createpost ပြန်စပါ။")
        return ConversationHandler.END

    text = format_movie_info_plain(movie)
    keyboard = [[InlineKeyboardButton("🎬 ရယူရန်", url=deep_link)]]
    if len(movie['plot']) > 800:
        url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']})", movie['plot'])
        if url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])

    await update.message.reply_photo(photo=poster, caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("✅ Post ပြင်ဆင်ပြီး။ Channel တွင် forward လုပ်ပါ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_createpost(update, context):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== /newfile ==========
async def newfile_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only")
        return
    await update.message.reply_text("📤 Video ဖိုင်ပို့ပါ။ Deep link ထုတ်ပေးမည်။")
    context.user_data['waiting_newfile'] = True

async def newfile_receive_video(update, context):
    if not context.user_data.get('waiting_newfile'):
        return
    if not is_admin(update.effective_user.id):
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
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return

    if not BOT_USERNAME:
        await update.message.reply_text("BOT_USERNAME missing")
        context.user_data.pop('waiting_newfile', None)
        return

    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    await update.message.reply_text(f"Deep Link:\n{deep_link}\n\n{file_name}")
    context.user_data.pop('waiting_newfile', None)

# ========== /channelpost conversation ==========
CHANNEL_PHOTO, CHANNEL_TEXT, CHANNEL_VIDEO = range(3)

async def channelpost_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only")
        return ConversationHandler.END
    await update.message.reply_text("📸 Poster ပုံပို့ပါ။")
    return CHANNEL_PHOTO

async def channelpost_photo(update, context):
    if not update.message.photo:
        await update.message.reply_text("ပုံပို့ပါ")
        return CHANNEL_PHOTO
    context.user_data['channel_photo'] = update.message.photo[-1].file_id
    await update.message.reply_text("✍️ စာသား (ဇာတ်ကားအချက်အလက်) ပို့ပါ။")
    return CHANNEL_TEXT

async def channelpost_text(update, context):
    context.user_data['channel_caption'] = update.message.text
    await update.message.reply_text("🎬 Video ဖိုင်ပို့ပါ။")
    return CHANNEL_VIDEO

async def channelpost_video(update, context):
    video = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "movie"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "movie"
    if not video:
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return CHANNEL_VIDEO

    if not BOT_USERNAME:
        await update.message.reply_text("BOT_USERNAME missing")
        return ConversationHandler.END

    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    photo_id = context.user_data.get('channel_photo')
    caption = context.user_data.get('channel_caption', '')
    button = InlineKeyboardButton("🎬 ရယူရန်", url=deep_link)
    markup = InlineKeyboardMarkup([[button]])

    # POST_CHANNELS ကိုသင်သတ်မှတ်ထားရမည် (ဥပမာ ["-100xxxx"])
    for ch_id in POST_CHANNELS:
        try:
            await context.bot.send_photo(chat_id=ch_id, photo=photo_id, caption=caption, reply_markup=markup)
        except Exception as e:
            logger.error(f"Send to {ch_id} failed: {e}")
    await update.message.reply_text(f"✅ ပြီးပါပြီ။ Channel {len(POST_CHANNELS)} ခုသို့တင်ခဲ့သည်။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_channelpost(update, context):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== /batchlink ==========
async def batchlink_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only")
        return
    context.user_data['batch'] = []
    await update.message.reply_text("Video များဆက်တိုက်ပို့ပါ။ ပြီးလျှင် /done")

async def batchlink_receive(update, context):
    if not is_admin(update.effective_user.id):
        return
    if 'batch' not in context.user_data:
        return
    video = None
    name = None
    if update.message.video:
        video = update.message.video
        name = video.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            name = doc.file_name or "video"
    if not video:
        await update.message.reply_text("Video ပို့ပါ")
        return
    context.user_data['batch'].append({"file_id": video.file_id, "name": name})
    await update.message.reply_text(f"✅ #{len(context.user_data['batch'])}: {name}")

async def batchlink_done(update, context):
    if not is_admin(update.effective_user.id):
        return
    batch = context.user_data.get('batch', [])
    if not batch:
        await update.message.reply_text("ဗီဒီယိုမရှိပါ")
        return
    if not BOT_USERNAME:
        await update.message.reply_text("BOT_USERNAME missing")
        return
    results = []
    for v in batch:
        payload = generate_payload()
        save_file_info(payload, v["file_id"], v["name"])
        link = create_deep_linked_url(BOT_USERNAME, payload)
        results.append(f"{v['name']}: {link}")
    text = "Batch Links:\n" + "\n".join(results)
    if len(text) > 4000:
        text = text[:4000] + "..."
    await update.message.reply_text(text)
    context.user_data.clear()

async def cancel_batch(update, context):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")

# ========== /start ==========
async def start(update, context):
    user_id = update.effective_user.id
    if context.args:
        payload = context.args[0]
        info = get_file_info(payload)
        if not info:
            await update.message.reply_text("လင့်မမှန်ကန်ပါ။")
            return
        if is_user_blocked(user_id):
            await update.message.reply_text("သင်ပိတ်ခံထားရသည်။")
            return
        ok, _ = await check_all_channels(user_id, context)
        if not ok:
            attempts = get_attempt_count(user_id) + 1
            increment_attempts(user_id)
            if attempts >= 10:
                block_user(user_id)
                await update.message.reply_text("၁၀ကြိမ်ထက်ကျော်သောကြောင့်ပိတ်ပါသည်။")
                return
            msg = "အောက်ပါ channel များအားလုံးကိုဝင်ပါ:\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• {ch['name']}: {ch['invite']}\n"
            msg += f"\nအကြိမ်ရေ: {attempts}/10"
            await update.message.reply_text(msg)
            return
        try:
            await update.message.reply_text(f"🎬 {info['file_name']} ပို့နေပါပြီ...")
            await context.bot.send_video(chat_id=user_id, video=info['file_id'], caption=f"🎬 {info['file_name']}")
            add_user(user_id)
            increment_requests()
            reset_attempts(user_id)
        except Exception as e:
            await update.message.reply_text(f"မပို့နိုင်ပါ: {e}")
    else:
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text("🎬 မင်္ဂလာပါ။ ဇာတ်ကားရယူရန် channel post ရှိ ခလုတ်ကိုနှိပ်ပါ။ /movie ဖြင့်ရှာနိုင်သည်။")

# ---------- Admin Menu ----------
async def show_menu(update, context):
    keyboard = [
        [InlineKeyboardButton("➕ Postအသစ်", callback_data="menu_createpost")],
        [InlineKeyboardButton("🔗 Deep Linkထုတ်", callback_data="menu_newfile")],
        [InlineKeyboardButton("📦 Batch Link", callback_data="menu_batch")],
        [InlineKeyboardButton("📢 Channelတိုက်ရိုက်", callback_data="menu_channel")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("🚫 Blockစာရင်း", callback_data="menu_block")],
        [InlineKeyboardButton("🔇 ပိတ်/ဖွင့်", callback_data="menu_mute")],
    ]
    await update.message.reply_text("Admin Menu", reply_markup=InlineKeyboardMarkup(keyboard))

async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("ခွင့်မပြု")
        return
    data = query.data
    if data == "menu_createpost":
        await query.edit_message_text("/createpost သုံးပါ။")
    elif data == "menu_newfile":
        await query.edit_message_text("/newfile သုံးပါ။")
    elif data == "menu_batch":
        await query.edit_message_text("/batchlink သုံးပါ။")
    elif data == "menu_channel":
        await query.edit_message_text("/channelpost သုံးပါ။")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await query.edit_message_text(f"👥 {total_users} users\n🎬 {total_req} requests")
    elif data == "menu_block":
        blocked = get_blocked_users()
        msg = "Blocked:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "Empty"
        await query.edit_message_text(msg)
    elif data == "menu_mute":
        current = context.bot_data.get('muted', False)
        await context.bot.set_bot_data({'muted': not current})
        await query.edit_message_text("🔇 Bot ပိတ်ထားပြီ" if not current else "🔊 Bot ဖွင့်ထားပြီ")

# ---------- Other commands ----------
async def stats(update, context):
    if is_admin(update.effective_user.id):
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await update.message.reply_text(f"👥 {total_users} users\n🎬 {total_req} requests")

async def broadcast(update, context):
    if not is_admin(update.effective_user.id):
        return
    msg = ' '.join(context.args)
    if not msg:
        await update.message.reply_text("/broadcast <message>")
        return
    users = get_all_users()
    cnt = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            cnt += 1
        except:
            pass
    await update.message.reply_text(f"Sent to {cnt} users")

async def blocklist(update, context):
    if is_admin(update.effective_user.id):
        blocked = get_blocked_users()
        await update.message.reply_text("Blocked:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "None")

async def unblock(update, context):
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

async def mute(update, context):
    if is_admin(update.effective_user.id):
        await context.bot.set_bot_data({'muted': True})
        await update.message.reply_text("Bot muted")

async def unmute(update, context):
    if is_admin(update.effective_user.id):
        await context.bot.set_bot_data({'muted': False})
        await update.message.reply_text("Bot unmuted")

async def menu_command(update, context):
    if is_admin(update.effective_user.id):
        await show_menu(update, context)
    else:
        await update.message.reply_text("Admin only")

# ---------- Placeholders ----------
async def placeholder(update, context):
    await update.message.reply_text("Not implemented yet")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# Conversations
createpost_conv = ConversationHandler(
    entry_points=[CommandHandler('createpost', createpost_start)],
    states={
        CREATE_POSTER: [MessageHandler(filters.PHOTO, createpost_receive_poster)],
        CREATE_MOVIE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_receive_movie_name)],
        CREATE_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.ALL, createpost_receive_video)],
    },
    fallbacks=[CommandHandler('cancel', cancel_createpost)],
)

channelpost_conv = ConversationHandler(
    entry_points=[CommandHandler('channelpost', channelpost_start)],
    states={
        CHANNEL_PHOTO: [MessageHandler(filters.PHOTO, channelpost_photo)],
        CHANNEL_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, channelpost_text)],
        CHANNEL_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.ALL, channelpost_video)],
    },
    fallbacks=[CommandHandler('cancel', cancel_channelpost)],
)

batch_conv = ConversationHandler(
    entry_points=[CommandHandler('batchlink', batchlink_start)],
    states={0: [MessageHandler(filters.VIDEO | filters.Document.ALL, batchlink_receive)]},
    fallbacks=[CommandHandler('done', batchlink_done), CommandHandler('cancel', cancel_batch)],
)

application.add_handler(createpost_conv)
application.add_handler(channelpost_conv)
application.add_handler(batch_conv)
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("movie", movie_command))
application.add_handler(CommandHandler("newfile", newfile_command))
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, newfile_receive_video))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("menu", menu_command))
# Placeholders for other commands
for cmd in ["link", "convert_old", "test_channel", "schedule", "listschedule", "cancelschedule", "delete", "deleteall"]:
    application.add_handler(CommandHandler(cmd, placeholder))
application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

def run_bot():
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.info("Bot started")
            application.run_polling()
        except Exception as e:
            logger.exception(f"Crashed: {e}. Restart in 10s")
            import time
            time.sleep(10)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
