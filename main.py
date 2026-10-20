import asyncio
import logging
import os
import threading

from flask import Flask
from pyrogram import Client, filters
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
# خادم الويب الخاص بـ Render
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


threading.Thread(
    target=run_web,
    daemon=True
).start()


# ==================================================
# إعدادات تيليجرام
# ==================================================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")


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
# إنشاء مشغل المحادثة الصوتية
# ==================================================

call_py = GroupCallFactory(app).get_group_call()


# ==================================================
# متغيرات التشغيل
# ==================================================

active_chat_id = None
current_audio_path = None

playback_lock = asyncio.Lock()


# ==================================================
# التحقق من أن الرسالة تحتوي على صوتية
# ==================================================

def get_audio_message(message):
    """
    يقبل الرسائل الصوتية وملفات الصوت،
    سواء أكانت Voice أو Audio أو ملفًا صوتيًا.
    """

    if message is None:
        return False

    if message.voice:
        return True

    if message.audio:
        return True

    if message.document:
        mime_type = message.document.mime_type or ""

        if mime_type.startswith("audio/"):
            return True

    return False


# ==================================================
# تسجيل الرسائل الواردة للتشخيص
# ==================================================

@app.on_message(filters.all, group=99)
async def debug_messages(client, message):
    try:
        chat_id = message.chat.id if message.chat else None

        message_text = (
            message.text
            or message.caption
            or ""
        )

        logger.info(
            "رسالة مرصودة | chat_id=%s | "
            "outgoing=%s | text=%s",
            chat_id,
            message.outgoing,
            message_text[:100]
        )

    except Exception:
        logger.exception(
            "حدث خطأ أثناء تسجيل الرسالة"
        )


# ==================================================
# أمر تشغيل الصوتية
# ==================================================

@app.on_message(
    filters.command("play", prefixes="/"),
    group=0
)
async def start_stream(client, message):

    global active_chat_id
    global current_audio_path

    chat_id = message.chat.id

    logger.info(
        "استلام أمر /play | chat_id=%s",
        chat_id
    )

    # يجب أن يكون الأمر ردًا على رسالة صوتية.
    audio_message = message.reply_to_message

    if not audio_message:
        await message.reply_text(
            "⚠️ لتشغيل الدرس:\n\n"
            "1. اختاري رسالة الصوتية المطلوبة.\n"
            "2. اضغطي «رد» على الرسالة.\n"
            "3. أرسلي الأمر /play"
        )
        return

    if not get_audio_message(audio_message):
        await message.reply_text(
            "❌ الرسالة التي رددتِ عليها ليست صوتية "
            "معروفة لديّ.\n\n"
            "ردّي على رسالة صوتية أو ملف صوتي "
            "ثم أرسلي /play."
        )
        return

    async with playback_lock:

        status_message = await message.reply_text(
            "⏳ جارٍ تجهيز صوتية الدرس..."
        )

        downloaded_path = None

        try:
            # تحميل الصوتية من رسالة تيليجرام.
            downloaded_path = await client.download_media(
                audio_message,
                file_name="/tmp/"
            )

            if not downloaded_path:
                raise RuntimeError(
                    "لم أتمكن من تحميل الصوتية من تيليجرام."
                )

            if not os.path.isfile(downloaded_path):
                raise FileNotFoundError(
                    "لم يُعثر على ملف الصوتية بعد تحميله."
                )

            if os.path.getsize(downloaded_path) == 0:
                raise RuntimeError(
                    "ملف الصوتية فارغ."
                )

            logger.info(
                "تم تحميل الصوتية | path=%s | size=%s",
                downloaded_path,
                os.path.getsize(downloaded_path)
            )

            # يمنع محاولة تشغيل الصوتية في محادثة أخرى
            # إذا كان المشغل متصلًا بمحادثة صوتية مختلفة.
            if (
                active_chat_id is not None
                and active_chat_id != chat_id
            ):
                raise RuntimeError(
                    "المشغل متصل بمحادثة أخرى. "
                    "أوقفي التشغيل الحالي أولًا."
                )

            # الانضمام إلى المحادثة الصوتية عند الحاجة.
            if active_chat_id is None:
                await call_py.join(chat_id)
                active_chat_id = chat_id

                logger.info(
                    "تم الانضمام إلى المحادثة الصوتية | chat_id=%s",
                    chat_id
                )

            # إيقاف الصوتية السابقة إن وجدت.
            if current_audio_path is not None:
                try:
                    await call_py.stop_audio()
                except Exception:
                    logger.info(
                        "لا توجد صوتية سابقة قيد التشغيل."
                    )

                try:
                    if os.path.isfile(current_audio_path):
                        os.remove(current_audio_path)
                except OSError:
                    logger.warning(
                        "تعذر حذف ملف الصوتية السابقة."
                    )

                current_audio_path = None

            # بث الصوتية المختارة.
            await call_py.start_audio(downloaded_path)

            current_audio_path = downloaded_path

            await status_message.edit_text(
                "🔊 بدأ تشغيل صوتية الدرس داخل المحادثة الصوتية."
            )

            logger.info(
                "بدأ تشغيل الصوتية بنجاح | chat_id=%s | file=%s",
                chat_id,
                downloaded_path
            )

        except Exception as error:

            logger.exception(
                "فشل تشغيل الصوتية | chat_id=%s",
                chat_id
            )

            error_text = (
                "❌ تعذّر تشغيل الصوتية.\n\n"
                f"نوع الخطأ: {type(error).__name__}\n"
                f"التفاصيل: {str(error)[:500]}\n\n"
                "تأكدي من أن المحادثة الصوتية مفتوحة "
                "وأن الحساب يستطيع الانضمام إليها."
            )

            try:
                await status_message.edit_text(error_text)
            except Exception:
                logger.exception(
                    "تعذر إرسال تفاصيل خطأ التشغيل."
                )

            # إذا فشل بدء التشغيل، نحذف الملف الذي حملناه
            # ما لم يكن هو الملف الجاري استخدامه.
            try:
                if (
                    downloaded_path
                    and downloaded_path != current_audio_path
                    and os.path.isfile(downloaded_path)
                ):
                    os.remove(downloaded_path)
            except OSError:
                logger.warning(
                    "تعذر حذف الملف المؤقت بعد فشل التشغيل."
                )


# ==================================================
# أمر إيقاف الصوتية
# ==================================================

@app.on_message(
    filters.command("stop", prefixes="/"),
    group=0
)
async def stop_stream(client, message):

    global current_audio_path

    chat_id = message.chat.id

    logger.info(
        "استلام أمر /stop | chat_id=%s",
        chat_id
    )

    async with playback_lock:

        try:
            if active_chat_id != chat_id:
                await message.reply_text(
                    "⚠️ لا يوجد تشغيل مسجل لهذه المجموعة."
                )
                return

            await call_py.stop_audio()

            logger.info(
                "تم إيقاف الصوتية | chat_id=%s",
                chat_id
            )

            await message.reply_text(
                "⏹ تم إيقاف صوتية الدرس."
            )

            # حذف الملف بعد طلب إيقاف التشغيل.
            if current_audio_path:
                try:
                    if os.path.isfile(current_audio_path):
                        os.remove(current_audio_path)
                except OSError:
                    logger.warning(
                        "تعذر حذف ملف الصوتية بعد إيقافها."
                    )

                current_audio_path = None

        except Exception as error:

            logger.exception(
                "فشل إيقاف الصوتية | chat_id=%s",
                chat_id
            )

            await message.reply_text(
                "❌ تعذّر إيقاف الصوتية.\n"
                f"نوع الخطأ: {type(error).__name__}\n"
                f"التفاصيل: {str(error)[:300]}"
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

        # مزامنة المحادثات لتقليل مشكلات عدم معرفة
        # معرّفات المجموعات في جلسة Pyrogram.
        logger.info(
            "جارٍ مزامنة المحادثات..."
        )

        dialog_count = 0

        async for dialog in app.get_dialogs(limit=200):
            dialog_count += 1

        logger.info(
            "اكتملت مزامنة المحادثات | count=%s",
            dialog_count
        )

        logger.info(
            "البوت جاهز. ردي على صوتية بالأمر /play."
        )

        await asyncio.Event().wait()

    except Exception:
        logger.exception(
            "حدث خطأ أثناء تشغيل الحساب."
        )
        raise

    finally:
        if app.is_connected:
            await app.stop()

            logger.info(
                "تم إيقاف اتصال تيليجرام."
            )


# ==================================================
# نقطة بداية البرنامج
# ==================================================

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
