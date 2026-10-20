import asyncio
import os
import logging
from threading import Thread

from flask import Flask
from pyrogram import Client, filters
from pytgcalls import GroupCallFactory


# ==========================================
# 1. إعداد السجلات
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("quran_audio_bot")


# ==========================================
# 2. خادم الويب لإبقاء Web Service على Render
# ==========================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Quran Audio Bot is Running!"


@web_app.route("/health")
def health():
    return {"status": "running"}, 200


def run_web():
    port = int(os.environ.get("PORT", "10000"))

    web_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
    )


Thread(
    target=run_web,
    daemon=True,
).start()


# ==========================================
# 3. إعداد حساب تيليجرام
# ==========================================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

STREAM_URL = "http://stream.radiojar.com/8s44324461uvv"


if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "إحدى متغيرات API_ID أو API_HASH أو SESSION_STRING غير مضبوطة."
    )


app = Client(
    "quran_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)


# ==========================================
# 4. إعداد المكالمة الصوتية
# ==========================================

call_py = GroupCallFactory(app).get_group_call()


# ==========================================
# 5. اختبار استقبال الرسائل
# ==========================================

@app.on_message(filters.incoming, group=10)
async def debug_messages(client, message):
    logger.info(
        "وصلت رسالة | chat_id=%s | type=%s",
        message.chat.id if message.chat else None,
        message.chat.type if message.chat else None,
    )


# ==========================================
# 6. أمر تشغيل بث القرآن الكريم
# ==========================================

@app.on_message(
    filters.command("play", prefixes="/"),
    group=0,
)
async def start_stream(client, message):

    chat_id = message.chat.id

    logger.info("استلام أمر /play في المحادثة
