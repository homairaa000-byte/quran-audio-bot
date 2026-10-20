import asyncio
import logging
import os
import threading
import time

from flask import Flask
from pyrogram import Client, filters
from pytgcalls import GroupCallFactory


# ==================================================
# إعداد السجلات
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("quran_lectures_bot")


# ==================================================
# إعداد خادم Render
# ==================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Quran Lectures Userbot Running", 200


@web_app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_web():
    port = int(os.environ.get("PORT", "10000"))

    web_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
        threaded=True,
    )


threading.Thread(
    target=run_web,
    daemon=True,
    name="render-health-server",
).start()


# ==================================================
# إعدادات تيليجرام
# ==================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

DEFAULT_AUDIO = "https://server8.mp3quran.net/afs/001.mp3"


# ==================================================
# إنشاء عميل Pyrogram
# ==================================================

app = Client(
    "quran_lectures_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)


# ==================================================
# إنشاء محرك المكالمات الصوتية
# ==================================================

group_call = GroupCallFactory(app).get_group_call()


# ==================================================
# أمر ping
# ==================================================

@app.on_message(filters.command("ping") & filters.me)
async def ping_handler(client, message):
    logger.info(
        "PING_RECEIVED | chat_id=%s",
        message.chat.id,
    )

    await message.reply_text(
        "🟢 البوت يعمل ويستقبل الأوامر بنجاح!"
    )


# ==================================================
# أمر start
# ==================================================

@app.on_message(filters.command("start") & filters.me)
async def start_handler(client, message):
    await message.reply_text(
        "مرحبًا بك في بوت مقرأة القرآن الكريم.\n\n"
        "الأوامر المتاحة:\n"
        "/ping - اختبار استجابة البوت\n"
        "/play - محاولة بدء بث الصوت\n"
        "/stop - إيقاف البث"
    )


# ==================================================
# أمر play
# ==================================================

@app.on_message(filters.command("play") & filters.me)
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
            "⏳ وصل أمر التشغيل، جارٍ التحقق من إمكانية بدء البث..."
        )

        # الانضمام إلى المكالمة الصوتية في المجموعة
        await group_call.join(chat_id)

        # تشغيل المصدر الصوتي باستخدام واجهة الإصدار المحدد
        await group_call.start(audio_source)

        logger.info(
            "PLAY_STARTED | chat_id=%s",
            chat_id,
        )

        await message.reply_text(
            "🎙️ تمت معالجة أمر التشغيل. "
            "تحققي من انضمام الحساب إلى المكالمة وسماع الصوت."
        )

    except Exception as exc:
        logger.exception(
            "PLAY_ERROR | chat_id=%s",
            chat_id,
        )

        await message.reply_text(
            "❌ تعذر تشغيل المحاضرة.\n"
            f"نوع الخطأ: {type(exc).__name__}\n"
            f"التفاصيل: {str(exc)[:700]}"
        )


# ==================================================
# أمر stop
# ==================================================

@app.on_message(filters.command("stop") & filters.me)
async def stop_handler(client, message):
    chat_id = message.chat.id

    logger.info(
        "STOP_COMMAND_RECEIVED | chat_id=%s",
        chat_id,
    )

    try:
        await group_call.stop()

        await message.reply_text(
            "⏹️ تمت محاولة إيقاف بث الصوت."
        )

    except Exception as exc:
        logger.exception(
            "STOP_ERROR | chat_id=%s",
            chat_id,
        )

        await message.reply_text(
            "❌ تعذر إيقاف البث.\n"
            f"نوع الخطأ: {type(exc).__name__}\n"
            f"التفاصيل: {str(exc)[:500]}"
        )


# ==================================================
# تسجيل الأخطاء غير المتوقعة في استقبال الرسائل
# ==================================================

@app.on_message(filters.command("status") & filters.me)
async def status_handler(client, message):
    me = await client.get_me()

    await message.reply_text(
        "🟢 حالة الاتصال:\n"
        f"معرّف الحساب: {me.id}\n"
        f"اسم المستخدم: @{me.username}"
        if me.username
        else
        "🟢 الحساب متصل بتيليجرام."
    )


# ==================================================
# تشغيل البوت
# ==================================================

async def main():
    logger.info("STARTING_BOT")

    try:
        await app.start()

        me = await app.get_me()

        logger.info(
            "CONNECTED | id=%s | username=%s",
            me.id,
            me.username,
        )

        logger.info(
            "BOT_READY | waiting_for_commands"
        )

        await asyncio.Event().wait()

    except Exception:
        logger.exception("FATAL_ERROR")
        raise

    finally:
        try:
            await app.stop()
        except Exception:
            logger.exception("APP_STOP_ERROR")


if __name__ == "__main__":
    asyncio.run(main())
