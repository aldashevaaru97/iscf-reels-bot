#!/usr/bin/env python3
import os, logging, tempfile, asyncio
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
import requests

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
YOUTUBE_CLIENT_SECRETS_FILE = "client_secrets.json"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "")
ALLOWED_USER_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "0").split(",") if x]
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
USER_NAMES = {int(k): v for k, v in (pair.split(":", 1) for pair in os.getenv("USER_NAMES", "").split(",") if ":" in pair)}

WAITING_CAPTION = 1
user_data_store = {}

def get_user_display_name(user_id, tg_user):
    if user_id in USER_NAMES:
        return USER_NAMES[user_id]
    if tg_user:
        return tg_user.full_name or f"ID:{user_id}"
    return f"ID:{user_id}"

def get_youtube_service():
    creds = None
    token_file = "youtube_token.json"
    if Path(token_file).exists():
        creds = google.oauth2.credentials.Credentials.from_authorized_user_file(token_file, YOUTUBE_SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(YOUTUBE_CLIENT_SECRETS_FILE, YOUTUBE_SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def upload_to_youtube(video_path, title, description):
    youtube = get_youtube_service()
    body = {"snippet": {"title": title[:100], "description": description + "\n\n#Shorts", "tags": ["Shorts"], "categoryId": "22"}, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(video_path, mimetype="video/*", resumable=True)
    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
    return f"https://youtube.com/shorts/{response['id']}"

def upload_to_facebook(video_path, description):
    base_url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}"
    init_data = requests.post(f"{base_url}/video_reels", data={"upload_phase": "start", "access_token": FACEBOOK_ACCESS_TOKEN}).json()
    if "error" in init_data:
        raise Exception(f"Facebook init error: {init_data['error']['message']}")
    video_id = init_data["video_id"]
    upload_url = init_data["upload_url"]
    with open(video_path, "rb") as vf:
        requests.post(upload_url, headers={"Authorization": f"OAuth {FACEBOOK_ACCESS_TOKEN}", "offset": "0", "file_size": str(os.path.getsize(video_path))}, data=vf)
    pub_data = requests.post(f"{base_url}/video_reels", data={"upload_phase": "finish", "video_id": video_id, "access_token": FACEBOOK_ACCESS_TOKEN, "video_state": "PUBLISHED", "description": description}).json()
    if "error" in pub_data:
        raise Exception(f"Facebook publish error: {pub_data['error']['message']}")
    return f"https://facebook.com/reel/{video_id}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text("👋 Привет! Отправь мне видео и я опубликую его на:\n  • YouTube Shorts 🎬\n  • Facebook Reels 📘")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return
    if update.message.video:
        file = update.message.video
    elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith("video"):
        file = update.message.document
    else:
        await update.message.reply_text("❌ Пожалуйста, отправь видео файл.")
        return
    await update.message.reply_text("⏳ Скачиваю видео...")
    tg_file = await context.bot.get_file(file.file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    await tg_file.download_to_drive(tmp.name)
    tmp.close()
    user_data_store[user_id] = {"video_path": tmp.name}
    keyboard = [[InlineKeyboardButton("Пропустить — без подписи", callback_data="no_caption")]]
    await update.message.reply_text("✏️ Напиши подпись для видео (или нажми кнопку):", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_CAPTION

async def handle_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store[user_id]["caption"] = update.message.text
    await publish_video(update, context, user_id)
    return ConversationHandler.END

async def handle_no_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data_store[user_id]["caption"] = ""
    await publish_video(update, context, user_id, query=query)
    return ConversationHandler.END

async def publish_video(update, context, user_id, query=None):
    data = user_data_store.get(user_id, {})
    video_path = data.get("video_path")
    caption = data.get("caption", "")
    if not video_path:
        return
    tg_user = update.effective_user if update and update.effective_user else (query.from_user if query else None)
    publisher_name = get_user_display_name(user_id, tg_user)
    send = query.edit_message_text if query else update.message.reply_text
    await send("🚀 Публикую видео...\n\n⏳ YouTube Shorts...\n⏳ Facebook Reels...")
    results, errors = {}, {}
    try:
        yt_url = await asyncio.get_event_loop().run_in_executor(None, upload_to_youtube, video_path, caption[:80] if caption else "Новое видео", caption)
        results["YouTube Shorts"] = yt_url
    except Exception as e:
        errors["YouTube Shorts"] = str(e)
        logger.error(f"YouTube error: {e}")
    try:
        fb_url = await asyncio.get_event_loop().run_in_executor(None, upload_to_facebook, video_path, caption)
        results["Facebook Reels"] = fb_url
    except Exception as e:
        errors["Facebook Reels"] = str(e)
        logger.error(f"Facebook error: {e}")
    lines = [f"✅ *Публикация завершена!*\n👤 *{publisher_name}*\n"]
    for p, url in results.items():
        lines.append(f"✅ *{p}*: [Открыть]({url})")
    for p, err in errors.items():
        lines.append(f"❌ *{p}*: Ошибка — `{err}`")
    lines.append("\n📸 *Instagram* — загрузи вручную как обычно")
    try:
        os.unlink(video_path)
    except:
        pass
    user_data_store.pop(user_id, None)
    reply = query.edit_message_text if query else update.message.reply_text
    await reply("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
    if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
        admin_lines = [f"🔔 *Новая публикация от коллеги!*\n👤 *{publisher_name}*\n"]
        if caption:
            admin_lines.append(f"📝 _{caption[:200]}_\n")
        for p, url in results.items():
            admin_lines.append(f"✅ *{p}*: [Открыть]({url})")
        for p in errors:
            admin_lines.append(f"❌ *{p}*: Ошибка")
        try:
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text="\n".join(admin_lines), parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = user_data_store.pop(user_id, {})
    if data.get("video_path"):
        try:
            os.unlink(data["video_path"])
        except:
            pass
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)],
        states={WAITING_CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_caption), CallbackQueryHandler(handle_no_caption, pattern="^no_caption$")]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    logger.info("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
