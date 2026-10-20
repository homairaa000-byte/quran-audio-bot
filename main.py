import asyncio
import logging
import os
import threading
import time
import uuid

from flask import Flask
from pyrogram import Client, filters
from pyrogram import utils as pyrogram_utils
from pytgcalls import GroupCallFactory


# ==================================================
# إعداد السجلات
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("quran_audio_bot")


# ==================================================
# إعداد معرّفات المحادثات في Pyrogram
# ==================================================

pyrogram_utils.MIN_CHANNEL_ID = -1007852516352
pyrogram_utils.MIN_CHAT_ID = -999999999999


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
        "تأكدي من ضبط API_ID وAPI_HASH وSESSION_STRING "
        "داخل Environment في Render."
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

call_py = GroupCallFactory(
    app,
    enable_logs_to_console=True,
).get_file_group_call(
    play_on_repeat=False,
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
# حذف الملفات المؤقتة بأمان
# ==================================================

def delete_file(path):
    if not path:
        return

    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("TEMP_FILE_DELETED | path=%s", path)

    except OSError:
        logger.exception("TEMP_FILE_DELETE_FAILED | path=%s", path)


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

    audio_message = message.reply_to_message

    if audio_message is None:
        await message.reply_text(
            "⚠️ لتشغيل صوتية الدرس:\n\n"
            "1. اختاري الصوتية المطلوبة.\n"
            "2. اضغطي على «رد» على الصوتية.\n"
            "3. أرسلي /play."
        )
        return

    if not get_audio_message(audio_message):
        await message.reply_text(
            "❌ الرسالة التي رددتِ عليها ليست ملفًا صوتيًا مدعومًا.\n\n"
            "ردّي على الصوتية المطلوبة ثم أرسلي /play."
        )
        return

    async with playback_lock:
        status_message = await message.reply_text(
            "⏳ جارٍ تحميل الصوتية وتجهيزها..."
        )

        downloaded_path = None

        try:
            # ------------------------------------------
            # تحميل الصوتية المطلوبة
            # ------------------------------------------

            downloaded_path = await client.download_media(
                audio_message,
                file_name=f"/tmp/quran_audio_{uuid.uuid4().hex}_",
            )

            if not downloaded_path:
                raise RuntimeError(
                    "لم يتمكن تيليجرام من تحميل الملف الصوتي."
                )

            if not os.path.isfile(downloaded_path):
                raise FileNotFoundError(
                    f"الملف غير موجود بعد التحميل: {downloaded_path}"
                )

            file_size = os.path.getsize(downloaded_path)

            if file_size <= 0:
                raise RuntimeError("الملف الصوتي فارغ.")

            logger.info(
                "AUDIO_DOWNLOADED | chat_id=%s | "
                "path=%s | size=%s",
                chat_id,
                downloaded_path,
                file_size,
            )

            # ------------------------------------------
            # لا نشغل المشغل في مجموعتين معًا
            # ------------------------------------------

            if (
                active_chat_id is not None
                and active_chat_id != chat_id
            ):
                raise RuntimeError(
                    "المشغل يعمل في مجموعة أخرى. "
                    "أوقفي التشغيل هناك أولًا."
                )

            # ------------------------------------------
            # إذا كان المشغل متصلًا بالمجموعة نفسها،
            # نوقف الملف السابق قبل استبداله.
            # ------------------------------------------

            if call_py.is_connected:
                call_py.stop_playout()

                # نمنح المشغل وقتًا قصيرًا لتحرير الملف السابق.
                await asyncio.sleep(0.3)

            # ------------------------------------------
            # تحديد الملف الذي سيشغله المشغل
            # ------------------------------------------

            call_py.input_filename = downloaded_path

            # ------------------------------------------
            # الانضمام إلى المحادثة الصوتية
            # عند عدم وجود اتصال سابق
            # ------------------------------------------

            if not call_py.is_connected:
                logger.info(
                    "JOINING_VOICE_CHAT | chat_id=%s",
                    chat_id,
                )

                await call_py.start(chat_id)

                logger.info(
                    "VOICE_CHAT_START_REQUESTED | chat_id=%s",
                    chat_id,
                )

            active_chat_id = chat_id

            # ------------------------------------------
            # تحديث الملف النشط وحذف الملف السابق
            # ------------------------------------------

            old_path = current_audio_path
            current_audio_path = downloaded_path

            downloaded_path = None

            if old_path and old_path != current_audio_path:
                delete_file(old_path)

            await status_message.edit_text(
                "🔊 تم تجهيز الصوتية وإرسالها إلى المشغل.\n\n"
                "إذا لم تسمعي الصوت، تأكدي من أن "
                "المحادثة الصوتية مفتوحة وأن الحساب "
                "مسموح له بالانضمام."
            )

            logger.info(
                "AUDIO_PLAYBACK_CONFIGURED | chat_id=%s | file=%s",
                chat_id,
                current_audio_path,
            )

        except Exception as error:
            logger.exception(
                "PLAYBACK_FAILED | chat_id=%s",
                chat_id,
            )

            error_text = (
                "❌ تعذّر تشغيل الصوتية.\n\n"
                f"نوع الخطأ: {type(error).__name__}\n"
                f"التفاصيل: {str(error)[:400]}\n\n"
                "تأكدي من فتح المحادثة الصوتية "
                "وإمكانية انضمام الحساب إليها."
            )

            try:
                await status_message.edit_text(error_text)

            except Exception:
                logger.exception("PLAYBACK_ERROR_REPLY_FAILED")

            # حذف الملف الجديد إذا لم يصبح الملف النشط.
            if (
                downloaded_path
                and downloaded_path != current_audio_path
            ):
                delete_file(downloaded_path)


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
            # إيقاف الملف فقط مع إبقاء اتصال المحادثة الصوتية.
            call_py.stop_playout()

            await asyncio.sleep(0.3)

            old_path = current_audio_path
            current_audio_path = None

            if old_path:
                delete_file(old_path)

            await message.reply_text(
                "⏹ تم إيقاف الصوتية."
            )

            logger.info(
                "AUDIO_STOPPED | chat_id=%s",
                chat_id,
            )

        except Exception as error:
            logger.exception(
                "STOP_FAILED | chat_id=%s",
                chat_id,
            )

            await message.reply_text(
                "❌ تعذّر إيقاف الصوتية.\n"
                f"الخطأ: {type(error).__name__}\n"
                f"{str(error)[:250]}"
            )


# ==================================================
# تشغيل الحساب
# ==================================================

async def main():
    global active_chat_id
    global current_audio_path

    logger.info(
        "STARTING_TELEGRAM_CLIENT | "
        "MIN_CHANNEL_ID=%s | MIN_CHAT_ID=%s",
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

        # مزامنة المحادثات لتحديث بيانات المجموعات.
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

        # إبقاء البرنامج يعمل.
        await asyncio.Event().wait()

    except Exception:
        logger.exception("TELEGRAM_CLIENT_FATAL_ERROR")
        raise

    finally:
        try:
            if call_py.is_connected:
                call_py.stop_playout()
                await call_py.stop()

        except Exception:
            logger.exception("FINAL_GROUP_CALL_STOP_FAILED")

        if app.is_connected:
            await app.stop()

            logger.info("TELEGRAM_CLIENT_STOPPED")

        if current_audio_path:
            delete_file(current_audio_path)


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
