import os
import asyncio
import threading
import logging
import sys
import secrets
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient

# ---------- Logging ----------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------- Flask Server ----------
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
db = mongo_client["file_share_bot"]
file_store_collection = db["file_store"]
users_collection = db["users"]
stats_collection = db["stats"]

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
        users_collection.insert_one({
            "user_id": user_id,
            "rate_count": 0,
            "blocked": False
        })

def update_rate(user_id):
    users_collection.update_one({"user_id": user_id}, {"$inc": {"rate_count": 1}})

def get_user_data(user_id):
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        add_user(user_id)
        user = users_collection.find_one({"user_id": user_id})
    return user

def block_user(user_id):
    users_collection.update_one({"user_id": user_id}, {"$set": {"blocked": True}})

def reset_rate(user_id):
    users_collection.update_one({"user_id": user_id}, {"$set": {"rate_count": 0}})

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

# ---------- Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
REQUIRED_CHANNELS = [ch.strip() for ch in os.environ.get("REQUIRED_CHANNELS", "").split(",") if ch.strip()]
CHANNEL_INVITE_LINKS = [link.strip() for link in os.environ.get("CHANNEL_INVITE_LINKS", "").split(",") if link.strip()]
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

RATE_LIMIT = 10  # max allowed attempts

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

maintenance_mode = False

def generate_payload():
    return secrets.token_urlsafe(16)

async def is_member_of_channel(user_id: int, channel_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Membership check failed for {channel_id}: {e}")
        return False

async def check_all_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    missing = []
    invite_links = []
    for i, ch in enumerate(REQUIRED_CHANNELS):
        if not await is_member_of_channel(user_id, ch, context):
            missing.append(ch)
            if i < len(CHANNEL_INVITE_LINKS):
                invite_links.append(CHANNEL_INVITE_LINKS[i])
            else:
                invite_links.append(None)
    return (len(missing) == 0, missing, invite_links)

# ---------- Start & Deep Link Handler ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user(user_id)  # ensure user exists

    # normal /start without payload
    if not context.args or len(context.args) == 0:
        if is_admin(user_id):
            await update.message.reply_text(
                "🤖 **File Share Bot**\n\n"
                "အောက်ပါ Command များကို သုံးနိုင်ပါသည်။\n\n"
                "/newfile - 🆕 ဖိုင်အသစ် တင်ရန် (Admin သာ)\n"
                "/stats - 📊 စာရင်းအင်းကြည့်ရန်\n"
                "/broadcast - 📢 အသုံးပြုသူအားလုံးကို စာပို့ရန်\n"
                "/mute - 🔇 Maintenance mode ဖွင့်ရန်\n"
                "/unmute - 🔊 Maintenance mode ပိတ်ရန်",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "🤖 **File Share Bot**\n\n"
                "ဤ Bot သည် ဖိုင်များကို လုံခြုံစွာ မျှဝေရန် သုံးပါသည်။\n"
                "ဖိုင်ရယူရန် သင့်အား ပေးထားသော Deep Link ကို နှိပ်ပါ။\n"
                "ပထမဆုံး လိုအပ်သော Channel များအားလုံးကို ဝင်ရောက်ရပါမည်။",
                parse_mode="Markdown"
            )
        return

    # deep link with payload
    payload = context.args[0]
    file_info = get_file_info(payload)

    if not file_info:
        await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
        return

    # check if user is blocked
    user_data = get_user_data(user_id)
    if user_data.get("blocked", False):
        await update.message.reply_text(
            "🚫 **သင့်အကောင့်အား ပိတ်ထားပါသည်။**\n\n"
            "သင်သည် ဇာတ်ကားရယူရန် လင့်ကို ၁၀ ကြိမ်ကျော် နှိပ်ထားပြီး လိုအပ်သော Channel များကို "
            "မဝင်ထားသောကြောင့် ကျွန်ုပ်က သင့်အား block လုပ်ထားပါသည်။\n"
            "နောင်တွင် ကျွန်ုပ်ထံမှ ဇာတ်ကားများ ရယူနိုင်မည် မဟုတ်ပါ။\n"
            "သာယာပျော်ရွှင်သော နေ့လေးဖြစ်ပါစေ 🙏",
            parse_mode="Markdown"
        )
        return

    # rate limit check
    current_count = user_data.get("rate_count", 0)
    if current_count >= RATE_LIMIT:
        # block user
        block_user(user_id)
        await update.message.reply_text(
            "🚫 **လူကြီးမင်းသည် ဇာတ်ကားရယူဖို့ လင့်ကို ၁၀ ခါပြည့်သွားလို့ ကျွန်ုပ်က လူကြီးမင်းကို block လိုက်ပါသည်။**\n\n"
            "သင် သည် ကျွန်ုပ်၏ တောင်းဆိုထားသည့်အတိုင်း ချန်နယ်လေးများကို မဝင်ထားသည့်အတွက် ဇာတ်ကားမယူနိုင်တော့ပါ။\n"
            "နောင်တွင်ကျွန်ုပ်ထံမှ ဇာတ်ကားများကို ရနိုင်မည်မဟုတ်ပါ။\n"
            "သာယာပျော်ရွှင်သောနေ့လေးဖြစ်ပါစေ 🙏",
            parse_mode="Markdown"
        )
        return

    # increment rate counter
    update_rate(user_id)

    # check channels membership
    all_ok, missing, invite_links = await check_all_channels(user_id, context)

    if not all_ok:
        # build message with missing channels
        msg = "🔒 **ဇာတ်ကားကို ဒေါင်းလုဒ်လုပ်ရန် အောက်ပါ Channel များအားလုံးကို ဝင်ရောက်ထားရပါမည်။**\n\n"
        for i, ch in enumerate(missing):
            msg += f"• Channel: `{ch}`\n"
            if invite_links[i]:
                msg += f"  👉 [ဝင်ရန် နှိပ်ပါ]({invite_links[i]})\n"
            msg += "\n"
        msg += f"**မှတ်ချက်:** သင်သည် ဤ Deep Link ကို အကြိမ် **{RATE_LIMIT - (current_count + 1)}** ထပ်မံ နှိပ်ခွင့် ကျန်သေးသည်။\n"
        msg += "Channel များအားလုံးကို ဝင်ပြီးမှသာ ဖိုင်ကို ရယူနိုင်မည်။"
        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
        return

    # all channels joined – deliver file
    file_id = file_info["file_id"]
    file_name = file_info["file_name"]

    try:
        await update.message.reply_text(f"📁 **သင်၏ဖိုင်:** {file_name}\n\nဖိုင်ကို ပို့ပေးနေပါပြီ...", parse_mode="Markdown")
        await context.bot.send_document(
            chat_id=user_id,
            document=file_id,
            filename=file_name,
            caption=f"📄 ဖိုင်အမည်: {file_name}"
        )
        increment_requests()
        # optional: reset rate count after successful delivery? Commented: we keep count but reset maybe? We'll keep for now.
        # reset_rate(user_id)   # uncomment if you want to reset after successful download
    except Exception as e:
        await update.message.reply_text(f"❌ ဖိုင်ပို့ရာတွင် အမှား: {str(e)}")

# ---------- /newfile Command ----------
async def newfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return

    # Wait for a file (no conversation, just simple)
    context.user_data['waiting_for_file'] = True
    await update.message.reply_text("📤 ဖိုင်တစ်ခု (Video, Document, Audio, Photo) ကို ပို့ပေးပါ။")

async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_file'):
        return
    if not is_admin(update.effective_user.id):
        context.user_data.pop('waiting_for_file', None)
        return

    doc = update.message.document
    video = update.message.video
    audio = update.message.audio
    photo = update.message.photo
    file_obj = None
    file_name = None

    if doc:
        file_obj = doc
        file_name = doc.file_name or "document"
    elif video:
        file_obj = video
        file_name = video.file_name or "video.mp4"
    elif audio:
        file_obj = audio
        file_name = audio.file_name or "audio.mp3"
    elif photo:
        file_obj = photo[-1]
        file_name = "photo.jpg"
    else:
        await update.message.reply_text("❌ ကျေးဇူးပြု၍ ဖိုင် (Document, Video, Audio, Photo) တစ်ခု ပို့ပေးပါ။")
        return

    try:
        payload = generate_payload()
        save_file_info(payload, file_obj.file_id, file_name)
        deep_link = create_deep_linked_url(BOT_USERNAME, payload)
        await update.message.reply_text(
            f"✅ **ဖိုင် တင်ခြင်း အောင်မြင်ပါသည်။**\n\n"
            f"**ဖိုင်အမည်:** {file_name}\n"
            f"**Deep Link:**\n{deep_link}\n\n"
            f"ဤလင့်ကို ကူးယူ၍ မျှဝေနိုင်ပါသည်။\n"
            f"အသုံးပြုသူများသည် လိုအပ်သော Channel များအားလုံးကို ဝင်ပြီးမှသာ ဖိုင်ကို ရယူနိုင်မည်။\n"
            f"အကြိမ်အရေအတွက် အကန့်အသတ် **{RATE_LIMIT}** ကြိမ်အထိ နှိပ်ခွင့်ရှိသည်။",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text(f"❌ ဖိုင် သိမ်းဆည်းရာတွင် အမှား: {str(e)}")
    finally:
        context.user_data.pop('waiting_for_file', None)

# ---------- Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n📥 အောင်မြင်သောတောင်းဆိုမှု: {total_requests}\n⛔ အသုံးပြုသူများ block ခံရမှု: {users_collection.count_documents({'blocked': True})}", parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("📢 /broadcast <message>")
        return
    users = [doc["user_id"] for doc in users_collection.find({}, {"user_id": 1})]
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 ပြန်လွှင့်ခြင်း ပြီးဆုံးပါပြီ။ လက်ခံသူ {count} ဦး။")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global maintenance_mode
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global maintenance_mode
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("newfile", newfile))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file_upload))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))

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
