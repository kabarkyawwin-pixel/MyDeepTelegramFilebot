import os
import asyncio
import threading
import logging
import sys
import secrets
import re
from datetime import datetime
from flask import Flask, request
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
    return "Bot is running (webhook)"

@app.route('/health')
def health():
    return "OK", 200

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

async def create_telegraph_page_movie(title, content):
    try:
        html = content.replace('\n', '<br>')
        resp = await asyncio.to_thread(telegraph.create_page, title=title, html_content=f"<p>{html}</p>", author_name="ရုပ်ရှင်အချက်အလက်")
        return resp['url']
    except:
        return None

# ---------- Translation ----------
tr_en2my = GoogleTranslator(source='en', target='my')
tr_my2en = GoogleTranslator(source='my', target='en')

def translate_text(text, source='en', target='my'):
    if not text or text == 'N/A':
        return text
    try:
        if source == 'en' and target == 'my':
            return tr_en2my.translate(text)
        elif source == 'my' and target == 'en':
            return tr_my2en.translate(text)
    except:
        return text

def has_burmese(text):
    return bool(re.search(r'[\u1000-\u109F]', text))

def normalize_name(text):
    if has_burmese(text):
        try:
            return tr_my2en.translate(text).strip()
        except:
            return text
    return text

# ---------- OMDB ----------
OMDB_KEY = "5025f95c"

def get_movie_info(q):
    name = normalize_name(q)
    year_match = re.search(r'[\(\[]?(\d{4})[\)\]]?', name)
    if year_match:
        year = year_match.group(1)
        name = re.sub(r'[\(\[]?\d{4}[\)\]]?\s*$', '', name).strip()
    else:
        year = None
    params = {'t': name, 'apikey': OMDB_KEY, 'plot': 'full'}
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

def format_movie_plain(m):
    return f"""🎬 {m['title']} ({m['year']})

📌 အမျိုးအစား – {m['genre']}
🎭 သရုပ်ဆောင်များ – {m['actors']}
🎥 ဒါရိုက်တာ – {m['director']}
⏱️ ကြာချိန် – {m['runtime']}
🌍 နိုင်ငံ – {m['country']}
🗣️ ဘာသာစကား – {m['language']}
⭐ IMDb – {m['imdb_rating']}/10
🗳️ မဲ – {m['imdb_votes']}

📖 ဇာတ်လမ်းအကျဉ်း – {m['plot']}"""

# ========== /movie ==========
async def movie_cmd(update, context):
    if not context.args:
        await update.message.reply_text("ဥပမာ - /movie Inception 2010")
        return
    q = ' '.join(context.args)
    msg = await update.message.reply_text(f"🔍 '{q}' ကို ရှာနေသည်...")
    m = get_movie_info(q)
    if not m:
        await msg.edit_text("မတွေ့ပါ။ အင်္ဂလိပ်အမည်နှင့် ထပ်စမ်းပါ။")
        return
    text = format_movie_plain(m)
    kb = []
    if len(m['plot']) > 800:
        url = await create_telegraph_page_movie(f"{m['title']} ({m['year']})", m['plot'])
        if url:
            kb.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None)

# ========== /createpost ==========
CREATE_STATE = {}

async def createpost_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /createpost ဇာတ်ကားအမည် နှစ်\nဥပမာ - /createpost Inception 2010")
        return
    q = ' '.join(context.args)
    m = get_movie_info(q)
    if not m:
        await update.message.reply_text("ဇာတ်ကား ရှာမတွေ့ပါ။")
        return
    uid = update.effective_user.id
    CREATE_STATE[uid] = {'movie': m, 'step': 'wait_poster'}
    await update.message.reply_text(f"✅ တွေ့ရှိပါသည်။\n{format_movie_plain(m)}\n\n📸 Poster ပုံကို ပို့ပါ။")

async def handle_poster(update, context):
    uid = update.effective_user.id
    if uid not in CREATE_STATE or CREATE_STATE[uid]['step'] != 'wait_poster':
        return
    if not update.message.photo:
        await update.message.reply_text("Poster ပုံပို့ပါ။")
        return
    CREATE_STATE[uid]['poster'] = update.message.photo[-1].file_id
    CREATE_STATE[uid]['step'] = 'wait_video'
    await update.message.reply_text("Poster ရပြီ။ 🎬 Video ဖိုင်ပို့ပါ။")

async def handle_video_for_create(update, context):
    uid = update.effective_user.id
    if uid not in CREATE_STATE or CREATE_STATE[uid]['step'] != 'wait_video':
        return
    vid = None
    fname = None
    if update.message.video:
        vid = update.message.video
        fname = vid.file_name or "movie"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            vid = doc
            fname = doc.file_name or "movie"
    if not vid:
        await update.message.reply_text("Video ဖိုင် (mp4, mkv) ပို့ပါ။")
        return
    payload = generate_payload()
    save_file_info(payload, vid.file_id, fname)
    link = create_deep_linked_url(BOT_USERNAME, payload)
    movie = CREATE_STATE[uid]['movie']
    poster = CREATE_STATE[uid]['poster']
    text = format_movie_plain(movie)
    kb = [[InlineKeyboardButton("🎬 ရယူရန်", url=link)]]
    if len(movie['plot']) > 800:
        url = await create_telegraph_page_movie(f"{movie['title']} ({movie['year']})", movie['plot'])
        if url:
            kb.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်", url=url)])
    for ch in REQUIRED_CHANNELS:
        kb.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    await update.message.reply_photo(photo=poster, caption=text, reply_markup=InlineKeyboardMarkup(kb))
    await update.message.reply_text("✅ Post ပြင်ဆင်ပြီး။ သင့် Channel တွင် Forward လုပ်ပါ။")
    del CREATE_STATE[uid]

# ========== /newfile ==========
async def newfile_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    await update.message.reply_text("📤 Video ဖိုင်ပို့ပါ။ Deep Link ထုတ်ပေးမည်။")
    context.user_data['newfile_wait'] = True

async def newfile_receive(update, context):
    if not context.user_data.get('newfile_wait'):
        return
    if not is_admin(update.effective_user.id):
        return
    vid = None
    fname = None
    if update.message.video:
        vid = update.message.video
        fname = vid.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            vid = doc
            fname = doc.file_name or "video"
    if not vid:
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return
    payload = generate_payload()
    save_file_info(payload, vid.file_id, fname)
    link = create_deep_linked_url(BOT_USERNAME, payload)
    await update.message.reply_text(f"🔗 Deep Link:\n{link}\n\n`{fname}`", parse_mode='Markdown')
    context.user_data.pop('newfile_wait', None)

# ========== /batchlink ==========
async def batch_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return
    context.user_data['batch'] = []
    await update.message.reply_text("📦 Video များ ဆက်တိုက်ပို့ပါ။ ပြီးလျှင် /done")

async def batch_receive(update, context):
    if not is_admin(update.effective_user.id):
        return
    if 'batch' not in context.user_data:
        return
    vid = None
    name = None
    if update.message.video:
        vid = update.message.video
        name = vid.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            vid = doc
            name = doc.file_name or "video"
    if not vid:
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return
    context.user_data['batch'].append({"file_id": vid.file_id, "name": name})
    await update.message.reply_text(f"✅ #{len(context.user_data['batch'])}: {name}")

async def batch_done(update, context):
    if not is_admin(update.effective_user.id):
        return
    videos = context.user_data.get('batch', [])
    if not videos:
        await update.message.reply_text("ဗီဒီယိုမရှိပါ။")
        return
    out = []
    for v in videos:
        payload = generate_payload()
        save_file_info(payload, v["file_id"], v["name"])
        link = create_deep_linked_url(BOT_USERNAME, payload)
        out.append(f"• {v['name']}\n  {link}")
    text = "Batch Links:\n" + "\n".join(out)
    if len(text) > 4000:
        text = text[:4000] + "..."
    await update.message.reply_text(text)
    context.user_data.clear()

async def batch_cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")

# ========== /start ==========
async def start_cmd(update, context):
    uid = update.effective_user.id
    if context.args:
        payload = context.args[0]
        info = get_file_info(payload)
        if not info:
            await update.message.reply_text("လင့်မမှန်ပါ။")
            return
        if is_user_blocked(uid):
            await update.message.reply_text("သင်ပိတ်ခံထားရသည်။")
            return
        ok, _ = await check_all_channels(uid, context.bot)
        if not ok:
            attempts = get_attempt_count(uid) + 1
            increment_attempts(uid)
            if attempts >= 10:
                block_user(uid)
                await update.message.reply_text("၁၀ကြိမ်ကျော်သောကြောင့် ပိတ်ပါသည်။")
                return
            msg = "အောက်ပါ Channel များအားလုံးကိုဝင်ပါ:\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• {ch['name']}: {ch['invite']}\n"
            msg += f"\nအကြိမ်ရေ: {attempts}/10"
            await update.message.reply_text(msg)
            return
        try:
            await update.message.reply_text(f"🎬 {info['file_name']} ပို့နေပါပြီ...")
            await context.bot.send_video(chat_id=uid, video=info['file_id'], caption=f"🎬 {info['file_name']}")
            add_user(uid)
            increment_requests()
            reset_attempts(uid)
        except Exception as e:
            await update.message.reply_text(f"မပို့နိုင်ပါ: {e}")
    else:
        if is_admin(uid):
            await show_menu(update, context)
        else:
            await update.message.reply_text("🎬 မင်္ဂလာပါ။ ဇာတ်ကားရယူရန် channel post ရှိ ခလုတ်ကိုနှိပ်ပါ။ /movie ဖြင့် ရှာနိုင်သည်။")

# ---------- Menu ----------
async def show_menu(update, context):
    kb = [
        [InlineKeyboardButton("➕ Postအသစ်", callback_data="menu_cp")],
        [InlineKeyboardButton("🔗 Deep Linkထုတ်", callback_data="menu_nf")],
        [InlineKeyboardButton("📦 Batch Link", callback_data="menu_batch")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("🚫 Blockစာရင်း", callback_data="menu_block")],
    ]
    await update.message.reply_text("Admin Menu", reply_markup=InlineKeyboardMarkup(kb))

async def menu_cb(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("ခွင့်မပြု")
        return
    d = q.data
    if d == "menu_cp":
        await q.edit_message_text("/createpost MovieName Year")
    elif d == "menu_nf":
        await q.edit_message_text("/newfile ပြီးမှ video")
    elif d == "menu_batch":
        await q.edit_message_text("/batchlink")
    elif d == "menu_stats":
        tu = users_collection.count_documents({})
        tr = get_total_requests()
        await q.edit_message_text(f"👥 Users: {tu}\n🎬 Requests: {tr}")
    elif d == "menu_block":
        bl = get_blocked_users()
        msg = "Blocked:\n" + "\n".join(str(uid) for uid in bl) if bl else "Empty"
        await q.edit_message_text(msg)

async def stats_cmd(update, context):
    if is_admin(update.effective_user.id):
        tu = users_collection.count_documents({})
        tr = get_total_requests()
        await update.message.reply_text(f"Users: {tu}\nRequests: {tr}")

async def blocklist_cmd(update, context):
    if is_admin(update.effective_user.id):
        bl = get_blocked_users()
        msg = "Blocked:\n" + "\n".join(str(uid) for uid in bl) if bl else "None"
        await update.message.reply_text(msg)

async def unblock_cmd(update, context):
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

async def menu_cmd(update, context):
    if is_admin(update.effective_user.id):
        await show_menu(update, context)

# ---------- Webhook ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL not set")
    sys.exit(1)

application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("movie", movie_cmd))
application.add_handler(CommandHandler("createpost", createpost_cmd))
application.add_handler(MessageHandler(filters.PHOTO, handle_poster))
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video_for_create))
application.add_handler(CommandHandler("newfile", newfile_cmd))
application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, newfile_receive))
application.add_handler(CommandHandler("batchlink", batch_start))
application.add_handler(CommandHandler("done", batch_done))
application.add_handler(CommandHandler("cancel", batch_cancel))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CommandHandler("blocklist", blocklist_cmd))
application.add_handler(CommandHandler("unblock", unblock_cmd))
application.add_handler(CommandHandler("menu", menu_cmd))
application.add_handler(CallbackQueryHandler(menu_cb, pattern="menu_"))

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            update = Update.de_json(data, application.bot)
            asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
            return "ok", 200
        except Exception as e:
            logger.exception("Webhook error")
            return "error", 500
    return "method not allowed", 405

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

async def set_webhook():
    await application.bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(set_webhook())
    threading.Thread(target=run_flask, daemon=True).start()
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.run_until_complete(application.shutdown())
