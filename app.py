import os
import asyncio
import threading
import logging
import sys
import secrets
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
    logger.error("MONGO_URI not set")
    sys.exit(1)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_share_bot"]
file_store_collection = db["file_store"]
users_collection = db["users"]
stats_collection = db["stats"]
blocked_collection = db["blocked"]          # {user_id: reason, blocked_at}
hit_count_collection = db["hit_counts"]      # {user_id: count}

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

def get_hit_count(user_id):
    doc = hit_count_collection.find_one({"_id": user_id})
    return doc["count"] if doc else 0

def increment_hit_count(user_id):
    hit_count_collection.update_one({"_id": user_id}, {"$inc": {"count": 1}}, upsert=True)

def reset_hit_count(user_id):
    hit_count_collection.update_one({"_id": user_id}, {"$set": {"count": 0}}, upsert=True)

def block_user(user_id, reason="Too many attempts without joining channels"):
    blocked_collection.update_one({"_id": user_id}, {"$set": {"reason": reason, "blocked_at": datetime.now()}}, upsert=True)

def unblock_user(user_id):
    blocked_collection.delete_one({"_id": user_id})

def is_blocked(user_id):
    return blocked_collection.find_one({"_id": user_id}) is not None

def get_blocked_users():
    return list(blocked_collection.find({}, {"_id": 1, "reason": 1, "blocked_at": 1}))

# ---------- Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
REQUIRED_CHANNELS = [ch.strip() for ch in os.environ.get("REQUIRED_CHANNELS", "").split(",") if ch.strip()]
CHANNEL_NAMES = [name.strip() for name in os.environ.get("CHANNEL_NAMES", "").split(",") if name.strip()]
CHANNEL_INVITE_LINKS = [link.strip() for link in os.environ.get("CHANNEL_INVITE_LINKS", "").split(",") if link.strip()]
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

MAX_HITS = 10   # max attempts before block

def is_admin(user_id):
    return user_id in ADMIN_IDS

maintenance_mode = False

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
    invite_links = []
    names = []
    for i, ch in enumerate(REQUIRED_CHANNELS):
        if not await is_member_of_channel(user_id, ch, context):
            missing.append(ch)
            if i < len(CHANNEL_INVITE_LINKS):
                invite_links.append(CHANNEL_INVITE_LINKS[i])
            else:
                invite_links.append(None)
            if i < len(CHANNEL_NAMES):
                names.append(CHANNEL_NAMES[i])
            else:
                names.append(ch)
    return (len(missing) == 0, missing, invite_links, names)

# ---------- /start (Deep Link Handler) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # If user is blocked, reject immediately
    if is_blocked(user_id):
        await update.message.reply_text(
            "🚫 သင်သည် ယခင်က လင့်ကို ၁၀ ကြိမ်ကျော် နှိပ်ခဲ့ပြီး ချန်နယ်များမဝင်ထားသောကြောင့် ပိတ်ပင်ခံထားရသည်။\n"
            "ကျေးဇူးပြု၍ လိုအပ်သော Channel များအားလုံးကို ဝင်ရောက်ပြီးနောက် အောက်ပါလင့်ကို ထပ်မံနှိပ်ပါ။"
        )
        return

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_info = get_file_info(payload)
        if not file_info:
            await update.message.reply_text("❌ လင့်မမှန်ပါ။")
            return

        # Increase hit count
        current_hits = get_hit_count(user_id) + 1
        increment_hit_count(user_id)

        # Check membership
        all_ok, missing, invite_links, channel_names = await check_all_channels(user_id, context)

        if all_ok:
            # Success: reset hit count, unblock if any, send file
            if is_blocked(user_id):
                unblock_user(user_id)
            reset_hit_count(user_id)

            file_id = file_info["file_id"]
            file_name = file_info["file_name"]
            try:
                await update.message.reply_text(f"📁 {file_name} ပို့ပေးနေပါပြီ...")
                await context.bot.send_document(
                    chat_id=user_id,
                    document=file_id,
                    filename=file_name,
                    caption=f"📄 {file_name}"
                )
                add_user(user_id)
                increment_requests()
            except Exception as e:
                await update.message.reply_text(f"❌ ဖိုင်ပို့ရာတွင် အမှား: {str(e)}")
        else:
            # Not all channels joined
            if current_hits >= MAX_HITS:
                # Block user
                block_user(user_id, f"Exceeded {MAX_HITS} attempts without joining all channels")
                await update.message.reply_text(
                    "လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည်မဟုတ်ပါ။ ဝမ်းနည်းပါတယ်ရှင့်\n\n"
                    "လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
                    "သင်သည် ချန်နယ်အားလုံးကိုဝင်ရောက်ပြီး ဇာတ်ကားလင့်ကို ပြန်နှိပ်ဖို့ အကြံပြုပါရစေ။\n"
                    "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏"
                )
                return

            # Build readable channel list
            msg = "🔒 **ဇာတ်ကားဖိုင်ကို ဒေါင်းလုဒ်လုပ်ရန် ကျေးဇူးပြု၍ အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါနော်**\n\n"
            for i, ch in enumerate(missing):
                name = channel_names[i] if i < len(channel_names) else ch
                msg += f"📢 **Channel:** {name}\n"
                if invite_links[i]:
                    msg += f"👉 [ဝင်ရန် နှိပ်ပါ]({invite_links[i]})\n"
                msg += "\n"
            remaining = MAX_HITS - current_hits
            msg += f"⚠️ သင်သည် လင့်ကို {current_hits}/{MAX_HITS} ကြိမ် နှိပ်ပြီးဖြစ်သည်။ {remaining} ကြိမ်သာ ကျန်ပါသေးသည်။\n"
            msg += "Channel များအားလုံးဝင်ပြီးနောက် လင့်ကို ထပ်မံနှိပ်ပါ။"
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
    else:
        # Normal /start - show menu if admin
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

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin menu"""
    await update.message.reply_text(
        "🤖 **File Share Bot**\n\n"
        "အောက်ပါ Command များကို သုံးနိုင်ပါသည်။\n\n"
        "/newfile - 🆕 ဖိုင်အသစ် တင်ရန် (Admin သာ)\n"
        "/stats - 📊 စာရင်းအင်းကြည့်ရန်\n"
        "/broadcast - 📢 အသုံးပြုသူအားလုံးကို စာပို့ရန်\n"
        "/blocklist - 🚫 ဘလော့ထားတဲ့လူစာရင်း\n"
        "/unblock <user_id> - 🔓 ဘလော့ဖြုတ်ရန်\n"
        "/mute - 🔇 Maintenance mode ဖွင့်ရန်\n"
        "/unmute - 🔊 Maintenance mode ပိတ်ရန်",
        parse_mode="Markdown"
    )

# ---------- /newfile Admin ----------
async def newfile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📤 ဖိုင် (Video, Document, Audio, Photo) ပို့ပါ။")
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
        # No parse_mode to avoid markdown errors
        await update.message.reply_text(
            f"✅ **File stored successfully**\n\n"
            f"Name: {file_name}\n"
            f"Deep Link:\n{deep_link}\n\n"
            f"Share this link. Users must join all required channels first."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    return ConversationHandler.END

async def cancel_newfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ---------- Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(
        f"📊 **Statistics**\n\n👥 Users: {total_users}\n📥 Total requests: {total_requests}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    users = get_all_users()
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 Broadcast sent to {count} users.")

async def blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    blocked = get_blocked_users()
    if not blocked:
        await update.message.reply_text("No blocked users.")
        return
    text = "🚫 **Blocked users:**\n\n"
    for b in blocked:
        text += f"• User ID: `{b['_id']}`\n  Reason: {b.get('reason', 'N/A')}\n  Since: {b.get('blocked_at', 'unknown')}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <user_id>")
        return
    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("Invalid user ID")
        return
    if is_blocked(uid):
        unblock_user(uid)
        reset_hit_count(uid)
        await update.message.reply_text(f"✅ User {uid} has been unblocked and hit count reset.")
    else:
        await update.message.reply_text(f"User {uid} is not blocked.")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode ENABLED. Users cannot download files.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode DISABLED.")

async def placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Command not available.")

# ---------- Set bot commands for menu ----------
async def set_commands(application):
    commands = [
        BotCommand("newfile", "🆕 Upload new file (Admin)"),
        BotCommand("stats", "📊 View statistics"),
        BotCommand("broadcast", "📢 Send message to all users"),
        BotCommand("blocklist", "🚫 Show blocked users"),
        BotCommand("unblock", "🔓 Unblock a user (provide ID)"),
        BotCommand("mute", "🔇 Enable maintenance mode"),
        BotCommand("unmute", "🔊 Disable maintenance mode"),
    ]
    await application.bot.set_my_commands(commands)

# ---------- Main ----------
application = Application.builder().token(TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newfile', newfile_start)],
    states={1: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_file)]},
    fallbacks=[CommandHandler('cancel', cancel_newfile)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(conv_handler)
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("help", show_menu))
application.add_handler(CommandHandler("menu", show_menu))

# ---------- Polling ----------
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("Starting bot polling...")
    application.run_polling()

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Set commands before starting
    asyncio.run(set_commands(application))
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
