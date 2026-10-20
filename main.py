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
# 2. خادم الويب المجاني على Render
# ==========================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Quran Audio Bot is Running!", 200


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


web_thread = Thread(
    target=run_web,
    daemon=True,
)

web_thread.start()


# ==========================================
# 3. إعداد حساب تيليجرام
# ==========================================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

STREAM_URL = "http://stream.radiojar.com/8s44324461uvv"


if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "خطأ: تأكدي من ضبط API_ID وAPI_HASH وSESSION_STRING في Render."
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
# 5. تسجيل الرسائل الواردة للتشخيص
# ==========================================

@app.on_message(
    filters.incoming,
    group=10,
)
async def debug_messages(client, message):

    chat_id = message.chat.id if message.chat else None

    logger.info(
        "وصلت رسالة | chat_id=%s | type=%s",
        chat_id,
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

    logger.info(
        "استلام أمر /play في المحادثة %s",
        chat_id,
    )

    status_message = None

    try:
        status_message = await message.reply_text(
            "⏳ جارٍ محاولة تشغيل بث القرآن الكريم..."
        )

        await call_py.join_group_call(
            chat_id,
            STREAM_URL,
        )

        await status_message.edit_text(
            "🔊 تم إرسال طلب تشغيل بث القرآن الكريم."
        )

        logger.info(
            "اكتمل طلب الانضمام إلى المكالمة في المحادثة %s",
            chat_id,
        )

    except Exception as e:

        logger.exception(
            "حدث خطأ أثناء تنفيذ أمر /play"
        )

        error_text = (
            "❌ تعذّر تشغيل البث.\n"
            f"نوع الخطأ: {type(e).__name__}\n"
            f"التفاصيل: {str(e)[:500]}"
        )

        try:
            if status_message:
                await status_message.edit_text(error_text)
            else:
                await message.reply_text(error_text)

        except Exception:
            logger.exception(
                "تعذّر إرسال رسالة الخطأ إلى تيليجرام"
            )


# ==========================================
# 7. أمر إيقاف بث القرآن الكريم
# ==========================================

@app.on_message(
    filters.command("stop", prefixes="/"),
    group=0,
)
async def stop_stream(client, message):

    chat_id = message.chat.id

    logger.info(
        "استلام أمر /stop في المحادثة %s",
        chat_id,
    )

    try:
        await call_py.leave_group_call(chat_id)

        await message.reply_text(
            "⏹ تم إرسال طلب إيقاف البث."
        )

        logger.info(
            "اكتمل طلب إيقاف البث في المحادثة %s",
            chat_id,
        )

    except Exception as e:

        logger.exception(
            "حدث خطأ أثناء تنفيذ أمر /stop"
        )

        try:
            await message.reply_text(
                "❌ تعذّر إيقاف البث.\n"
                f"نوع الخطأ: {type(e).__name__}\n"
                f"التفاصيل: {str(e)[:500]}"
            )

        except Exception:
            logger.exception(
                "تعذّر إرسال رسالة الخطأ إلى تيليجرام"
            )


# ==========================================
# 8. تشغيل حساب تيليجرام
# ==========================================

async def main():

    logger.info(
        "بدء تشغيل حساب تيليجرام..."
    )

    try:
        await app.start()

        me = await app.get_me()

        logger.info(
            "تم الاتصال بنجاح | user_id=%s | username=%s",
            me.id,
            me.username,
        )

        logger.info(
            "البوت جاهز لاختبار استقبال الرسائل."
        )

        await asyncio.Event().wait()

    except Exception:
        logger.exception(
            "حدث خطأ أثناء تشغيل حساب تيليجرام."
        )
        raise

    finally:
        if app.is_connected:
            await app.stop()

            logger.info(
                "تم إيقاف اتصال تيليجرام."
            )


# ==========================================
# 9. نقطة بدء البرنامج
# ==========================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info(
            "تم إيقاف البرنامج يدويًا."
        )

    except Exception:
        logger.exception(
            "توقف البرنامج بسبب خطأ."
        )
        raise
