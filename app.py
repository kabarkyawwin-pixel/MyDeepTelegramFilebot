import os
import asyncio
import threading
import logging
import sys
import secrets
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient
from telegraph import Telegraph

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

# ---------- Telegram Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

# Required Channels
REQUIRED_CHANNELS = [
    {"id": "-1003753299714", "name": "🎬 Movies channel main (HD Movies များ)", "invite": "https://t.me/wznmoviescollector"},
    {"id": "-1003899625672", "name": "🎬 Movies channel 2 (အရံချန်နယ်)", "invite": "https://t.me/moviesandseriesforallwzn"},
    {"id": "-1003792838735", "name": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ် (ကလေးများမဝင်ရ)", "invite": "https://t.me/everyboyhobby"},
    {"id": "-1003785717514", "name": "🎵 မြန်မာသီချင်းချန်နယ်", "invite": "https://t.me/wznmusiclibary"}
]

OTHER_CHANNELS = [link.strip() for link in os.environ.get("OTHER_CHANNELS", "").split(",") if link.strip()] if os.environ.get("OTHER_CHANNELS") else []
MUSIC_CHANNEL_LINK = os.environ.get("MUSIC_CHANNEL_LINK", "")

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

async def create_telegraph_page(title: str, content_text: str) -> str:
    try:
        html_content = content_text.replace('\n', '<br>')
        response = await asyncio.to_thread(
            telegraph.create_page,
            title=title,
            html_content=f"<p>{html_content}</p>",
            author_name="File Share Bot"
        )
        return response['url']
    except Exception as e:
        logger.error(f"Telegraph error: {e}")
        return None

# ---------- Start & Deep Link Handler ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_info = get_file_info(payload)
        if not file_info:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return

        if is_user_blocked(user_id):
            await update.message.reply_text(
                "🔒 လူကြီးမင်းသည် ချန်နယ်များကို မဝင်ဘဲ လင့်ကို ၁၀ ကြိမ်အထက်နှိပ်ထားသည့်အတွက် ကျွန်ုပ်က block လုပ်ထားပါသည်။\n"
                "ကျေးဇူးပြု၍ လိုအပ်သော ချန်နယ်များအားလုံးကို ဝင်ပြီးနောက် ကျွန်ုပ်ထံ ဆက်သွယ်ပါ။"
            )
            return

        all_joined, missing = await check_all_channels(user_id, context)
        if not all_joined:
            increment_attempts(user_id)
            attempts = get_attempt_count(user_id)
            remaining = 10 - attempts

            msg = "🎬 **ဇာတ်ကားဖိုင်ကို ဒေါင်းလုဒ်လုပ်ရန် အောက်ပါ Channel များအားလုံးကို ဝင်ထားပေးပါနော်**\n\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• **{ch['name']}**\n"
                msg += f"  👉 [ဝင်ရန် နှိပ်ပါ]({ch['invite']})\n\n"
            msg += f"⚠️ သင်သည် ဤလင့်ကို **{attempts}/10** ကြိမ် နှိပ်ပြီးဖြစ်သည်။ {remaining} ကြိမ်သာ ကျန်ပါသေးသည်။\n"
            msg += "Channel များအားလုံးဝင်ပြီးနောက် လင့်ကို ထပ်မံနှိပ်ပါ။"

            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

            if attempts >= 10:
                block_user(user_id)
                block_msg = (
                    "🚫 **လူကြီးမင်းသည် ချန်နယ်ကို မဝင်ဘဲ ဇာတ်ကားလင့်ကို ၁၀ ကြိမ်နှိပ်လိုက်သည့်အတွက် ဇာတ်ကားရယူနိုင်မည် မဟုတ်ပါ။**\n\n"
                    "ဝမ်းနည်းပါတယ်ရှင့် လူကြီးမင်းကို ကျွန်ုပ်၏ဘက်မှ block လိုက်ပါသည်။\n"
                    "သာယာပျော်ရွင်သောနေ့လေးဖြစ်ပါစေ 🙏🙏🙏"
                )
                await update.message.reply_text(block_msg)
            return

        # All channels joined
        if is_user_blocked(user_id):
            unblock_user(user_id)
            await update.message.reply_text("✅ သင်သည် လိုအပ်သောချန်နယ်များအားလုံးကို ဝင်ရောက်ထားပြီးဖြစ်သောကြောင့် သင့်အား unblock လုပ်လိုက်ပါသည်။")

        file_id = file_info["file_id"]
        file_name = file_info["file_name"]

        try:
            await update.message.reply_text(f"🎬 {file_name} ပို့ပေးနေပါပြီ...")
            video_msg = await context.bot.send_video(
                chat_id=user_id,
                video=file_id,
                caption=f"🎬 သင့်ဇာတ်ကား - {file_name}"
            )
            warning_text = (
                "⚠️ ⚠️ ⚠️ အရေးကြီးပါတယ် ⚠️ ⚠️ ⚠️\n\n"
                "ဤရုပ်ရှင်ဖိုင်များ/ဗီဒီယိုများကို 5 မိနစ်အတွင်း (မူပိုင်ခွင့်ပြဿနာများကြောင့်) ဖျက်ပါမည်။\n\n"
                "ကျေးဇူးပြု၍ ဤဖိုင်များ/ဗီဒီယိုများအားလုံးကို သင်၏ Saved Messages များသို့ Forward လုပ်ပြီး ထိုနေရာတွင် ဇာတ်ကားအား ကြည့်ရှုပါ။\n\n"
                "ကျွန်ုပ်၏ Channel ကို လာရောက်အားပေးမှုအတွက် ကျေးဇူးအထူးတင်ပါတယ် 🙏🙏🙏\n\n"
                "Channel ရေရှည်တည်တံ့ဖို့အတွက် Support ပေးချင်ပါက Wave Pay (09767011991) ကို ကူညီနိုင်ပါတယ်။\n\n"
                "အားလုံးကို ကျေးဇူးတင်ပါတယ်။\n\n!!! IMPORTANT !!!\n"
                "This Movie Files/Videos will be deleted in 5 mins (Due to Copyright Issues).\n"
                "Please forward these ALL Files/Videos to your Saved Messages and start downloading there."
            )
            warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text)

            async def delete_after():
                await asyncio.sleep(300)
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                    await context.bot.delete_message(chat_id=user_id, message_id=video_msg.message_id)
                except:
                    pass
            asyncio.create_task(delete_after())

            add_user(user_id)
            increment_requests()
            reset_attempts(user_id)

            # Additional channel buttons
            keyboard = []
            if OTHER_CHANNELS:
                for idx, link in enumerate(OTHER_CHANNELS, 1):
                    if idx == 1:
                        keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
                    elif idx == 2:
                        keyboard.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
                    elif idx == 3:
                        keyboard.append([InlineKeyboardButton("🎵 မြန်မာသီချင်း ချန်နယ်", url=link)])
                    else:
                        keyboard.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
            if MUSIC_CHANNEL_LINK:
                keyboard.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])

            if keyboard:
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
        except Exception as e:
            await context.bot.send_message(chat_id=user_id, text=f"❌ Video ပို့ရာတွင် အမှား: {str(e)}")
    else:
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text(
                "🎬 **မင်္ဂလာပါ**\n\n"
                "ဤ Bot သည် Channel အတွက် ဇာတ်ကားများ ဖြန့်ဝေရန် သုံးပါသည်။\n"
                "ဇာတ်ကားရယူရန် Channel ရှိ Post အောက်က ခလုတ်ကို နှိပ်ပါ။\n"
                "ပထမဆုံး လိုအပ်သော Channel 4 ခုလုံးကို ဝင်ရောက်ထားရပါမည်။",
                parse_mode="Markdown"
            )

# ---------- Admin Menu ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🆕 New Post", callback_data="menu_newpost")],
        [InlineKeyboardButton("🔗 New File (Deep Link)", callback_data="menu_newfile")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="menu_broadcast")],
        [InlineKeyboardButton("🚫 Blocklist", callback_data="menu_blocklist")],
        [InlineKeyboardButton("🔇 Mute", callback_data="menu_mute")],
        [InlineKeyboardButton("🔊 Unmute", callback_data="menu_unmute")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🤖 **Admin Menu**\n\nအောက်ပါခလုတ်များကို နှိပ်ပါ။", reply_markup=reply_markup, parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return

    data = query.data
    if data == "menu_newpost":
        await query.edit_message_text("📸 /newpost command ကို သုံးပါ။")
    elif data == "menu_newfile":
        await query.edit_message_text("🔗 /newfile command ကို သုံးပါ။ (Video ပို့ပါက Deep Link ရမည်)")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_requests = get_total_requests()
        await query.edit_message_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n🎬 တောင်းဆိုမှုအရေအတွက်: {total_requests}", parse_mode="Markdown")
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
        maintenance_mode = True
        await query.edit_message_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။")
    elif data == "menu_unmute":
        maintenance_mode = False
        await query.edit_message_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

# ---------- /newpost Command (Post ဖန်တီးခြင်း + Deep Link ထုတ်ပေးခြင်း) ----------
POSTER, CAPTION, VIDEO_FILE = range(3)

async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ဇာတ်ကားအတွက် ပုံတစ်ပုံ ပို့ပေးပါ...")
    return POSTER

async def receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("✍️ ဇာတ်ကားအကြောင်း စာသား (ဇာတ်ညွှန်း) ရေးပေးပါ...\n(စာသားရှည်ပါက Telegraph တွင် အလိုအလျောက် တင်ပေးပါမည်)")
    return CAPTION

async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption_text = update.message.text
    context.user_data['caption_full'] = caption_text
    context.user_data['telegraph_url'] = None

    if len(caption_text) > 1024:
        await update.message.reply_text("⏳ စာသားရှည်နေပါသည်။ Telegraph စာမျက်နှာ ဖန်တီးနေပါပြီ...")
        try:
            title = f"Movie Synopsis - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            page_url = await create_telegraph_page(title, caption_text)
            if page_url:
                context.user_data['telegraph_url'] = page_url
                await update.message.reply_text(f"✅ Telegraph စာမျက်နှာ ဖန်တီးပြီးပါပြီ။\n\nဇာတ်ညွှန်းအပြည့်အစုံကို ဤလင့်တွင် ဖတ်ရှုနိုင်ပါသည်။\n{page_url}")
            else:
                await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် အမှားရှိသည်။ စာသားကို ဆက်လက်အသုံးပြုပါမည်။")
        except Exception as e:
            logger.error(f"Telegraph error: {e}")
            await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် ချို့ယွင်းချက်ရှိသည်။")
    else:
        pass

    await update.message.reply_text("🎬 Video File ကို ပို့ပေးပါ...")
    return VIDEO_FILE

async def receive_video_for_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith('video/'):
        video = update.message.document

    if not video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ (video file သို့မဟုတ် video document)။")
        return VIDEO_FILE

    try:
        file_name = getattr(video, 'file_name', None)
        if not file_name:
            file_name = "ဇာတ်ကား"

        payload = generate_payload()
        save_file_info(payload, video.file_id, file_name)
        deep_link = create_deep_linked_url(BOT_USERNAME, payload)

        # Build buttons for the post
        buttons = []
        buttons.append([InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)])
        synopsis_url = context.user_data.get('telegraph_url')
        if synopsis_url:
            buttons.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်အစုံ ဖတ်ရန်", url=synopsis_url)])
        if OTHER_CHANNELS:
            for idx, link in enumerate(OTHER_CHANNELS, 1):
                if idx == 1:
                    buttons.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
                elif idx == 2:
                    buttons.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
                elif idx == 3:
                    buttons.append([InlineKeyboardButton("🎵 မြန်မာသီချင်း ချန်နယ်", url=link)])
                else:
                    buttons.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
        if MUSIC_CHANNEL_LINK:
            buttons.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])

        reply_markup = InlineKeyboardMarkup(buttons)

        poster = context.user_data.get('poster')
        caption_full = context.user_data.get('caption_full', '')
        telegraph_url = context.user_data.get('telegraph_url')

        if not poster:
            await update.message.reply_text("ပုံ မတွေ့ပါ။ /newpost ကို ထပ်မံစတင်ပါ။")
            return ConversationHandler.END

        # Create post preview (photo with caption and buttons)
        if telegraph_url:
            preview = caption_full[:300] + "..." if len(caption_full) > 300 else caption_full
            photo_caption = f"📝 ဇာတ်ကားအကျဉ်းချုပ်\n\n{preview}\n\n🔗 အပြည့်အစုံဖတ်ရန်: {telegraph_url}"
        else:
            photo_caption = f"📝 ဇာတ်ကားအကြောင်း\n\n{caption_full}"

        await update.message.reply_photo(
            photo=poster,
            caption=photo_caption,
            reply_markup=reply_markup
        )

        # Also send the deep link to admin for reference
        await update.message.reply_text(
            f"✅ **Post ဖန်တီးပြီးပါပြီ။**\n\n"
            f"ဤ Post ကို Forward လုပ်ပြီး Channel မှာ တင်လိုက်ပါ။\n\n"
            f"**Deep Link (ဇာတ်ကားရယူရန်):**\n{deep_link}\n\n"
            f"ဤလင့်ကို ကူးယူ၍လည်း အသုံးပြုနိုင်ပါသည်။"
        )

        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Post ဖန်တီးရာတွင် အမှား: {str(e)}")
        return ConversationHandler.END

async def cancel_newpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- /newfile Command (Admin က Video ပို့ → Deep Link) ----------
async def newfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await update.message.reply_text("📤 Video file တစ်ခု ပို့ပေးပါ။ (ဒီ Video အတွက် Deep Link ထုတ်ပေးပါမည်)")
    context.user_data['waiting_for_newfile'] = True

async def handle_video_for_newfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get('waiting_for_newfile'):
        video = update.message.video
        if video:
            try:
                payload = generate_payload()
                file_name = video.file_name or "ဇာတ်ကား"
                save_file_info(payload, video.file_id, file_name)
                deep_link = create_deep_linked_url(BOT_USERNAME, payload)
                await update.message.reply_text(
                    f"🔗 **သင်၏ Deep Link**\n\n{deep_link}\n\n"
                    f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် `{file_name}` ကို ရရှိမည်။\n"
                    f"(Channel 4 ခုစလုံးဝင်ထားရန် လိုအပ်)"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Deep Link ထုတ်ရာတွင် အမှား: {str(e)}")
            context.user_data.pop('waiting_for_newfile', None)
        else:
            await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")

# ---------- /link Command (အဟောင်း – လည်း အလုပ်လုပ်မယ်) ----------
async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await update.message.reply_text("📤 Video file တစ်ခု ပို့ပေးပါ။ (ဒီ Video အတွက် Deep Link ထုတ်ပေးပါမည်)")
    context.user_data['waiting_for_link'] = True

async def handle_video_for_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get('waiting_for_link'):
        video = update.message.video
        if video:
            try:
                payload = generate_payload()
                file_name = video.file_name or "ဇာတ်ကား"
                save_file_info(payload, video.file_id, file_name)
                deep_link = create_deep_linked_url(BOT_USERNAME, payload)
                await update.message.reply_text(
                    f"🔗 **သင်၏ Deep Link**\n\n{deep_link}\n\n"
                    f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် `{file_name}` ကို ရရှိမည်။\n"
                    f"(Channel 4 ခုစလုံးဝင်ထားရန် လိုအပ်)"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Deep Link ထုတ်ရာတွင် အမှား: {str(e)}")
            context.user_data.pop('waiting_for_link', None)
        else:
            await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")

# ---------- Other Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n🎬 တောင်းဆိုမှုအရေအတွက်: {total_requests}", parse_mode="Markdown")

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
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await show_menu(update, context)

# ---------- Placeholder commands ----------
async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⏳ အချိန်ဇယား (လုပ်ဆောင်ဆဲ)")
async def listschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📋 အချိန်ဇယားစာရင်း (လုပ်ဆောင်ဆဲ)")
async def cancelschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("❌ အချိန်ဇယားဖျက်ရန် (လုပ်ဆောင်ဆဲ)")
async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🗑️ ဖိုင်ဖျက်ရန် (လုပ်ဆောင်ဆဲ)")
async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⚠️ အားလုံးဖျက်ရန် (လုပ်ဆောင်ဆဲ)")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# Conversation for /newpost
newpost_handler = ConversationHandler(
    entry_points=[CommandHandler('newpost', newpost_start)],
    states={
        POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
        CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
        VIDEO_FILE: [
            MessageHandler(filters.VIDEO, receive_video_for_post),
            MessageHandler(filters.Document.VIDEO, receive_video_for_post)
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_newpost)],
)

# Add all handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(newpost_handler)
application.add_handler(CommandHandler("newfile", newfile_command))
application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video_for_newfile))
application.add_handler(CommandHandler("link", link_command))
application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video_for_link))
application.add_handler(CommandHandler("menu", menu_command))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("blocklist", blocklist))
application.add_handler(CommandHandler("unblock", unblock))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))
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
