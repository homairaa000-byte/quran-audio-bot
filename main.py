import asyncio
import logging
import os
import threading

from flask import Flask
from werkzeug.serving import make_server

from pyrogram import Client, filters
from pyrogram import utils as pyrogram_utils
from pytgcalls import GroupCallFactory
from pytgcalls.types.input_stream import AudioPiped


# =========================================================
# 1. إعداد السجلات
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("quran_lectures_bot")


# =========================================================
# 2. إيصال إصلاح معالجة معرّفات القنوات والمجموعات في Pyrogram
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


logger.info(
    "PYROGRAM_ID_FIX_APPLIED | "
    "MIN_CHANNEL_ID=%s | "
    "MIN_CHAT_ID=%s | "
    "MAX_CHANNEL_ID=%s",
    pyrogram_utils.MIN_CHANNEL_ID,
    pyrogram_utils.MIN_CHAT_ID,
    pyrogram_utils.MAX_CHANNEL_ID,
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
            threaded=True
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
# 6. إنشاء محرك المكالمات الصوتية بالطريقة الصحيحة
# =========================================================

group_call = GroupCallFactory(app).get_group_call()


# =========================================================
# 7. الأوامر (ping, start, status, play, stop)
# =========================================================

@app.on_message(
    filters.command("ping") & (filters.me | filters.incoming)
)
async def ping_handler(client, message):
    logger.info(
        "PING_RECEIVED | chat_id=%s",
        message.chat.id,
    )
    await message.reply_text(
        "🟢 بوت مقرأة القرآن الكريم يعمل بنجاح!"
    )


@app.on_message(
    filters.command("start") & (filters.me | filters.incoming)
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


@app.on_message(
    filters.command("status") & (filters.me | filters.incoming)
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
    except Exception:
        logger.exception("STATUS_ERROR")
        await message.reply_text(
            "❌ تعذر فحص الحالة بسبب خطأ داخلي."
        )


@app.on_message(
    filters.command("play") & (filters.me | filters.incoming)
)
async def play_handler(client, message):
    try:
        chat_id = message.chat.id
        args = message.text.split(maxsplit=1)
        audio_source = args[1] if len(args) > 1 else DEFAULT_AUDIO

        logger.info(
            "PLAY_RECEIVED | chat_id=%s | source=%s",
            chat_id,
            audio_source,
        )

        # استخدام الطريقة الصحيحة المتوافقة مع GroupCallFactory للبث والصوت
        await group_call.join(chat_id)
        await group_call.input_stream(AudioPiped(audio_source))

        await message.reply_text(
            "🎙️ **تم بدء بث المحاضرة في المكالمة المباشرة بنجاح!**"
        )

    except Exception as exc:
        logger.exception("PLAY_ERROR")
        await message.reply_text(f"❌ تعذر بدء البث: {exc}")


@app.on_message(
    filters.command("stop") & (filters.me | filters.incoming)
)
async def stop_handler(client, message):
    try:
        chat_id = message.chat.id

        logger.info(
            "STOP_RECEIVED | chat_id=%s",
            chat_id,
        )

        await group_call.leave(chat_id)

        await message.reply_text(
            "⏹️ **تم إيقاف البث والخروج من المكالمة المباشرة.**"
        )

    except Exception as exc:
        logger.exception("STOP_ERROR")
        await message.reply_text(f"❌ تعذر إيقاف البث: {exc}")


# =========================================================
# 8. التشغيل الأساسي
# =========================================================

if __name__ == "__main__":
    app.run()
