import os
import asyncio
import threading
import logging
import sys
import secrets
import re
from datetime import datetime
from flask import Flask
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient
from telegraph import Telegraph
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running (polling mode)"

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI not set")
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
    file_store_collection.update_one({"payload": payload}, {"$set": {"file_id": file_id, "file_name": file_name}}, upsert=True)
    logger.info(f"Saved: payload={payload}, file_id={file_id}")

def get_file_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    if doc:
        return {"file_id": doc["file_id"], "file_name": doc["file_name"]}
    return None

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

# ---------- Telegram Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_TOKEN not set")
    sys.exit(1)

BOT_USERNAME = os.environ.get("BOT_USERNAME")
if not BOT_USERNAME:
    logger.error("BOT_USERNAME not set! Deep links will not work.")
    # အရေးကြီး: BOT_USERNAME မှာ @ မပါဘဲ ထည့်ပါ (ဥပမာ wznsendmovbot)

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
    return len(missing) == 0, missing

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
    except:
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
    except:
        return text

def contains_burmese(text):
    return bool(re.search(r'[\u1000-\u109F]', text))

def normalize_movie_name(text):
    if contains_burmese(text):
        try:
            return translator_my_to_en.translate(text).strip()
        except:
            return text
    return text

# ---------- Movie Info ----------
OMDB_API_KEY = "5025f95c"

def get_movie_info(movie_input):
    name = normalize_movie_name(movie_input)
    year_match = re.search(r'[\(\[]?(\d{4})[\)\]]?', name)
    if year_match:
        year = year_match.group(1)
        name = re.sub(r'[\(\[]?\d{4}[\)\]]?\s*$', '', name).strip()
    else:
        year = None
    params = {'t': name, 'apikey': OMDB_API_KEY, 'plot': 'full'}
    if year:
        params['y'] = year
    try:
        resp = requests.get("http://www.omdbapi.com/", params=params, timeout=10).json()
        if resp.get('Response') == 'False':
            return None
        plot_en = resp.get('Plot', 'N/A')
        plot_my = translate_text(plot_en) if len(plot_en) < 5000 else plot_en
        runtime_raw = resp.get('Runtime', 'N/A')
        runtime = runtime_raw
        if 'min' in runtime_raw:
            try:
                minutes = int(runtime_raw.split()[0])
                hours = minutes // 60
                mins = minutes % 60
                runtime = f"{hours}နာရီ {mins}မိနစ်" if hours else f"{mins}မိနစ်"
            except:
                pass
        return {
            'title': resp.get('Title', 'N/A'),
            'year': resp.get('Year', 'N/A'),
            'genre': translate_text(resp.get('Genre', 'N/A')),
            'actors': translate_text(resp.get('Actors', 'N/A')),
            'director': translate_text(resp.get('Director', 'N/A')),
            'runtime': runtime,
            'country': translate_text(resp.get('Country', 'N/A')),
            'language': translate_text(resp.get('Language', 'N/A')),
            'imdb_rating': resp.get('imdbRating', 'N/A'),
            'imdb_votes': resp.get('imdbVotes', 'N/A'),
            'plot': plot_my,
            'poster': resp.get('Poster', 'N/A'),
        }
    except:
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
        await update.message.reply_text("ဥပမာ - /movie Inception 2010 သို့မဟုတ် အင်စက်ပရှင်")
        return
    movie_input = ' '.join(context.args)
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ ရှာမတွေ့ပါ။ အင်္ဂလိပ်အမည် (သို့) မြန်မာအမည်ဖြင့် ထပ်စမ်းပါ။")
        return
    text = format_movie_info_plain(movie)
    keyboard = []
    if len(movie['plot']) > 800:
        url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']})", movie['plot'])
        if url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None, disable_web_page_preview=True)

# ========== /createpost (in-memory state) ==========
CREATE_STATES = {}

async def createpost_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /createpost ဇာတ်ကားအမည် ထုတ်ဝေနှစ်\nဥပမာ - /createpost Inception 2010")
        return
    movie_input = ' '.join(context.args)
    movie = get_movie_info(movie_input)
    if not movie:
        await update.message.reply_text("❌ ဇာတ်ကား ရှာမတွေ့ပါ။ အင်္ဂလိပ်အမည်အပြည့် ထည့်ပါ။")
        return
    user_id = update.effective_user.id
    CREATE_STATES[user_id] = {'movie': movie, 'step': 'waiting_poster'}
    await update.message.reply_text(f"✅ ဇာတ်ကားတွေ့ရှိပါသည်။\n{format_movie_info_plain(movie)}\n\n📸 ယခု Poster ပုံကို ပို့ပေးပါ။")

async def handle_poster(update, context):
    user_id = update.effective_user.id
    if user_id not in CREATE_STATES:
        return
    if CREATE_STATES[user_id]['step'] != 'waiting_poster':
        return
    if not update.message.photo:
        await update.message.reply_text("ကျေးဇူးပြု၍ Poster ပုံတစ်ပုံ ပို့ပေးပါ။")
        return
    CREATE_STATES[user_id]['poster'] = update.message.photo[-1].file_id
    CREATE_STATES[user_id]['step'] = 'waiting_video'
    await update.message.reply_text("✅ Poster လက်ခံပြီး။\n🎬 ယခု Video ဖိုင်ကို ပို့ပေးပါ။")

async def handle_video_for_create(update, context):
    user_id = update.effective_user.id
    if user_id not in CREATE_STATES:
        return
    if CREATE_STATES[user_id]['step'] != 'waiting_video':
        return
    video = None
    file_name = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "movie"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "movie"
    if not video:
        await update.message.reply_text("❌ Video ဖိုင် (mp4, mkv, avi, mov) တစ်ခု ပို့ပေးပါ။")
        return
    if not BOT_USERNAME:
        await update.message.reply_text("❌ BOT_USERNAME မသတ်မှတ်ထားပါ။")
        return
    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    movie = CREATE_STATES[user_id]['movie']
    poster = CREATE_STATES[user_id]['poster']
    text = format_movie_info_plain(movie)
    keyboard = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)]]
    if len(movie['plot']) > 800:
        url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']})", movie['plot'])
        if url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    await update.message.reply_photo(photo=poster, caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("✅ **Post ပြင်ဆင်ပြီးပါပြီ။**\n\nဤ Post ကို သင့် Channel တွင် Forward လုပ်ပါ။")
    del CREATE_STATES[user_id]

# ========== /newfile ==========
async def newfile_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    await update.message.reply_text("📤 Video ဖိုင်တစ်ခု ပို့ပေးပါ။ (Deep Link ထုတ်ပေးပါမည်)")
    context.user_data['newfile_waiting'] = True

async def newfile_receive(update, context):
    if not context.user_data.get('newfile_waiting'):
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
        await update.message.reply_text("❌ Video ဖိုင်တစ်ခု ပို့ပေးပါ။")
        return
    if not BOT_USERNAME:
        await update.message.reply_text("❌ BOT_USERNAME မသတ်မှတ်ထားပါ။")
        context.user_data.pop('newfile_waiting', None)
        return
    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    await update.message.reply_text(f"🔗 **Deep Link**\n\n{deep_link}\n\n`{file_name}` အတွက်ဖြစ်ပါသည်။\n(Channel 4 ခုလုံးဝင်ထားရန် လိုအပ်)", parse_mode='Markdown')
    context.user_data.pop('newfile_waiting', None)

# ========== /batchlink ==========
async def batchlink_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    context.user_data['batch_videos'] = []
    await update.message.reply_text("📦 **Batch Deep Link Generator**\n\nVideo ဖိုင်များကို တစ်ခုချင်းစီ ဆက်တိုက်ပို့ပါ။ ပြီးပါက `/done` ရိုက်ပါ။")

async def batchlink_receive_video(update, context):
    if not is_admin(update.effective_user.id):
        return
    if 'batch_videos' not in context.user_data:
        await update.message.reply_text("/batchlink ဖြင့် စတင်ပါ။")
        return
    video = None
    file_name = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "movie"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "movie"
    if not video:
        await update.message.reply_text("Video ဖိုင်တစ်ခု ပို့ပေးပါ။")
        return
    context.user_data['batch_videos'].append({"file_id": video.file_id, "file_name": file_name})
    await update.message.reply_text(f"✅ ဖိုင် #{len(context.user_data['batch_videos'])}: `{file_name}` လက်ခံပြီး။\n(ဆက်ပို့ရန် သို့မဟုတ် `/done`)", parse_mode='Markdown')

async def batchlink_done(update, context):
    if not is_admin(update.effective_user.id):
        return
    videos = context.user_data.get('batch_videos', [])
    if not videos:
        await update.message.reply_text("❌ Video များမရှိပါ။")
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
        text = text[:4000] + "\n..."
    await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)
    context.user_data.clear()

async def cancel_batchlink(update, context):
    context.user_data.clear()
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")

# ========== /start ==========
async def start(update, context):
    user_id = update.effective_user.id
    if context.args:
        payload = context.args[0]
        file_info = get_file_info(payload)
        if not file_info:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return
        if is_user_blocked(user_id):
            await update.message.reply_text("🔒 သင်သည် block ခံထားရပါသည်။")
            return
        ok, missing = await check_all_channels(user_id, context)
        if not ok:
            attempts = get_attempt_count(user_id) + 1
            increment_attempts(user_id)
            if attempts >= 10:
                block_user(user_id)
                await update.message.reply_text("🚫 ၁၀ ကြိမ်အထက် မအောင်မြင်သောကြောင့် block ခံရပါသည်။")
                return
            msg = "🎬 ဇာတ်ကားဖိုင်ရယူရန် အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါ။\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• {ch['name']}: {ch['invite']}\n"
            msg += f"\n⚠️ သင်သည် ဤလင့်ကို **{attempts}/10** ကြိမ် နှိပ်ပြီးဖြစ်သည်။"
            await update.message.reply_text(msg, disable_web_page_preview=True)
            return
        file_id = file_info["file_id"]
        file_name = file_info["file_name"]
        try:
            await update.message.reply_text(f"🎬 {file_name} ပို့ပေးနေပါပြီ...")
            await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"🎬 {file_name}")
            add_user(user_id)
            increment_requests()
            reset_attempts(user_id)
        except Exception as e:
            await update.message.reply_text(f"❌ Video ပို့ရာတွင် အမှား: {str(e)}")
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
async def show_menu(update, context):
    keyboard = [
        [InlineKeyboardButton("➕ Post အသစ်ဖန်တီးရန်", callback_data="menu_createpost")],
        [InlineKeyboardButton("🔗 Deep Link အသစ်ထုတ်ရန်", callback_data="menu_newfile")],
        [InlineKeyboardButton("📦 Batch Link (အစုလိုက်)", callback_data="menu_batchlink")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("🚫 Block စာရင်း", callback_data="menu_blocklist")],
        [InlineKeyboardButton("🔇 Bot ပိတ်ရန်", callback_data="menu_mute")],
        [InlineKeyboardButton("🔊 Bot ဖွင့်ရန်", callback_data="menu_unmute")],
    ]
    await update.message.reply_text("🤖 Admin Menu", reply_markup=InlineKeyboardMarkup(keyboard))

async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin မဟုတ်ပါ။")
        return
    data = query.data
    if data == "menu_createpost":
        await query.edit_message_text("/createpost ဇာတ်ကားအမည် နှစ်")
    elif data == "menu_newfile":
        await query.edit_message_text("/newfile ပြီးမှ video ဖိုင်ပို့ပါ။")
    elif data == "menu_batchlink":
        await query.edit_message_text("/batchlink ပြီးမှ video များဆက်တိုက်ပို့ပါ။")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await query.edit_message_text(f"👥 အသုံးပြုသူ: {total_users}\n🎬 တောင်းဆိုမှု: {total_req}")
    elif data == "menu_blocklist":
        blocked = get_blocked_users()
        msg = "Blocked users:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "ဘယ်သူမှမရှိပါ။"
        await query.edit_message_text(msg)
    elif data == "menu_mute":
        await context.bot.set_bot_data({'muted': True})
        await query.edit_message_text("🔇 Bot ကို ယာယီပိတ်ထားပါသည်။")
    elif data == "menu_unmute":
        await context.bot.set_bot_data({'muted': False})
        await query.edit_message_text("🔊 Bot ပုံမှန်အလုပ်လုပ်ပါပြီ။")

async def stats(update, context):
    if is_admin(update.effective_user.id):
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await update.message.reply_text(f"👥 အသုံးပြုသူ: {total_users}\n🎬 တောင်းဆိုမှု: {total_req}")

async def blocklist(update, context):
    if is_admin(update.effective_user.id):
        blocked = get_blocked_users()
        msg = "Blocked users:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "None"
        await update.message.reply_text(msg)

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
            await update.message.reply_text(f"✅ User {uid} ကို unblock လုပ်ပြီး။")
        else:
            await update.message.reply_text(f"User {uid} သည် block မခံရပါ။")
    except:
        await update.message.reply_text("User ID ဂဏန်းသာ ထည့်ပါ။")

async def mute(update, context):
    if is_admin(update.effective_user.id):
        await context.bot.set_bot_data({'muted': True})
        await update.message.reply_text("🔇 Bot ကို ယာယီပိတ်ထားပါသည်။")

async def unmute(update, context):
    if is_admin(update.effective_user.id):
        await context.bot.set_bot_data({'muted': False})
        await update.message.reply_text("🔊 Bot ပုံမှန်အလုပ်လုပ်ပါပြီ။")

async def menu_command(update, context):
    if is_admin(update.effective_user.id):
        await show_menu(update, context)
    else:
        await update.message.reply_text("Admin only")

# ---------- Placeholders ----------
async def channelpost(update, context): await update.message.reply_text("Not implemented yet")
async def convert_old(update, context): await update.message.reply_text("Not implemented yet")
async def test_channel(update, context): await update.message.reply_text("Not implemented yet")
async def schedule(update, context): await update.message.reply_text("Not implemented yet")
async def listschedule(update, context): await update.message.reply_text("Not implemented yet")
async def cancelschedule(update, context): await update.message.reply_text("Not implemented yet")
async def delete_file(update, context): await update.message.reply_text("Not implemented yet")
async def deleteall(update, context): await update.message.reply_text("Not implemented yet")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("movie", movie_command))
application.add_handler(CommandHandler("createpost", createpost_command))
application.add_handler(MessageHandler(filters.PHOTO, handle_poster))
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video_for_create))
application.add_handler(CommandHandler("newfile", newfile_command))
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, newfile_receive))
application.add_handler(CommandHandler("batchlink", batchlink_start))
application.add_handler(CommandHandler("done", batchlink_done))
application.add_handler(CommandHandler("cancel", cancel_batchlink))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("menu", menu_command))
application.add_handler(CommandHandler("channelpost", channelpost))
application.add_handler(CommandHandler("convert_old", convert_old))
application.add_handler(CommandHandler("test_channel", test_channel))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))
application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

# ---------- Polling with correct asyncio ----------
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    # Flask ကို thread နဲ့ run
    threading.Thread(target=run_flask, daemon=True).start()
    # polling ကို asyncio နဲ့ မှန်မှန်ကန်ကန် run
    asyncio.run(application.run_polling())
