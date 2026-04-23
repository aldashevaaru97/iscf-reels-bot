#!/usr/bin/env python3
"""
Social Media Publishing Bot
Telegram-бот для публикации видео на YouTube Shorts и Facebook Reels
"""

import os
import logging
import tempfile
import asyncio
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# Google / YouTube
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow

# Facebook / Meta Graph API
import requests

# ─── Логирование ────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Конфигурация (заполни своими данными) ──────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВАШ_TELEGRAM_TOKEN")

# YouTube
YOUTUBE_CLIENT_SECRETS_FILE = "client_secrets.json"  # скачай из Google Console
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Facebook
FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "ВАШ_FACEBOOK_TOKEN")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "ВАШ_PAGE_ID")

# Telegram user IDs у кого есть доступ к боту (через запятую)
ALLOWED_USER_IDS = [int(x) for x in os.getenv("ALLOWED_USER_IDS", "0").split(",") if x]

# Твой личный Telegram ID — тебе будут приходить уведомления о публикациях коллег
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# Имена пользователей (ID → Имя) — заполни своими данными
# Пример в .env: USER_NAMES=123456789:Алия,987654321:Коллега
USER_NAMES = {
    int(k): v
    for k, v in (
        pair.split(":", 1)
        for pair in os.getenv("USER_NAMES", "").split(",")
        if ":" in pair
    )
}

# ─── Состояния диалога ──────────────────────────────────────────────────────
WAITING_CAPTION = 1

# Временное хранилище данных пользователя
user_data_store: dict = {}


# ─── Авторизация YouTube ─────────────────────────────────────────────────────
def get_youtube_service():
    """Получить авторизованный YouTube сервис."""
    creds = None
    token_file = "youtube_token.json"

    if Path(token_file).exists():
        creds = google.oauth2.credentials.Credentials.from_authorized_user_file(
            token_file, YOUTUBE_SCOPES
        )

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            YOUTUBE_CLIENT_SECRETS_FILE, YOUTUBE_SCOPES
        )
        creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


# ─── Загрузка на YouTube Shorts ──────────────────────────────────────────────
def upload_to_youtube(video_path: str, title: str, description: str) -> str:
    """Загрузить видео на YouTube как Short. Возвращает URL."""
    youtube = get_youtube_service()

    body = {
        "snippet": {
            "title": title[:100],  # YouTube лимит — 100 символов
            "description": description + "\n\n#Shorts",
            "tags": ["Shorts"],
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, mimetype="video/*", resumable=True)

    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"YouTube upload: {int(status.progress() * 100)}%")

    video_id = response["id"]
    return f"https://youtube.com/shorts/{video_id}"


# ─── Загрузка на Facebook Reels ──────────────────────────────────────────────
def upload_to_facebook(video_path: str, description: str) -> str:
    """Загрузить видео на Facebook как Reel. Возвращает URL."""
    base_url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}"

    # Шаг 1: Инициализация загрузки
    init_response = requests.post(
        f"{base_url}/video_reels",
        data={
            "upload_phase": "start",
            "access_token": FACEBOOK_ACCESS_TOKEN,
        },
    )
    init_data = init_response.json()

    if "error" in init_data:
        raise Exception(f"Facebook init error: {init_data['error']['message']}")

    video_id = init_data["video_id"]
    upload_url = init_data["upload_url"]

    # Шаг 2: Загрузка файла
    with open(video_path, "rb") as video_file:
        file_size = os.path.getsize(video_path)
        upload_response = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {FACEBOOK_ACCESS_TOKEN}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=video_file,
        )

    if upload_response.status_code != 200:
        raise Exception(f"Facebook upload error: {upload_response.text}")

    # Шаг 3: Публикация
    publish_response = requests.post(
        f"{base_url}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "access_token": FACEBOOK_ACCESS_TOKEN,
            "video_state": "PUBLISHED",
            "description": description,
        },
    )
    publish_data = publish_response.json()

    if "error" in publish_data:
        raise Exception(f"Facebook publish error: {publish_data['error']['message']}")

    return f"https://facebook.com/reel/{video_id}"


# ─── Обработчики Telegram ─────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    await update.message.reply_text(
        "👋 Привет! Я твой помощник по публикации видео.\n\n"
        "📤 Отправь мне видео и я опубликую его на:\n"
        "  • YouTube Shorts 🎬\n"
        "  • Facebook Reels 📘\n\n"
        "Просто отправь видео файлом или как видео-сообщение!"
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    # Получаем файл (видео или документ)
    if update.message.video:
        file = update.message.video
    elif update.message.document and update.message.document.mime_type.startswith("video"):
        file = update.message.document
    else:
        await update.message.reply_text("❌ Пожалуйста, отправь видео файл.")
        return

    await update.message.reply_text("⏳ Скачиваю видео...")

    # Скачиваем файл
    tg_file = await context.bot.get_file(file.file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    await tg_file.download_to_drive(tmp.name)
    tmp.close()

    # Сохраняем путь
    user_data_store[user_id] = {"video_path": tmp.name}

    # Спрашиваем подпись
    keyboard = [[InlineKeyboardButton("Пропустить — опубликовать без подписи", callback_data="no_caption")]]
    await update.message.reply_text(
        "✏️ Напиши подпись/описание для видео\n"
        "(или нажми кнопку чтобы пропустить):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_CAPTION


async def handle_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    caption = update.message.text
    user_data_store[user_id]["caption"] = caption
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

    msg_func = query.edit_message_text if query else update.message.reply_text
    await msg_func("🚀 Публикую видео на платформы...\n\n⏳ YouTube Shorts...\n⏳ Facebook Reels...")

    results = {}
    errors = {}

    # YouTube
    try:
        title = caption[:80] if caption else "Новое видео"
        yt_url = await asyncio.get_event_loop().run_in_executor(
            None, upload_to_youtube, video_path, title, caption
        )
        results["YouTube Shorts"] = yt_url
    except Exception as e:
        errors["YouTube Shorts"] = str(e)
        logger.error(f"YouTube error: {e}")

    # Facebook
    try:
        fb_url = await asyncio.get_event_loop().run_in_executor(
            None, upload_to_facebook, video_path, caption
        )
        results["Facebook Reels"] = fb_url
    except Exception as e:
        errors["Facebook Reels"] = str(e)
        logger.error(f"Facebook error: {e}")

    # Формируем ответ
    lines = ["✅ *Публикация завершена!*\n"]

    for platform, url in results.items():
        lines.append(f"✅ *{platform}*: [Открыть]({url})")

    for platform, err in errors.items():
        lines.append(f"❌ *{platform}*: Ошибка — `{err}`")

    lines.append("\n📸 *Instagram* — загрузи вручную как обычно")

    # Удаляем временный файл
    try:
        os.unlink(video_path)
    except Exception:
        pass

    user_data_store.pop(user_id, None)

    send_func = query.edit_message_text if query else update.message.reply_text
    await send_func("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = user_data_store.pop(user_id, {})
    if data.get("video_path"):
        try:
            os.unlink(data["video_path"])
        except Exception:
            pass
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ─── Запуск бота ─────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
        ],
        states={
            WAITING_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_caption),
                CallbackQueryHandler(handle_no_caption, pattern="^no_caption$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    logger.info("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
