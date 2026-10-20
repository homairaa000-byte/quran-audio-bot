import asyncio
import logging
import os
import threading

from flask import Flask
from pyrogram import Client, filters
from pyrogram import utils as pyrogram_utils
from pytgcalls import GroupCallFactory


# ==================================================
# معالجة حدود معرّفات مجموعات وقنوات تيليجرام
# ==================================================

# توافق مع معرّفات القنوات الحديثة في Pyrogram 2.0.106.
# يجب تنفيذ ذلك قبل استقبال تحديثات تيليجرام.

pyrogram_utils.MIN_CHANNEL_ID = -1007852516352
pyrogram_utils.MIN_CHAT_ID = -999999999999


# ==================================================
# إعداد السجلات
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("quran_audio_bot")


# ==================================================
# خادم Render
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
        use_reloader=False,
    )


threading.Thread(
    target=run_web,
    daemon=True,
    name="render-web-server",
).start()


# ==================================================
# إعدادات تيليجرام
# ==================================================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "تأكدي من وجود API_ID وAPI_HASH وSESSION_STRING "
        "في Environment داخل Render."
    )


# ==================================================
# إنشاء عميل تيليجرام
# ==================================================

app = Client(
    "quran_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)


# ==================================================
# إنشاء مشغل الملفات الصوتية
# ==================================================

group_call_factory = GroupCallFactory(
    app,
    enable_logs_to_console=True,
)

# استخدام مشغل الملفات، وليس مشغلًا عامًا غير مناسب.
call_py = group_call_factory.get_file_group_call(
    play_on_repeat=False
)


# ==================================================
# متغيرات التشغيل
# ==================================================

active_chat_id = None
current_audio_path = None

playback_lock = asyncio.Lock()


# ==================================================
# التحقق من الرسالة الصوتية
# ==================================================

def get_audio_message(message):
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
# تسجيل الرسائل الواردة والصادرة
# ==================================================

@app.on_message(filters.all, group=-1)
async def debug_messages(client, message):
    try:
        chat_id = message.chat.id if message.chat else None

        text_value = (
            message.text
            or message.caption
            or ""
        )

        logger.info(
            "MESSAGE_RECEIVED | chat_id=%s | "
            "message_id=%s | outgoing=%s | text=%r",
            chat_id,
            message.id,
            message.outgoing,
            text_value[:150],
        )

    except Exception:
        logger.exception("MESSAGE_LOGGING_ERROR")


# ==================================================
# أمر /play
# ==================================================

@app.on_message(
    filters.outgoing & filters.command("play", prefixes="/"),
    group=0,
)
async def start_stream(client, message):
    global active_chat_id
    global current_audio_path

    chat_id = message.chat.id

    logger.info(
        "PLAY_COMMAND_RECEIVED | chat_id=%s | message_id=%s",
        chat_id,
        message.id,
    )

    # يجب أن يكون الأمر ردًا على الصوتية المطلوبة.
    audio_message = message.reply_to_message

    if audio_message is None:
        await message.reply_text(
            "⚠️ لتشغيل صوتية الدرس:\n\n"
            "1. اختاري رسالة الصوتية.\n"
            "2. اضغطي على «رد» عليها.\n"
            "3. أرسلي /play."
        )
        return

    if not get_audio_message(audio_message):
        await message.reply_text(
            "❌ الرسالة التي رددتِ عليها ليست صوتية "
            "أو ملفًا صوتيًا مدعومًا.\n\n"
            "ردّي على الصوتية المطلوبة ثم أرسلي /play."
        )
        return

    async with playback_lock:
        status_message = await message.reply_text(
            "⏳ جارٍ تجهيز الصوتية..."
        )

        downloaded_path = None

        try:
            # تحميل الملف الصوتي إلى مساحة مؤقتة.
            downloaded_path = await client.download_media(
                audio_message,
                file_name="/tmp/",
            )

            if not downloaded_path:
                raise RuntimeError(
                    "لم يتمكن تيليجرام من تحميل الملف الصوتي."
                )

            if not os.path.isfile(downloaded_path):
                raise FileNotFoundError(
                    "ملف الصوتية غير موجود بعد التحميل."
                )

            if os.path.getsize(downloaded_path) == 0:
                raise RuntimeError(
                    "حجم ملف الصوتية صفر."
                )

            logger.info(
                "AUDIO_DOWNLOADED | chat_id=%s | "
                "path=%s | size=%s",
                chat_id,
                downloaded_path,
                os.path.getsize(downloaded_path),
            )

            # منع تشغيل الصوتية في مجموعة أخرى في الوقت نفسه.
            if (
                active_chat_id is not None
                and active_chat_id != chat_id
            ):
                raise RuntimeError(
                    "المشغل مستخدم حاليًا في مجموعة أخرى. "
                    "أوقفي التشغيل هناك أولًا."
                )

            # إذا كان المشغل متصلًا بمجموعة أخرى فلا نغيّر الملف.
            # أما إذا كان متصلًا بالمجموعة نفسها، فيمكن تغيير الملف.
            if call_py.is_connected:
                if active_chat_id != chat_id:
                    raise RuntimeError(
                        "المشغل متصل بمحادثة صوتية أخرى. "
                        "أوقفي الاتصال السابق أولًا."
                    )

                # إيقاف تشغيل الملف السابق قبل تغييره.
                call_py.stop_playout()

            # تعيين الملف الذي سيشغّله مشغل الملفات.
            call_py.input_filename = downloaded_path

            logger.info(
                "AUDIO_FILE_SET | chat_id=%s | file=%s",
                chat_id,
                downloaded_path,
            )

            # start(chat_id) يتولى بدء الاتصال بالمحادثة الصوتية.
            # لا نستخدم join() أو start_audio() هنا.
            if not call_py.is_connected:
                logger.info(
                    "JOINING_VOICE_CHAT | chat_id=%s",
                    chat_id,
                )

                await call_py.start(chat_id)

                active_chat_id = chat_id

                logger.info(
                    "VOICE_CHAT_CONNECTED | chat_id=%s",
                    chat_id,
                )

            current_audio_path = downloaded_path

            await status_message.edit_text(
                "🔊 تم إرسال طلب تشغيل الصوتية.\n"
                "إذا كانت المحادثة الصوتية مفتوحة، "
                "فمن المفترض أن يبدأ التشغيل."
            )

            logger.info(
                "AUDIO_START_REQUESTED | chat_id=%s | file=%s",
                chat_id,
                downloaded_path,
            )

        except Exception as error:
            logger.exception(
                "PLAYBACK_FAILED | chat_id=%s",
                chat_id,
            )

            # تنظيف الملف الجديد إذا لم يصبح الملف النشط.
            try:
                if (
                    downloaded_path
                    and downloaded_path != current_audio_path
                    and os.path.isfile(downloaded_path)
                ):
                    os.remove(downloaded_path)

            except OSError:
                logger.exception("AUDIO_CLEANUP_FAILED")

            error_text = (
                "❌ تعذّر تشغيل الصوتية.\n\n"
                f"نوع الخطأ: {type(error).__name__}\n"
                f"التفاصيل: {str(error)[:350]}"
            )

            try:
                await status_message.edit_text(error_text)

            except Exception:
                logger.exception(
                    "PLAYBACK_ERROR_REPLY_FAILED"
                )


# ==================================================
# أمر /stop
# ==================================================

@app.on_message(
    filters.outgoing & filters.command("stop", prefixes="/"),
    group=0,
)
async def stop_stream(client, message):
    global current_audio_path

    chat_id = message.chat.id

    logger.info(
        "STOP_COMMAND_RECEIVED | chat_id=%s",
        chat_id,
    )

    async with playback_lock:
        if active_chat_id != chat_id:
            await message.reply_text(
                "⚠️ لا يوجد تشغيل مسجل لهذه المجموعة."
            )
            return

        try:
            # إيقاف تشغيل الملف فقط.
            # يظل الاتصال بالمحادثة الصوتية قائمًا
            # حتى يمكن تشغيل ملف آخر لاحقًا.
            call_py.stop_playout()

            logger.info(
                "AUDIO_STOPPED | chat_id=%s",
                chat_id,
            )

            await message.reply_text(
                "⏹ تم إرسال طلب إيقاف الصوتية."
            )

            if current_audio_path:
                try:
                    if os.path.isfile(current_audio_path):
                        os.remove(current_audio_path)

                except OSError:
                    logger.exception(
                        "STOP_AUDIO_FILE_DELETE_ERROR"
                    )

                current_audio_path = None

        except Exception as error:
            logger.exception(
                "STOP_FAILED | chat_id=%s",
                chat_id,
            )

            try:
                await message.reply_text(
                    "❌ تعذّر إيقاف الصوتية.\n"
                    f"نوع الخطأ: {type(error).__name__}\n"
                    f"التفاصيل: {str(error)[:300]}"
                )

            except Exception:
                logger.exception(
                    "STOP_ERROR_REPLY_FAILED"
                )


# ==================================================
# تشغيل الحساب
# ==================================================

async def main():
    global active_chat_id
    global current_audio_path

    logger.info(
        "STARTING_TELEGRAM_CLIENT | MIN_CHANNEL_ID=%s | "
        "MIN_CHAT_ID=%s",
        pyrogram_utils.MIN_CHANNEL_ID,
        pyrogram_utils.MIN_CHAT_ID,
    )

    try:
        await app.start()

        me = await app.get_me()

        logger.info(
            "TELEGRAM_CONNECTED | user_id=%s | username=%s",
            me.id,
            me.username,
        )

        logger.info("SYNCING_DIALOGS")

        dialog_count = 0

        async for dialog in app.get_dialogs(limit=200):
            dialog_count += 1

        logger.info(
            "DIALOG_SYNC_COMPLETED | count=%s",
            dialog_count,
        )

        logger.info(
            "BOT_READY | account_id=%s | "
            "instructions=Reply to an audio message with /play",
            me.id,
        )

        await asyncio.Event().wait()

    except Exception:
        logger.exception("TELEGRAM_CLIENT_FATAL_ERROR")
        raise

    finally:
        try:
            if call_py.is_connected:
                await call_py.stop()

        except Exception:
            logger.exception("FINAL_GROUP_CALL_STOP_FAILED")

        if app.is_connected:
            await app.stop()
            logger.info("TELEGRAM_CLIENT_STOPPED")


# ==================================================
# نقطة البداية
# ==================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info("PROGRAM_STOPPED_MANUALLY")

    except Exception:
        logger.exception("PROGRAM_TERMINATED")
        raise
