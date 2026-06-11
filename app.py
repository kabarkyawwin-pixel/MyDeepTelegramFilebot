import os
import asyncio
import threading
import logging
import sys
import secrets
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
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

# ---------- MongoDB Connection ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI environment variable not set!")
    sys.exit(1)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_share_bot"]
file_store_collection = db["file_store"]
users_collection = db["users"]
stats_collection = db["stats"]
blocked_users_collection = db["blocked_users"]          # {user_id: {"attempts": N, "blocked_at": timestamp}}
user_attempts_collection = db["user_attempts"]          # {user_id: attempt_count}

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
        users_collection.insert_one({"user_id": user_id, "joined_at": datetime.now()})

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

# ---------- Block/Attempt Management ----------
def get_user_attempts(user_id: int) -> int:
    doc = user_attempts_collection.find_one({"user_id": user_id})
    return doc["attempts"] if doc else 0

def increment_user_attempts(user_id: int) -> int:
    new_attempts = get_user_attempts(user_id) + 1
    user_attempts_collection.update_one(
        {"user_id": user_id},
        {"$set": {"attempts": new_attempts}},
        upsert=True
    )
    return new_attempts

def reset_user_attempts(user_id: int):
    user_attempts_collection.update_one(
        {"user_id": user_id},
        {"$set": {"attempts": 0}},
        upsert=True
    )

def is_user_blocked(user_id: int) -> bool:
    doc = blocked_users_collection.find_one({"user_id": user_id})
    return doc is not None

def block_user(user_id: int):
    blocked_users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"blocked_at": datetime.now()}},
        upsert=True
    )

def unblock_user(user_id: int):
    blocked_users_collection.delete_one({"user_id": user_id})
    reset_user_attempts(user_id)

def get_blocked_users():
    return [doc["user_id"] for doc in blocked_users_collection.find({}, {"user_id": 1})]

# ---------- Configuration from Environment ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")

# Channel configuration: list of dicts with "id" and "name" and "invite_link"
# Format in env: CHANNEL_IDS=id1,id2,id3,id4
# CHANNEL_NAMES=Movies channel main (HD Movies များကိုကြည့်ဖို့ဝင်ထားပေးပါ),Movies channel 2 (အရံချန်နယ်),လူကြီးများအတွက်သီးသန့်ချန်နယ် (ကလေးများမဝင်ရ),မြန်မာသီချင်းချန်နယ် (သီချင်းနားထောင်လို့ရပါတယ်)
# CHANNEL_INVITE_LINKS=link1,link2,link3,link4

CHANNEL_IDS = [ch.strip() for ch in os.environ.get("CHANNEL_IDS", "").split(",") if ch.strip()]
CHANNEL_NAMES = [name.strip() for name in os.environ.get("CHANNEL_NAMES", "").split(",") if name.strip()]
CHANNEL_INVITE_LINKS = [link.strip() for link in os.environ.get("CHANNEL_INVITE_LINKS", "").split(",") if link.strip()]

# Ensure lengths match
if len(CHANNEL_IDS) != len(CHANNEL_NAMES):
    logger.warning("CHANNEL_IDS and CHANNEL_NAMES length mismatch!")
while len(CHANNEL_NAMES) < len(CHANNEL_IDS):
    CHANNEL_NAMES.append(f"Channel {len(CHANNEL_NAMES)+1}")
while len(CHANNEL_INVITE_LINKS) < len(CHANNEL_IDS):
    CHANNEL_INVITE_LINKS.append(None)

ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

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
    missing_indices = []
    missing_names = []
    missing_links = []
    for i, ch_id in enumerate(CHANNEL_IDS):
        if not await is_member_of_channel(user_id, ch_id, context):
            missing_indices.append(i)
            missing_names.append(CHANNEL_NAMES[i] if i < len(CHANNEL_NAMES) else f"Channel {i+1}")
            missing_links.append(CHANNEL_INVITE_LINKS[i] if i < len(CHANNEL_INVITE_LINKS) else None)
    return (len(missing_indices) == 0, missing_names, missing_links)

# ---------- Start & Deep Link Handler ----------
MAX_ATTEMPTS = 10

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Check if blocked
    if is_user_blocked(user_id):
        await update.message.reply_text(
            "🚫 **လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည်မဟုတ်ပါ။**\n\n"
            "ဝမ်းနည်းပါတယ်ရှင့် လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
            "သင်သည် ချန်နယ်အားလုံးကိုဝင်ရောက်ပြီး ဇာတ်ကားလင့်ကို ပြန်နှိပ်ဖို့ အကြံပြုပါရစေ။\n\n"
            "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏",
            parse_mode="Markdown"
        )
        return

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_info = get_file_info(payload)

        if not file_info:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return

        # Increment attempts
        attempts = increment_user_attempts(user_id)
        remaining = MAX_ATTEMPTS - attempts

        # Check membership
        all_ok, missing_names, missing_links = await check_all_channels(user_id, context)

        if not all_ok:
            if remaining <= 0:
                # Block user
                block_user(user_id)
                await update.message.reply_text(
                    "🚫 **လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည်မဟုတ်ပါ။**\n\n"
                    "ဝမ်းနည်းပါတယ်ရှင့် လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
                    "သင်သည် ချန်နယ်အားလုံးကိုဝင်ရောက်ပြီး ဇာတ်ကားလင့်ကို ပြန်နှိပ်ဖို့ အကြံပြုပါရစေ။\n\n"
                    "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏",
                    parse_mode="Markdown"
                )
                return

            # Build message with channel names
            msg = "🎬 **ဇာတ်ကားဖိုင်ကိုဒေါင်းလုဒ်လုပ်ရန် ကျေးဇူးပြု၍ အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါနော်**\n\n"
            for i, name in enumerate(missing_names):
                msg += f"📌 **{name}**\n"
                if missing_links[i]:
                    msg += f"   👉 [ဝင်ရန် နှိပ်ပါ]({missing_links[i]})\n"
                msg += "\n"
            msg += f"⚠️ သင်သည် လင့်ကို {attempts}/{MAX_ATTEMPTS} ကြိမ် နှိပ်ပြီးဖြစ်သည်။ {remaining} ကြိမ်သာ ကျန်ပါသေးသည်။\n"
            msg += "Channel များအားလုံးဝင်ပြီးနောက် လင့်ကို ထပ်မံနှိပ်ပါ။"
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            return

        # All channels joined – reset attempts and grant access
        reset_user_attempts(user_id)
        if is_user_blocked(user_id):
            unblock_user(user_id)  # auto unblock when all channels joined

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
            add_user(user_id)
            increment_requests()
        except Exception as e:
            await update.message.reply_text(f"❌ ဖိုင်ပို့ရာတွင် အမှား: {str(e)}")
    else:
        # Normal /start command
        if is_admin(user_id):
            await update.message.reply_text(
                "🤖 **File Share Bot**\n\n"
                "အောက်ပါ Command များကို သုံးနိုင်ပါသည်။\n\n"
                "/newfile - 🆕 ဖိုင်အသစ် တင်ရန် (Admin သာ)\n"
                "/stats - 📊 စာရင်းအင်းကြည့်ရန်\n"
                "/broadcast - 📢 အသုံးပြုသူအားလုံးကို စာပို့ရန်\n"
                "/blocklist - 📋 ဘလော့ထားတဲ့လူစာရင်း\n"
                "/unblock - 🔓 User ID ဖြင့် block ဖွင့်ရန်\n"
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

# ---------- /newfile Command (Admin only) ----------
async def newfile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📤 ဖိုင်တစ်ခု (ဗီဒီယို၊ စာရွက်စာတမ်း၊ အသံ၊ ပုံ) ကို ပို့ပေးပါ။")
    return 1

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        return 1

    try:
        payload = generate_payload()
        save_file_info(payload, file_obj.file_id, file_name)
        deep_link = create_deep_linked_url(BOT_USERNAME, payload)
        await update.message.reply_text(
            f"✅ **ဖိုင် တင်ခြင်း အောင်မြင်ပါသည်။**\n\n"
            f"**ဖိုင်အမည်:** {file_name}\n"
            f"**Deep Link:**\n{deep_link}\n\n"
            f"ဤလင့်ကို ကူးယူ၍ မျှဝေနိုင်ပါသည်။\n"
            f"အသုံးပြုသူများသည် လိုအပ်သော Channel များအားလုံးကို ဝင်ပြီးမှသာ ဖိုင်ကို ရယူနိုင်မည်။\n\n"
            f"**မှတ်ချက်:** လင့်ကို 10 ကြိမ်အထိ နှိပ်နိုင်ပြီး ကျော်ပါက block ခံရမည်။",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text(f"❌ ဖိုင် သိမ်းဆည်းရာတွင် အမှား: {str(e)}")
    return ConversationHandler.END

async def cancel_newfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    return ConversationHandler.END

# ---------- Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    blocked_count = blocked_users_collection.count_documents({})
    await update.message.reply_text(
        f"📊 **စာရင်းအင်း**\n\n"
        f"👥 အသုံးပြုသူဦးရေ: {total_users}\n"
        f"📥 တောင်းဆိုမှုအရေအတွက်: {total_requests}\n"
        f"🚫 Block ခံရသူဦးရေ: {blocked_count}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
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
    await update.message.reply_text(f"📢 ပြန်လွှင့်ခြင်း ပြီးဆုံးပါပြီ။ လက်ခံသူ {count} ဦး။")

async def blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    blocked_users = get_blocked_users()
    if not blocked_users:
        await update.message.reply_text("📋 ဘလော့ထားတဲ့ လူစာရင်း မရှိသေးပါ။")
        return
    msg = "🚫 **ဘလော့ထားတဲ့ User များ**\n\n"
    for uid in blocked_users:
        msg += f"• `{uid}`\n"
    msg += "\n/unblock <user_id> ဖြင့် ပြန်ဖွင့်နိုင်ပါသည်။"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("🔓 /unblock <user_id> ဖြင့် အသုံးပြုပါ။")
        return
    try:
        user_id = int(context.args[0])
        if is_user_blocked(user_id):
            unblock_user(user_id)
            await update.message.reply_text(f"✅ User `{user_id}` ကို unblock လုပ်ပြီးပါပြီ။", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"ℹ️ User `{user_id}` သည် block မခံရပါ။", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ မှန်ကန်သော User ID ထည့်ပါ။")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။ (အသုံးပြုသူများ ဖိုင်ရယူနိုင်မည် မဟုတ်ပါ)")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

async def placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("လုပ်ဆောင်ချက် ရရှိနိုင်သေးပါ။")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# Conversation for /newfile
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newfile', newfile_start)],
    states={
        1: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_file)],
    },
    fallbacks=[CommandHandler('cancel', cancel_newfile)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(conv_handler)
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock_command))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("help", placeholder))
application.add_handler(CommandHandler("about", placeholder))

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
