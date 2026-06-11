import os
import asyncio
import threading
import logging
import sys
import secrets
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
users_collection = db["users"]          # regular users who successfully got files
banned_collection = db["banned_users"]  # {user_id: reason, attempt_count, banned_at}
attempts_collection = db["attempts"]    # {user_id: count, last_attempt}
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
        users_collection.insert_one({"user_id": user_id})

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

# ---------- Banned users functions ----------
def is_banned(user_id: int) -> bool:
    return banned_collection.find_one({"user_id": user_id}) is not None

def ban_user(user_id: int, reason: str):
    banned_collection.update_one(
        {"user_id": user_id},
        {"$set": {"reason": reason, "banned_at": datetime.now()}},
        upsert=True
    )

def unban_user(user_id: int):
    banned_collection.delete_one({"user_id": user_id})

def get_banned_users():
    return list(banned_collection.find({}, {"user_id": 1, "reason": 1}))

def get_attempt_count(user_id: int) -> int:
    doc = attempts_collection.find_one({"user_id": user_id})
    return doc["count"] if doc else 0

def increment_attempt(user_id: int) -> int:
    new_count = attempts_collection.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {"count": 1}},
        upsert=True,
        return_document=True
    )["count"]
    return new_count

def reset_attempts(user_id: int):
    attempts_collection.delete_one({"user_id": user_id})

# ---------- Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
REQUIRED_CHANNELS = [ch.strip() for ch in os.environ.get("REQUIRED_CHANNELS", "").split(",") if ch.strip()]
CHANNEL_INVITE_LINKS = [link.strip() for link in os.environ.get("CHANNEL_INVITE_LINKS", "").split(",") if link.strip()]
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

maintenance_mode = False
MAX_ATTEMPTS = 10

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
    invite_links = []
    for i, ch in enumerate(REQUIRED_CHANNELS):
        if not await is_member_of_channel(user_id, ch, context):
            missing.append(ch)
            if i < len(CHANNEL_INVITE_LINKS):
                invite_links.append(CHANNEL_INVITE_LINKS[i])
            else:
                invite_links.append(None)
    return (len(missing) == 0, missing, invite_links)

# ---------- Channel display names mapping (you can customize here) ----------
# These names will be shown instead of channel IDs
CHANNEL_NAMES = {
    "-1003753299714": "🎬 Movies channel main (HD Movies များကိုကြည့်ရန်)",
    "-1003899625672": "🎬 Movies channel 2 (အရံချန်နယ်)",
    "-1003792838735": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ် (ကလေးများမဝင်ရ)",
    "-1003785717514": "🎵 မြန်မာသီချင်းချန်နယ် (သီချင်းနားထောင်ရန်)",
}
DEFAULT_CHANNEL_NAME = "Channel"

def get_channel_display(channel_id: str) -> str:
    return CHANNEL_NAMES.get(channel_id, channel_id)

# ---------- Start & Deep Link Handler ----------
from datetime import datetime

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_info = get_file_info(payload)

        if not file_info:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return

        # Check if user is banned
        if is_banned(user_id):
            await update.message.reply_text(
                "🚫 လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည်မဟုတ်ပါ။\n"
                "ဝမ်းနည်းပါတယ်ရှင့်။\n"
                "လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
                "သင်ချန်နယ်အားလုံးကိုဝင်ရောက်ပြီး ဇာတ်ကားလင့်ကို ပြန်နှိပ်ဖို့ အကြံပြုပါရစေ။\n"
                "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏"
            )
            return

        # Check membership
        all_ok, missing, invite_links = await check_all_channels(user_id, context)

        if not all_ok:
            # Increment attempt counter
            attempts = increment_attempt(user_id)
            remaining = MAX_ATTEMPTS - attempts
            if attempts >= MAX_ATTEMPTS:
                # Ban user
                ban_user(user_id, "Exceeded max attempts without joining channels")
                await update.message.reply_text(
                    "လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည်မဟုတ်ပါ။\n"
                    "ဝမ်းနည်းပါတယ်ရှင့်။\n"
                    "လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
                    "သင်ချန်နယ်အားလုံးကိုဝင်ရောက်ပြီး ဇာတ်ကားလင့်ကို ပြန်နှိပ်ဖို့ အကြံပြုပါရစေ။\n"
                    "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏"
                )
                return

            # Build message with channel names and invite links
            message = "🔒 **ဇာတ်ကားဖိုင်ကိုဒေါင်းလုဒ်လုပ်ရန် ကျေးဇူးပြု၍ အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါနော်**\n\n"
            for i, ch in enumerate(missing):
                display = get_channel_display(ch)
                message += f"📌 {display}\n"
                if invite_links[i]:
                    message += f"   👉 [ဝင်ရန် နှိပ်ပါ]({invite_links[i]})\n"
                message += "\n"
            message += f"⚠️ သင်သည် လင့်ကို {attempts}/{MAX_ATTEMPTS} ကြိမ် နှိပ်ပြီးဖြစ်သည်။ {remaining} ကြိမ်သာ ကျန်ပါသေးသည်။\n"
            message += "Channel များအားလုံးဝင်ပြီးနောက် လင့်ကို ထပ်မံနှိပ်ပါ။"
            await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)
            return

        # All channels joined – grant access
        # Reset attempts and unban if previously banned (but we already checked is_banned false)
        reset_attempts(user_id)
        if is_banned(user_id):
            unban_user(user_id)
            await update.message.reply_text("✅ သင်သည် ချန်နယ်အားလုံးကို ဝင်ရောက်ထားသောကြောင့် block မှ ပြန်လည်လွတ်မြောက်ပါပြီ။ ကျေးဇူးတင်ပါတယ်။")

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
        # Normal /start without payload
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text(
                "🤖 **File Share Bot**\n\n"
                "ဤ Bot သည် ဖိုင်များကို လုံခြုံစွာ မျှဝေရန် သုံးပါသည်။\n"
                "ဖိုင်ရယူရန် သင့်အား ပေးထားသော Deep Link ကို နှိပ်ပါ။\n"
                "ပထမဆုံး လိုအပ်သော Channel များအားလုံးကို ဝင်ရောက်ရပါမည်။",
                parse_mode="Markdown"
            )

# ---------- Menu ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **File Share Bot**\n\n"
        "အောက်ပါ Command များကို သုံးနိုင်ပါသည်။\n\n"
        "/newfile - 🆕 ဖိုင်အသစ် တင်ရန် (Admin သာ)\n"
        "/stats - 📊 စာရင်းအင်းကြည့်ရန်\n"
        "/broadcast - 📢 အသုံးပြုသူအားလုံးကို စာပို့ရန်\n"
        "/blocklist - 🚫 block ထားတဲ့လူစာရင်း\n"
        "/unblock <user_id> - 🛡️ သတ်မှတ် user ကို unblock လုပ်ရန်\n"
        "/mute - 🔇 Maintenance mode ဖွင့်ရန်\n"
        "/unmute - 🔊 Maintenance mode ပိတ်ရန်",
        parse_mode="Markdown"
    )

# ---------- /newfile Command ----------
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
        # No parse_mode to avoid Markdown errors
        await update.message.reply_text(
            f"✅ ဖိုင် တင်ခြင်း အောင်မြင်ပါသည်။\n\n"
            f"ဖိုင်အမည်: {file_name}\n"
            f"Deep Link:\n{deep_link}\n\n"
            f"ဤလင့်ကို ကူးယူ၍ မျှဝေနိုင်ပါသည်။\n"
            f"အသုံးပြုသူများသည် လိုအပ်သော Channel များအားလုံးကို ဝင်ပြီးမှသာ ဖိုင်ကို ရယူနိုင်မည်။"
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
    banned_count = banned_collection.count_documents({})
    await update.message.reply_text(
        f"📊 **စာရင်းအင်း**\n\n"
        f"👥 အသုံးပြုသူဦးရေ: {total_users}\n"
        f"📥 တောင်းဆိုမှုအရေအတွက်: {total_requests}\n"
        f"🚫 Block ခံရသူဦးရေ: {banned_count}",
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
    banned_users = list(banned_collection.find({}, {"user_id": 1, "reason": 1}))
    if not banned_users:
        await update.message.reply_text("🚫 Block ခံထားရသူ မရှိပါသေးပါ။")
        return
    text = "🚫 **Block ခံထားရသူများ**\n\n"
    for u in banned_users:
        text += f"• User ID: `{u['user_id']}`\n"
    text += "\n/unblock <user_id> ဖြင့် ပြန်လည်ဖွင့်နိုင်ပါသည်။"
    await update.message.reply_text(text, parse_mode="Markdown")

async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("❗ /unblock <user_id> ဟု ထည့်ပေးပါ။")
        return
    try:
        user_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ User ID သည် နံပါတ်သာဖြစ်ရမည်။")
        return
    if not is_banned(user_id):
        await update.message.reply_text(f"User ID {user_id} သည် block မခံရပါ။")
        return
    unban_user(user_id)
    reset_attempts(user_id)
    await update.message.reply_text(f"✅ User ID {user_id} အား unblock ပြုလုပ်ပြီးပါပြီ။")

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

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newfile', newfile_start)],
    states={
        1: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_file)],
    },
    fallbacks=[CommandHandler('cancel', cancel_newfile)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("menu", show_menu))
application.add_handler(conv_handler)
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
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
