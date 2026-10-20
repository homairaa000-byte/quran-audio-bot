import asyncio
import logging
import os
import threading

from flask import Flask
from werkzeug.serving import make_server

from pyrogram import Client, filters
from pyrogram import utils as pyrogram_utils
from pytgcalls import GroupCallFactory


# =========================================================
# 1. إعداد السجلات
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("quran_lectures_bot")


# =========================================================
# 2. إعداد حدود معرّفات القنوات
# =========================================================

pyrogram_utils.MIN_CHANNEL_ID = -1007852516352

logger.info(
    "PYROGRAM_CHANNEL_ID_LIMIT_UPDATED | MIN_CHANNEL_ID=%s",
    pyrogram_utils.MIN_CHANNEL_ID,
)


# =========================================================
# 3. خادم الويب الخاص بـ Render
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Quran Lectures Userbot Running", 200


@web_app.route("/health")
def health():
    return {
        "status": "ok",
        "service": "quran_lectures_bot",
    }, 200


def run_web():
    port = int(os.environ.get("PORT", "10000"))

    try:
        server = make_server(
            "0.0.0.0",
            port,
            web_app,
        )

        logger.info(
            "WEB_SERVER_STARTED | host=0.0.0.0 | port=%s",
            port,
        )

        server.serve_forever()

    except Exception:
        logger.exception("WEB_SERVER_ERROR")
        raise


web_thread = threading.Thread(
    target=run_web,
    name="render-web-server",
    daemon=True,
)

web_thread.start()


# =========================================================
# 4. إعدادات تيليجرام
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

DEFAULT_AUDIO = (
    "https://server8.mp3quran.net/afs/001.mp3"
)


# =========================================================
# 5. إنشاء عميل Pyrogram
# =========================================================

app = Client(
    "quran_lectures_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)


# =========================================================
# 6. إنشاء محرك المكالمات الصوتية
# =========================================================

group_call = GroupCallFactory(app).get_group_call()


# =========================================================
# 7. أمر ping
# =========================================================

@app.on_message(
    filters.command("ping") & filters.me
)
async def ping_handler(client, message):
    logger.info(
        "PING_RECEIVED | chat_id=%s",
        message.chat.id,
    )

    await message.reply_text(
        "🟢 بوت مقرأة القرآن الكريم يعمل بنجاح!"
    )


# =========================================================
# 8. أمر start
# =========================================================

@app.on_message(
    filters.command("start") & filters.me
)
async def start_handler(client, message):
    await message.reply_text(
        "مرحبًا بك في بوت مقرأة القرآن الكريم.\n\n"
        "الأوامر المتاحة:\n"
        "/ping - اختبار استجابة البوت\n"
        "/play - بدء بث المحاضرة\n"
        "/stop - إيقاف البث\n"
        "/status - فحص حالة الاتصال"
    )


# =========================================================
# 9. أمر status
# =========================================================

@app.on_message(
    filters.command("status") & filters.me
)
async def status_handler(client, message):
    try:
        me = await client.get_me()

        username = (
            f"@{me.username}"
            if me.username
            else "غير محدد"
        )

        await message.reply_text(
            "🟢 الحساب متصل بتيليجرام.\n"
            f"معرّف الحساب: {me.id}\n"
            f"اسم المستخدم: {username}"
        )

        logger.info(
            "STATUS_OK | user_id=%s",
            me.id,
        )

    except Exception as exc:
        logger.exception("STATUS_ERROR")

        await message.reply_text(
            "❌ تعذر ف
