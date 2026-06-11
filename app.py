import os
import asyncio
import threading
import logging
import sys
import secrets
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault
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

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI environment variable not set!")
    sys.exit(1)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_share_bot"]
file_store_collection = db["file_store"]      # {payload: {"file_id": "...", "file_name": "..."}}
users_collection = db["users"]                # {user_id: ..., click_count: ..., blocked: bool}
stats_collection = db["stats"]                # {_id: "total_requests", count: N}
blocked_collection = db["blocked"]            # {user_id: ..., reason: ..., blocked_at: ...}
channel_names = {                             # ပြသလိုသော Channel အမည်များ
    "-1003753299714": "Movies Channel Main (HD Movies ကြည့်ရန်)",
    "-1003899625672": "Movies Channel 2 (အရံချန်နယ်)",
    "-1003792838735": "လူကြီးများအတွက် သီးသန့်ချန်နယ် (ကလေးများမဝင်ရ)",
    "-1003785717514": "မြန်မာသီချင်းချန်နယ် (သီချင်းနားထောင်ရန်)"
}
channel_invite_links = {                     # Channel များ၏ Invite Links
    "-1003753299714": "https://t.me/wznmoviescollector",
    "-1003899625672": "https://t.me/moviesandseriesforallwzn",
    "-1003792838735": "https://t.me/everyboyhobby",
    "-1003785717514": "https://t.me/wznmusiclibary"
}
REQUIRED_CHANNELS = list(channel_names.keys())   # Channel IDs (must be numeric or @username)
MAX_CLICKS = 10

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
        users_collection.insert_one({"user_id": user_id, "click_count": 0, "blocked": False})

def update_click_count(user_id, increment=True):
    if increment:
        users_collection.update_one({"user_id": user_id}, {"$inc": {"click_count": 1}}, upsert=True)
    doc = users_collection.find_one({"user_id": user_id})
    return doc["click_count"] if doc else 0

def reset_click_count(user_id):
    users_collection.update_one({"user_id": user_id}, {"$set": {"click_count": 0}}, upsert=True)

def get_click_count(user_id):
    doc = users_collection.find_one({"user_id": user_id})
    return doc["click_count"] if doc else 0

def is_user_blocked(user_id):
    doc = blocked_collection.find_one({"user_id": user_id})
    return doc is not None

def block_user(user_id, reason):
    blocked_collection.update_one(
        {"user_id": user_id},
        {"$set": {"reason": reason, "blocked_at": datetime.now()}},
        upsert=True
    )
    users_collection.update_one({"user_id": user_id}, {"$set": {"blocked": True}}, upsert=True)

def unblock_user(user_id):
    blocked_collection.delete_one({"user_id": user_id})
    users_collection.update_one({"user_id": user_id}, {"$set": {"blocked": False, "click_count": 0}}, upsert=True)

def get_blocked_users():
    return list(blocked_collection.find({}, {"user_id": 1, "reason": 1, "blocked_at": 1}))

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

# ---------- Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
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
    except:
        return False

# ---------- Start & Deep Link ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_info = get_file_info(payload)
        if not file_info:
            await update.message.reply_text("❌ လင့်မမှန်ပါ သို့မဟုတ် သက်တမ်းကုန်ပြီ။")
            return

        # Check if user is blocked
        if is_user_blocked(user_id):
            await update.message.reply_text(
                "🔒 သင်သည် ဇာတ်ကားလင့်ကို ၁၀ ကြိမ်ကျော် နှိပ်ထားသောကြောင့် ပိတ်ပင်ခြင်းခံရသည်။\n"
                "ချန်နယ်များအားလုံးကို ဝင်ပြီးနောက် လင့်ကို ထပ်မံနှိပ်ပါက အလိုအလျောက် ပြန်ဖွင့်ပေးပါမည်။"
            )
            return

        # Check membership
        missing_channels = []
        for ch_id in REQUIRED_CHANNELS:
            if not await is_member_of_channel(user_id, ch_id, context):
                missing_channels.append(ch_id)

        if missing_channels:
            # Increase click count
            new_count = update_click_count(user_id, True)
            remaining = MAX_CLICKS - new_count
            if remaining < 0:
                remaining = 0

            # Build message
            msg = "🔒 **ဇာတ်ကားဖိုင်ကို ဒေါင်းလုဒ်လုပ်ရန် ကျေးဇူးပြု၍ အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါနော်**\n\n"
            for ch_id in missing_channels:
                ch_name = channel_names.get(ch_id, ch_id)
                invite_link = channel_invite_links.get(ch_id, "")
                msg += f"• **{ch_name}**\n"
                if invite_link:
                    msg += f"  👉 [ဝင်ရန် နှိပ်ပါ]({invite_link})\n"
                msg += "\n"
            msg += f"⚠️ **သင်သည် လင့်ကို {new_count}/{MAX_CLICKS} ကြိမ် နှိပ်ပြီးဖြစ်သည်။**\n"
            msg += f"Channel များအားလုံးဝင်ပြီးနောက် လင့်ကို ထပ်မံနှိပ်ပါ။"

            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

            # If clicks reach MAX_CLICKS -> block user
            if new_count >= MAX_CLICKS:
                block_user(user_id, f"Clicked link {MAX_CLICKS} times without joining all channels")
                await update.message.reply_text(
                    "🙏 **လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည်မဟုတ်ပါ။**\n\n"
                    "ဝမ်းနည်းပါတယ်ရှင့် လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
                    "သင်ချန်နယ်အားလုံးကိုဝင်ရောက်ပြီး ဇာတ်ကားလင့်ကို ပြန်နှိပ်ဖို့ အကြံပြုပါရစေ။\n"
                    "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏",
                    parse_mode="Markdown"
                )
            return

        # All channels joined -> unblock if previously blocked, then deliver file
        if is_user_blocked(user_id):
            unblock_user(user_id)
            await update.message.reply_text("✅ သင်၏ တားမြစ်ချက်ကို ဖယ်ရှားလိုက်ပါပြီ။ ယခု ဖိုင်ကို ပို့ပေးပါမည်။")

        file_id = file_info["file_id"]
        file_name = file_info["file_name"]
        try:
            await update.message.reply_text(f"📁 **ဖိုင်အမည်:** {file_name}\n\nဖိုင်ကို ပို့ပေးနေပါပြီ...")
            await context.bot.send_document(
                chat_id=user_id,
                document=file_id,
                filename=file_name,
                caption=f"📄 {file_name}"
            )
            add_user(user_id)
            increment_requests()
            reset_click_count(user_id)  # reset after successful download
        except Exception as e:
            await update.message.reply_text(f"❌ ဖိုင်ပို့ရာတွင် အမှား: {str(e)}")
    else:
        # Normal /start
        if is_admin(user_id):
            await update.message.reply_text(
                "🤖 **File Share Bot**\n\n"
                "အောက်ပါ Command များကို သုံးနိုင်ပါသည်။\n\n"
                "/newfile - 🆕 ဖိုင်အသစ် တင်ရန် (Admin သာ)\n"
                "/stats - 📊 စာရင်းအင်းကြည့်ရန်\n"
                "/broadcast - 📢 အသုံးပြုသူအားလုံးကို စာပို့ရန်\n"
                "/blocklist - 🚫 Block ထားသောစာရင်း\n"
                "/unblock <user_id> - 🔓 Block ဖြုတ်ရန်\n"
                "/mute - 🔇 Maintenance mode ဖွင့်ရန်\n"
                "/unmute - 🔊 Maintenance mode ပိတ်ရန်\n"
                "/menu - 📋 Command များပြန်မြင်ရန်",
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

# ---------- Menu Command ----------
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text(
            "📋 **Admin Commands**\n\n"
            "/newfile - ဖိုင်အသစ်တင်ရန်\n"
            "/stats - စာရင်းအင်း\n"
            "/broadcast - စာတိုပြန်လွှင့်ရန်\n"
            "/blocklist - Block စာရင်း\n"
            "/unblock <id> - Block ဖြုတ်ရန်\n"
            "/mute - Maintenance mode\n"
            "/unmute - ပြန်ဖွင့်ရန်\n"
            "/menu - ဤစာရင်းပြန်မြင်ရန်",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "📋 **User Commands**\n\n"
            "သင့်တွင် သုံးနိုင်သော command မရှိပါ။\n"
            "ဖိုင်ရယူရန် သင့်အား ပေးထားသော link ကို နှိပ်ပါ။",
            parse_mode="Markdown"
        )

# ---------- /newfile (Admin) ----------
async def newfile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📤 ဖိုင် (Document, Video, Audio, Photo) ပို့ပါ။")
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
        await update.message.reply_text("❌ ဖိုင်တစ်ခု ပို့ပါ။")
        return 1

    try:
        payload = generate_payload()
        save_file_info(payload, file_obj.file_id, file_name)
        deep_link = create_deep_linked_url(BOT_USERNAME, payload)
        await update.message.reply_text(
            f"✅ **ဖိုင် တင်ပြီးပါပြီ**\n\n"
            f"**ဖိုင်အမည်:** {file_name}\n"
            f"**Deep Link:**\n{deep_link}\n\n"
            f"ဤလင့်ကို မျှဝေပါ။\n"
            f"သုံးစွဲသူများ ချန်နယ် ၄ ခုလုံးဝင်မှ ရယူနိုင်မည်။",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text(f"❌ ဖိုင်သိမ်းရာတွင် အမှား: {str(e)}")
    return ConversationHandler.END

async def cancel_newfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက်ပယ်ဖျက်ပြီး။")
    return ConversationHandler.END

# ---------- Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူ: {total_users}\n📥 တောင်းဆိုမှု: {total_requests}", parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("📢 /broadcast <message>")
        return
    users = [doc["user_id"] for doc in users_collection.find({}, {"user_id": 1})]
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 ပို့ပြီး။ လက်ခံသူ {count} ဦး။")

async def blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    blocked = get_blocked_users()
    if not blocked:
        await update.message.reply_text("🚫 Block ထားသူမရှိသေးပါ။")
        return
    text = "🚫 **Block ထားသူများ**\n\n"
    for u in blocked:
        text += f"• User ID: `{u['user_id']}`\n   Reason: {u.get('reason', 'N/A')}\n   Time: {u.get('blocked_at', 'N/A')}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("❗ /unblock <user_id>")
        return
    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("❌ User ID နံပါတ်သာ ထည့်ပါ။")
        return
    if is_user_blocked(uid):
        unblock_user(uid)
        await update.message.reply_text(f"✅ User `{uid}` အား unblock လုပ်ပြီး။", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ User `{uid}` သည် block မခံရပါ။", parse_mode="Markdown")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode **ဖွင့်**ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode **ပိတ်**ထားပါသည်။")

# ---------- Set Persistent Menu (Bot Commands) ----------
async def set_bot_commands(application: Application):
    commands = [
        BotCommand("newfile", "🆕 ဖိုင်အသစ်တင်ရန် (Admin)"),
        BotCommand("stats", "📊 စာရင်းအင်းကြည့်ရန်"),
        BotCommand("broadcast", "📢 အသုံးပြုသူအားလုံးကို စာပို့ရန်"),
        BotCommand("blocklist", "🚫 Block ထားသူစာရင်း"),
        BotCommand("unblock", "🔓 Block ဖြုတ်ရန် (Admin)"),
        BotCommand("mute", "🔇 Maintenance mode ဖွင့်"),
        BotCommand("unmute", "🔊 Maintenance mode ပိတ်"),
        BotCommand("menu", "📋 Command များပြန်မြင်ရန်"),
    ]
    await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info("Bot commands set.")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()
application.post_init = set_bot_commands

conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newfile', newfile_start)],
    states={1: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_file)]},
    fallbacks=[CommandHandler('cancel', cancel_newfile)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("menu", menu))
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
            logger.exception(f"Bot crashed: {e}. Restarting in 10s")
            import time
            time.sleep(10)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
