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
# 2. إصلاح حدود معرّفات القنوات في Pyrogram 2.0.106
# =========================================================
#
# هذا الإصدار القديم قد يرفض بعض معرّفات القنوات الحديثة.
# نوسّع الحد الأدنى قبل بدء العميل.
#
# لا نستبدل resolve_peer ولا نخفي أخطاء Pyrogram.
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
            "❌ تعذر فحص الحالة.\n"
            f"{type(exc).__name__}: {str(exc)[:400]}"
        )


# =========================================================
# 10. أمر play
# =========================================================

@app.on_message(
    filters.command("play") & filters.me
)
async def play_handler(client, message):
    chat_id = message.chat.id

    args = message.text.split(maxsplit=1)

    audio_source = (
        args[1].strip()
        if len(args) > 1
        else DEFAULT_AUDIO
    )

    logger.info(
        "PLAY_COMMAND_RECEIVED | chat_id=%s | source=%s",
        chat_id,
        audio_source,
    )

    try:
        await message.reply_text(
            "⏳ جارٍ الاتصال بالمكالمة الصوتية "
            "وتشغيل المحاضرة..."
        )

        # الانضمام إلى المكالمة الصوتية
        await group_call.join(chat_id)

        # بدء بث الصوت
        await group_call.start_audio(audio_source)

        logger.info(
            "PLAY_STARTED | chat_id=%s",
            chat_id,
        )

        await message.reply_text(
            "🎙️ تم إرسال طلب تشغيل المحاضرة.\n"
            "تحققي من انضمام الحساب إلى المكالمة "
            "وسماع الصوت."
        )

    except Exception as exc:
        logger.exception(
            "PLAY_ERROR | chat_id=%s",
            chat_id,
        )

        await message.reply_text(
            "❌ تعذر تشغيل المحاضرة.\n"
            f"الخطأ: {type(exc).__name__}\n"
            f"التفاصيل: {str(exc)[:600]}"
        )


# =========================================================
# 11. أمر stop
# =========================================================

@app.on_message(
    filters.command("stop") & filters.me
)
async def stop_handler(client, message):
    chat_id = message.chat.id

    logger.info(
        "STOP_COMMAND_RECEIVED | chat_id=%s",
        chat_id,
    )

    try:
        # إيقاف مصدر الصوت
        await group_call.stop_audio()

        # مغادرة المكالمة
        await group_call.leave()

        logger.info(
            "PLAY_STOPPED | chat_id=%s",
            chat_id,
        )

        await message.reply_text(
            "⏹️ تم إيقاف بث الصوت ومغادرة المكالمة."
        )

    except Exception as exc:
        logger.exception(
            "STOP_ERROR | chat_id=%s",
            chat_id,
        )

        await message.reply_text(
            "❌ تعذر إيقاف البث.\n"
            f"الخطأ: {type(exc).__name__}\n"
            f"التفاصيل: {str(exc)[:500]}"
        )


# =========================================================
# 12. تحميل معلومات المحادثات
# =========================================================

async def warm_up_dialogs():
    """
    تحميل معلومات المحادثات إلى مخزن Pyrogram.
    يساعد ذلك على توفير بيانات المحادثات اللازمة
    لحل معرّفات المجموعات والقنوات.
    """

    logger.info("DIALOGS_LOADING_STARTED")

    count = 0

    try:
        async for dialog in app.get_dialogs():
            count += 1

        logger.info(
            "DIALOGS_LOADING_FINISHED | count=%s",
            count,
        )

    except Exception:
        logger.exception("DIALOGS_LOADING_ERROR")


# =========================================================
# 13. مراقبة عمل البوت
# =========================================================

async def heartbeat():
    while True:
        logger.info("BOT_ALIVE")
        await asyncio.sleep(60)


# =========================================================
# 14. التشغيل الرئيسي
# =========================================================

async def main():
    logger.info("BOT_STARTING")

    try:
        await app.start()

        me = await app.get_me()

        logger.info(
            "CONNECTED | id=%s | username=%s",
            me.id,
            me.username,
        )

        # تحميل معلومات المحادثات
        await warm_up_dialogs()

        logger.info("BOT_READY | waiting_for_commands")

        asyncio.create_task(
            heartbeat(),
            name="bot-heartbeat",
        )

        await asyncio.Event().wait()

    except Exception:
        logger.exception("FATAL_ERROR")
        raise

    finally:
        try:
            if app.is_connected:
                await app.stop()

        except Exception:
            logger.exception("APP_STOP_ERROR")


# =========================================================
# 15. نقطة الدخول
# =========================================================

if __name__ == "__main
