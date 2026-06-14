import os
import asyncio
import logging
import sys
import secrets
import re
from datetime import datetime
from flask import Flask
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler, ConversationHandler
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
    logger.error("BOT_USERNAME not set")
    sys.exit(1)

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

async def is_member_of_channel(user_id, channel_id, bot):
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def check_all_channels(user_id, bot):
    missing = []
    for ch in REQUIRED_CHANNELS:
        if not await is_member_of_channel(user_id, ch["id"], bot):
            missing.append(ch)
    return len(missing) == 0, missing

# ---------- Telegraph ----------
telegraph = Telegraph()
try:
    telegraph.create_account(short_name=BOT_USERNAME)
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
        await update.message.reply_text("ဥပမာ - /movie Inception 2010")
        return
    movie_input = ' '.join(context.args)
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ ရှာမတွေ့ပါ။")
        return
    text = format_movie_info_plain(movie)
    keyboard = []
    if len(movie['plot']) > 800:
        url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']})", movie['plot'])
        if url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

# ========== /createpost Conversation ==========
CREATE_POSTER, CREATE_MOVIE_NAME, CREATE_VIDEO = range(3)

async def createpost_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ဇာတ်ကား Poster ပုံတစ်ပုံ ပို့ပေးပါ။")
    return CREATE_POSTER

async def createpost_receive_poster(update, context):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပေးပါ။")
        return CREATE_POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("✍️ ဇာတ်ကားအမည် (ဥပမာ - Inception 2010) ကို ပို့ပေးပါ။")
    return CREATE_MOVIE_NAME

async def createpost_receive_movie_name(update, context):
    movie_input = update.message.text.strip()
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကို ရှာဖွေနေပါသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("❌ ရှာမတွေ့ပါ။ /cancel ဖြင့် ပြန်စပါ။")
        return CREATE_MOVIE_NAME
    context.user_data['movie_data'] = movie
    await msg.edit_text(f"✅ တွေ့ရှိပါသည်။\n{format_movie_info_plain(movie)}")
    await update.message.reply_text("🎬 Video ဖိုင်ကို ပို့ပေးပါ။")
    return CREATE_VIDEO

async def createpost_receive_video(update, context):
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
        await update.message.reply_text("❌ Video ဖိုင် (mp4, mkv, avi, mov) ပို့ပါ။")
        return CREATE_VIDEO

    poster = context.user_data.get('poster')
    movie = context.user_data.get('movie_data')
    if not poster or not movie:
        await update.message.reply_text("အချက်အလက်ပျောက်နေသည်။ /createpost ဖြင့် ပြန်စပါ။")
        return ConversationHandler.END

    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    text = format_movie_info_plain(movie)
    keyboard = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)]]
    if len(movie['plot']) > 800:
        url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']})", movie['plot'])
        if url:
            keyboard.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])

    await update.message.reply_photo(photo=poster, caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("✅ **Post ပြင်ဆင်ပြီးပါပြီ။**\nForward to channel.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_createpost(update, context):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ========== /newfile ==========
async def newfile_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    await update.message.reply_text("📤 Video ဖိုင်ပို့ပါ။ (Deep Link ထုတ်ပေးမည်)")
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
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return
    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    await update.message.reply_text(f"🔗 Deep Link:\n{deep_link}\n\n{file_name}")
    context.user_data.pop('newfile_waiting', None)

# ========== /batchlink ==========
async def batchlink_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    context.user_data['batch_videos'] = []
    await update.message.reply_text("📦 Batch Link\nVideo များဆက်တိုက်ပို့ပါ။ ပြီးပါက /done")

async def batchlink_receive(update, context):
    if not is_admin(update.effective_user.id):
        return
    if 'batch_videos' not in context.user_data:
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
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return
    context.user_data['batch_videos'].append({"file_id": video.file_id, "name": name})
    await update.message.reply_text(f"✅ #{len(context.user_data['batch_videos'])}: {name}")

async def batchlink_done(update, context):
    if not is_admin(update.effective_user.id):
        return
    videos = context.user_data.get('batch_videos', [])
    if not videos:
        await update.message.reply_text("ဗီဒီယိုမရှိပါ။")
        return
    results = []
    for v in videos:
        payload = generate_payload()
        save_file_info(payload, v["file_id"], v["name"])
        link = create_deep_linked_url(BOT_USERNAME, payload)
        results.append(f"• {v['name']}\n  {link}")
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
            await update.message.reply_text("လင့်မမှန်ပါ။")
            return
        if is_user_blocked(user_id):
            await update.message.reply_text("သင်ပိတ်ခံထားရသည်။")
            return
        ok, _ = await check_all_channels(user_id, context.bot)
        if not ok:
            attempts = get_attempt_count(user_id) + 1
            increment_attempts(user_id)
            if attempts >= 10:
                block_user(user_id)
                await update.message.reply_text("၁၀ကြိမ်ကျော်သောကြောင့်ပိတ်ပါသည်။")
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
            await update.message.reply_text("🎬 မင်္ဂလာပါ။ /movie ဖြင့်ရှာနိုင်သည်။")

# ---------- Admin Menu ----------
async def show_menu(update, context):
    keyboard = [
        [InlineKeyboardButton("➕ Postအသစ်", callback_data="menu_createpost")],
        [InlineKeyboardButton("🔗 Deep Link", callback_data="menu_newfile")],
        [InlineKeyboardButton("📦 Batch Link", callback_data="menu_batch")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("🚫 Blockစာရင်း", callback_data="menu_block")],
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
        await query.edit_message_text("/createpost")
    elif data == "menu_newfile":
        await query.edit_message_text("/newfile ပြီးမှ video")
    elif data == "menu_batch":
        await query.edit_message_text("/batchlink")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await query.edit_message_text(f"👥 {total_users} users\n🎬 {total_req} requests")
    elif data == "menu_block":
        blocked = get_blocked_users()
        msg = "Blocked:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "Empty"
        await query.edit_message_text(msg)

async def stats(update, context):
    if is_admin(update.effective_user.id):
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await update.message.reply_text(f"Users: {total_users}\nRequests: {total_req}")

async def blocklist(update, context):
    if is_admin(update.effective_user.id):
        blocked = get_blocked_users()
        msg = "Blocked:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "None"
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
            await update.message.reply_text(f"Unblocked {uid}")
        else:
            await update.message.reply_text(f"{uid} not blocked")
    except:
        await update.message.reply_text("Invalid ID")

async def menu_command(update, context):
    if is_admin(update.effective_user.id):
        await show_menu(update, context)

# ---------- Build Application ----------
def main():
    application = Application.builder().token(TOKEN).build()

    createpost_conv = ConversationHandler(
        entry_points=[CommandHandler('createpost', createpost_start)],
        states={
            CREATE_POSTER: [MessageHandler(filters.PHOTO, createpost_receive_poster)],
            CREATE_MOVIE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, createpost_receive_movie_name)],
            CREATE_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.ALL, createpost_receive_video)],
        },
        fallbacks=[CommandHandler('cancel', cancel_createpost)],
    )

    application.add_handler(createpost_conv)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("movie", movie_command))
    application.add_handler(CommandHandler("newfile", newfile_command))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, newfile_receive))
    application.add_handler(CommandHandler("batchlink", batchlink_start))
    application.add_handler(CommandHandler("done", batchlink_done))
    application.add_handler(CommandHandler("cancel", cancel_batch))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("blocklist", blocklist))
    application.add_handler(CommandHandler("unblock", unblock))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

    # Run polling - the correct way for Python 3.14
    application.run_polling()

if __name__ == "__main__":
    # Flask in thread
    def run_flask():
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port, use_reloader=False)
    import threading
    threading.Thread(target=run_flask, daemon=True).start()
    main()
