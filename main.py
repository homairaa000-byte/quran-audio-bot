import asyncio
import os
import logging
from threading import Thread

from flask import Flask
from pyrogram import Client, filters
import pyrogram.utils
from pytgcalls import GroupCallFactory


# ==================================================
# إعداد السجلات
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("quran_audio_bot")


# ==================================================
# معالجة معرّفات القنوات والمجموعات الحديثة
# ==================================================
# إصدار Pyrogram المستخدم قد يرفض بعض معرّفات القنوات الحديثة.
# نوسّع الحد المقبول قبل بدء استقبال تحديثات تيليجرام.

pyrogram.utils.MIN_CHANNEL_ID = -1007852516352


# ==================================================
# خادم الويب المطلوب في Render
# ==================================================

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
        use_reloader=False
    )


Thread(
    target=run_web,
    daemon=True
).start()


# ==================================================
# إعدادات تيليجرام
# ==================================================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

STREAM_URL = "http://stream.radiojar.com/8s44324461uvv"


if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "تأكدي من ضبط API_ID وAPI_HASH وSESSION_STRING "
        "في Environment داخل Render."
    )


# ==================================================
# إنشاء عميل تيليجرام
# ==================================================

app = Client(
    "quran_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)


# ==================================================
# إنشاء مشغل الصوت
# ==================================================

call_py = GroupCallFactory(app).get_group_call()


# ==================================================
# تسجيل الرسائل الواردة والصادرة للتشخيص
# ==================================================

@app.on_message(filters.all, group=99)
async def debug_messages(client, message):
    try:
        chat_id = message.chat.id if message.chat else None
        chat_type = message.chat.type if message.chat else None

        message_text = message.text or message.caption or ""

        logger.info(
            "تم رصد رسالة | chat_id=%s | type=%s | "
            "outgoing=%s | text=%s",
            chat_id,
            chat_type,
            message.outgoing,
            message_text[:150]
        )

    except Exception:
        logger.exception(
            "حدث خطأ أثناء تسجيل رسالة للتشخيص"
        )


# ==================================================
# أمر تشغيل بث القرآن الكريم
# ==================================================

@app.on_message(
    filters.command("play", prefixes="/"),
    group=0
)
async def start_stream(client, message):

    chat_id = message.chat.id

    logger.info(
        "تم استلام أمر /play | chat_id=%s | outgoing=%s",
        chat_id,
        message.outgoing
    )

    status_message = None

    try:
        status_message = await message.reply_text(
            "⏳ جارٍ محاولة تشغيل بث القرآن الكريم..."
        )

        await call_py.join_group_call(
            chat_id,
            STREAM_URL
        )

        await status_message.edit_text(
            "🔊 تم إرسال طلب تشغيل بث القرآن الكريم."
        )

        logger.info(
            "تم تنفيذ طلب /play بنجاح | chat_id=%s",
            chat_id
        )

    except Exception as e:

        logger.exception(
            "فشل تنفيذ أمر /play | chat_id=%s",
            chat_id
        )

        error_text = (
            "❌ تعذّر تشغيل البث.\n"
            f"نوع الخطأ: {type(e).__name__}\n"
            f"التفاصيل: {str(e)[:300]}"
        )

        try:
            if status_message:
                await status_message.edit_text(error_text)
            else:
                await message.reply_text(error_text)

        except Exception:
            logger.exception(
                "تعذّر إرسال رسالة الخطأ الخاصة بأمر /play"
            )


# ==================================================
# أمر إيقاف بث القرآن الكريم
# ==================================================

@app.on_message(
    filters.command("stop", prefixes="/"),
    group=0
)
async def stop_stream(client, message):

    chat_id = message.chat.id

    logger.info(
        "تم استلام أمر /stop | chat_id=%s | outgoing=%s",
        chat_id,
        message.outgoing
    )

    try:
        await call_py.leave_group_call(chat_id)

        await message.reply_text(
            "⏹ تم إرسال طلب إيقاف البث."
        )

        logger.info(
            "تم تنفيذ طلب /stop بنجاح | chat_id=%s",
            chat_id
        )

    except Exception as e:

        logger.exception(
            "فشل تنفيذ أمر /stop | chat_id=%s",
            chat_id
        )

        try:
            await message.reply_text(
                "❌ تعذّر إيقاف البث.\n"
                f"نوع الخطأ: {type(e).__name__}\n"
                f"التفاصيل: {str(e)[:300]}"
            )

        except Exception:
            logger.exception(
                "تعذّر إرسال رسالة الخطأ الخاصة بأمر /stop"
            )


# ==================================================
# تشغيل الحساب
# ==================================================

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
            me.username
        )

        logger.info(
            "البوت جاهز لاختبار استقبال الرسائل."
        )

        await asyncio.Event().wait()

    except Exception:

        logger.exception(
            "حدث خطأ أثناء تشغيل الحساب"
        )

        raise

    finally:
        if app.is_connected:
            await app.stop()

            logger.info(
                "تم إيقاف اتصال تيليجرام"
            )


# ==================================================
# نقطة بداية البرنامج
# ==================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info(
            "تم إيقاف البرنامج يدويًا"
        )

    except Exception:
        logger.exception(
            "توقف البرنامج بسبب خطأ"
        )

        raise
