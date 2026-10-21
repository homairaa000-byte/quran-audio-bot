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
# 2. إصلاح معرّفات تيليجرام
# =========================================================

_original_get_peer_type = pyrogram_utils.get_peer_type


def fixed_get_peer_type(peer_id):
    if (
        isinstance(peer_id, int)
        and peer_id < pyrogram_utils.MAX_CHANNEL_ID
        and peer_id >= -1007852516352
    ):
        return "channel"

    return _original_get_peer_type(peer_id)


pyrogram_utils.MIN_CHANNEL_ID = -1007852516352
pyrogram_utils.MIN_CHAT_ID = -999999999999
pyrogram_utils.get_peer_type = fixed_get_peer_type


# =========================================================
# 3. خادم الويب الفوري لـ Render
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Quran Lectures Userbot Running", 200


@web_app.route("/health")
def health():
    return {"status": "ok", "service": "quran_lectures_bot"}, 200


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    server = make_server("0.0.0.0", port, web_app, threaded=True)
    logger.info("WEB_SERVER_STARTED | port=%s", port)
    server.serve_forever()


web_thread = threading.Thread(target=run_web, daemon=True)
web_thread.start()


# =========================================================
# 4. إعدادات البوت والمكالمات
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

DEFAULT_AUDIO = "https://server8.mp3quran.net/afs/001.mp3"

app = Client(
    "quran_lectures_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

group_call = GroupCallFactory(app).get_group_call()


# =========================================================
# 5. الأوامر
# =========================================================

@app.on_message(filters.command("ping") & (filters.me | filters.incoming))
async def ping_handler(client, message):
    await message.reply_text("🟢 بوت مقرأة القرآن الكريم يعمل بنجاح!")


@app.on_message(filters.command("start") & (filters.me | filters.incoming))
async def start_handler(client, message):
    await message.reply_text(
        "مرحبًا بك في بوت مقرأة القرآن الكريم.\n\n"
        "الأوامر المتاحة:\n"
        "/ping - اختبار الاستجابة\n"
        "/play - بدء البث المباشر\n"
        "/stop - إيقاف البث"
    )


@app.on_message(filters.command("play") & (filters.me | filters.incoming))
async def play_handler(client, message):
    try:
        chat_id = message.chat.id
        args = message.text.split(maxsplit=1)
        audio_source = args[1] if len(args) > 1 else DEFAULT_AUDIO

        logger.info("PLAY_RECEIVED | chat_id=%s | source=%s", chat_id, audio_source)

        await group_call.join(chat_id)
        group_call.input_filename = audio_source

        await message.reply_text("🎙️ **تم بدء بث المحاضرة في المكالمة المباشرة بنجاح!**")

    except Exception as exc:
        logger.exception("PLAY_ERROR")
        await message.reply_text(f"❌ تعذر بدء البث: {exc}")


@app.on_message(filters.command("stop") & (filters.me | filters.incoming))
async def stop_handler(client, message):
    try:
        chat_id = message.chat.id
        logger.info("STOP_RECEIVED | chat_id=%s", chat_id)

        await group_call.leave_current_group_call()
        await message.reply_text("⏹️ **تم إيقاف البث والخروج من المكالمة المباشرة.**")

    except Exception as exc:
        logger.exception("STOP_ERROR")
        await message.reply_text(f"❌ تعذر إيقاف البث: {exc}")


if __name__ == "__main__":
    app.start()
    import asyncio
    asyncio.get_event_loop().run_forever()
