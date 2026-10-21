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

app = Client(
    "quran_lectures_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

group_call = GroupCallFactory(app).get_group_call()


# =========================================================
# 5. الأوامر للتعامل مع الصوتيات داخل الشات
# =========================================================

@app.on_message(filters.command("ping") & (filters.me | filters.incoming))
async def ping_handler(client, message):
    await message.reply_text("🟢 بوت مقرأة القرآن الكريم يعمل بنجاح!")


@app.on_message(filters.command("play") & (filters.me | filters.incoming))
async def play_handler(client, message):
    try:
        chat_id = message.chat.id
        target_message = message.reply_to_message

        # التحقق مما إذا كانت الرسالة رد على ملف صوتي أو رسالة صوتية
        if target_message and (target_message.audio or target_message.voice or target_message.document):
            audio_file = target_message.audio or target_message.voice or target_message.document
            
            # تحميل الملف الصوتي مؤقتاً لتشغيله
            await message.reply_text("📥 جاري تحميل الملف الصوتي وبثه في المكالمة...")
            file_path = await client.download_media(audio_file)

            logger.info("PLAY_LOCAL_AUDIO | chat_id=%s | file=%s", chat_id, file_path)

            await group_call.join(chat_id)
            group_call.input_filename = file_path

            await message.reply_text("🎙️ **تم بدء بث الصوتية في المكالمة المباشرة بنجاح!**")
        else:
            await message.reply_text("⚠️ الرجاء **الرد (Reply)** على أي رسالة صوتية أو ملف صوتي داخل المجموعة بالأمر `/play` لبثه.")

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
    app.run()
