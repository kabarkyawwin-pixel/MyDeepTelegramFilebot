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
blocked_collection = db["blocked_users"]   # {user_id: True, attempts: N}

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
        users_collection.insert_one({"user_id": user_id, "first_seen": datetime.now()})

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

# ---------- Blocked users helpers ----------
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

# ---------- Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

# Channel 4 ခု (ID, Name, Invite Link) - သင့်အတိုင်း configure လုပ်ထား
REQUIRED_CHANNELS = [
    {"id": "-1003753299714", "name": "🎬 Movies channel main (HD Movies များ)", "invite": "https://t.me/wznmoviescollector"},
    {"id": "-1003899625672", "name": "🎬 Movies channel 2 (အရံချန်နယ်)", "invite": "https://t.me/moviesandseriesforallwzn"},
    {"id": "-1003792838735", "name": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ် (ကလေးများမဝင်ရ)", "invite": "https://t.me/everyboyhobby"},
    {"id": "-1003785717514", "name": "🎵 မြန်မာသီချင်းချန်နယ်", "invite": "https://t.me/wznmusiclibary"}
]

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
    """Returns (all_joined, missing_channels_info_list)"""
    missing = []
    for ch in REQUIRED_CHANNELS:
        if not await is_member_of_channel(user_id, ch["id"], context):
            missing.append(ch)
    return (len(missing) == 0, missing)

# ---------- Start & Deep Link Handler ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_info = get_file_info(payload)

        if not file_info:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return

        # Check if user is already blocked
        if is_user_blocked(user_id):
            await update.message.reply_text(
                "🔒 လူကြီးမင်းသည် ချန်နယ်များကို မဝင်ဘဲ လင့်ကို ၁၀ ကြိမ်အထက်နှိပ်ထားသည့်အတွက် ကျွန်ုပ်က block လုပ်ထားပါသည်။\n"
                "ကျေးဇူးပြု၍ လိုအပ်သော ချန်နယ်များအားလုံးကို ဝင်ပြီးနောက် ကျွန်ုပ်ထံ ဆက်သွယ်ပါ။"
            )
            return

        # Check membership
        all_joined, missing = await check_all_channels(user_id, context)

        if not all_joined:
            # Increment attempts
            increment_attempts(user_id)
            attempts = get_attempt_count(user_id)
            remaining = 10 - attempts

            # Build message with channel list
            msg = "🎬 **ဇာတ်ကားဖိုင်ကို ဒေါင်းလုဒ်လုပ်ရန် အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါနော်**\n\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• **{ch['name']}**\n"
                msg += f"  👉 [ဝင်ရန် နှိပ်ပါ]({ch['invite']})\n\n"
            msg += f"⚠️ သင်သည် ဤလင့်ကို **{attempts}/10** ကြိမ် နှိပ်ပြီးဖြစ်သည်။ {remaining} ကြိမ်သာ ကျန်ပါသေးသည်။\n"
            msg += "Channel များအားလုံးဝင်ပြီးနောက် လင့်ကို ထပ်မံနှိပ်ပါ။"

            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

            # If attempts reached 10, block user
            if attempts >= 10:
                block_user(user_id)
                block_msg = (
                    "🚫 **လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည် မဟုတ်ပါ။**\n\n"
                    "ဝမ်းနည်းပါတယ်ရှင့် လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
                    "သင်သည် ချန်နယ်အားလုံးကို ဝင်ရောက်ပြီး ဇာတ်ကားလင့်ကို ပြန်နှိပ်ဖို့ အကြံပြုပါရစေ။\n"
                    "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏"
                )
                await update.message.reply_text(block_msg)
            return

        # All channels joined - grant access
        # If previously blocked, unblock now
        if is_user_blocked(user_id):
            unblock_user(user_id)
            await update.message.reply_text("✅ သင်သည် လိုအပ်သောချန်နယ်များအားလုံးကို ဝင်ရောက်ထားပြီးဖြစ်သောကြောင့် သင့်အား unblock လုပ်လိုက်ပါသည်။")

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
            reset_attempts(user_id)  # reset attempts on success
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

# ---------- Menu (Inline Keyboard) ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🆕 New File", callback_data="menu_newfile")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="menu_broadcast")],
        [InlineKeyboardButton("🚫 Blocklist", callback_data="menu_blocklist")],
        [InlineKeyboardButton("🔇 Mute", callback_data="menu_mute")],
        [InlineKeyboardButton("🔊 Unmute", callback_data="menu_unmute")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🤖 **Admin Menu**\n\nအောက်ပါခလုတ်များကို နှိပ်ပါ။", reply_markup=reply_markup, parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return

    data = query.data
    if data == "menu_newfile":
        await query.edit_message_text("📤 ဖိုင်တစ်ခု ပို့ပေးပါ (Admin အတွက်)")
        context.user_data['waiting_for_file'] = True
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_requests = get_total_requests()
        await query.edit_message_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n📥 တောင်းဆိုမှုအရေအတွက်: {total_requests}", parse_mode="Markdown")
    elif data == "menu_broadcast":
        await query.edit_message_text("📢 /broadcast <message> ဖြင့် အသုံးပြုသူအားလုံးကို စာပို့နိုင်ပါသည်။")
    elif data == "menu_blocklist":
        blocked = get_blocked_users()
        if not blocked:
            await query.edit_message_text("📊 လောလောဆယ် block ထားသူ မရှိပါ။")
        else:
            msg = "🚫 **Blocked Users**\n\n"
            for uid in blocked:
                msg += f"• `{uid}`\n"
            msg += "\n/unblock <user_id> ဖြင့် ပြန်ဖွင့်နိုင်ပါသည်။"
            await query.edit_message_text(msg, parse_mode="Markdown")
    elif data == "menu_mute":
        global maintenance_mode
        maintenance_mode = True
        await query.edit_message_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။ (အသုံးပြုသူများ ဖိုင်ရယူနိုင်မည် မဟုတ်ပါ)")
    elif data == "menu_unmute":
        global maintenance_mode
        maintenance_mode = False
        await query.edit_message_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

# ---------- /newfile Command (Admin only) ----------
async def newfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await update.message.reply_text("📤 ဖိုင်တစ်ခု (ဗီဒီယို၊ စာရွက်စာတမ်း၊ အသံ၊ ပုံ) ကို ပို့ပေးပါ။")
    context.user_data['waiting_for_file'] = True

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get('waiting_for_file'):
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
                f"အသုံးပြုသူများသည် လိုအပ်သော Channel များအားလုံးကို ဝင်ပြီးမှသာ ဖိုင်ကို ရယူနိုင်မည်။"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ ဖိုင် သိမ်းဆည်းရာတွင် အမှား: {str(e)}")
        context.user_data.pop('waiting_for_file', None)

# ---------- Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n📥 တောင်းဆိုမှုအရေအတွက်: {total_requests}", parse_mode="Markdown")

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
    blocked = get_blocked_users()
    if not blocked:
        await update.message.reply_text("📊 လောလောဆယ် block ထားသူ မရှိပါ။")
        return
    msg = "🚫 **Blocked Users**\n\n"
    for uid in blocked:
        msg += f"• `{uid}`\n"
    msg += "\n/unblock <user_id> ဖြင့် ပြန်ဖွင့်နိုင်ပါသည်။"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("📌 /unblock <user_id>")
        return
    try:
        user_id = int(args[0])
        if is_user_blocked(user_id):
            unblock_user(user_id)
            await update.message.reply_text(f"✅ User `{user_id}` ကို unblock လုပ်လိုက်ပါသည်။", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"ℹ️ User `{user_id}` သည် block မခံရသေးပါ။", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ User ID သည် ဂဏန်းသက်သက် ဖြစ်ရပါမည်။")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global maintenance_mode
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။ (အသုံးပြုသူများ ဖိုင်ရယူနိုင်မည် မဟုတ်ပါ)")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global maintenance_mode
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await show_menu(update, context)

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# Handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("newfile", newfile_command))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file))
application.add_handler(CommandHandler("menu", menu_command))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
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
