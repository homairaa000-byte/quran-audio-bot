import asyncio
import logging
import os
import threading

from flask import Flask
from pyrogram import Client, filters
from pyrogram import utils as pyrogram_utils
from pytgcalls import GroupCallFactory


# ==================================================
# إصلاح حدود معرّفات المجموعات والقنوات في Pyrogram
# ==================================================

# بعض معرّفات القنوات الحديثة تتجاوز الحدود القديمة
# في Pyrogram 2.0.106.
# يجب تطبيق التعديل قبل استقبال تحديثات تيليجرام.

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
# التحقق من نوع الرسالة الصوتية
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

    audio_message = message.reply_to_message

    # يجب الرد على الرسالة الصوتية نفسها.
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
            # تحميل الصوتية المحددة.
            downloaded_path = await client.download_media(
                audio_message,
                file_name="/tmp/",
            )

            if not downloaded_path:
                raise RuntimeError(
                    "لم يتمكن تيليجرام من تحميل الصوتية."
                )

            if not os.path.isfile(downloaded_path):
                raise FileNotFoundError(
                    "ملف الصوتية غير موجود بعد التحميل."
                )

            file_size = os.path.getsize(downloaded_path)

            if file_size == 0:
                raise RuntimeError(
                    "ملف الصوتية فارغ."
                )

            logger.info(
                "AUDIO_DOWNLOADED | chat_id=%s | "
                "path=%s | size=%s",
                chat_id,
                downloaded_path,
                file_size,
            )

            # لا نسمح باستخدام المشغل في مجموعتين
            # في الوقت نفسه.
            if (
                active_chat_id is not None
                and active_chat_id != chat_id
            ):
                raise RuntimeError(
                    "المشغل مستخدم في مجموعة أخرى. "
                    "أوقفي التشغيل هناك أولًا."
                )

            # الانضمام إلى المحادثة الصوتية.
            if active_chat_id is None:
                logger.info(
                    "JOINING_VOICE_CHAT | chat_id=%s",
                    chat_id,
                )

                await call_py.join(chat_id)

                active_chat_id = chat_id

                logger.info(
                    "JOINED_VOICE_CHAT | chat_id=%s",
                    chat_id,
                )

            # إيقاف الصوتية السابقة إن وجدت.
            if current_audio_path is not None:
                try:
                    await call_py.stop_audio()
                except Exception:
                    logger.exception(
                        "PREVIOUS_AUDIO_STOP_ERROR"
                    )

                old_path = current_audio_path
                current_audio_path = None

                try:
                    if (
                        old_path != downloaded_path
                        and os.path.isfile(old_path)
                    ):
                        os.remove(old_path)

                except OSError:
                    logger.exception(
                        "PREVIOUS_AUDIO_DELETE_ERROR"
                    )

            # تشغيل الصوتية المطلوبة.
            logger.info(
                "STARTING_AUDIO | chat_id=%s | file=%s",
                chat_id,
                downloaded_path,
            )

            await call_py.start_audio(downloaded_path)

            current_audio_path = downloaded_path

            await status_message.edit_text(
                "🔊 بدأ تشغيل الصوتية داخل المحادثة الصوتية."
            )

            logger.info(
                "AUDIO_STARTED_SUCCESSFULLY | chat_id=%s",
                chat_id,
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
                logger.exception(
                    "PLAYBACK_ERROR_REPLY_FAILED"
                )

            # نحذف الملف المؤقت إذا لم يبدأ استخدامه.
            try:
                if (
                    downloaded_path
                    and downloaded_path != current_audio_path
                    and os.path.isfile(downloaded_path)
                ):
                    os.remove(downloaded_path)

            except OSError:
                logger.exception(
                    "FAILED_AUDIO_FILE_CLEANUP"
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
            await call_py.stop_audio()

            logger.info(
                "AUDIO_STOPPED | chat_id=%s",
                chat_id,
            )

            await message.reply_text(
                "⏹ تم إيقاف صوتية الدرس."
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

        # مزامنة المحادثات لتحميل بياناتها.
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

        # إبقاء البرنامج قيد التشغيل.
        await asyncio.Event().wait()

    except Exception:
        logger.exception(
            "TELEGRAM_CLIENT_FATAL_ERROR"
        )
        raise

    finally:
        # محاولة إيقاف الصوتية قبل إنهاء الاتصال.
        try:
            if active_chat_id is not None:
                await call_py.stop_audio()

        except Exception:
            logger.exception(
                "FINAL_AUDIO_STOP_ERROR"
            )

        if app.is_connected:
            await app.stop()

            logger.info(
                "TELEGRAM_CLIENT_STOPPED"
            )


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
        raise2026-10-20T10:16:32.856854924Z ==> Setting WEB_CONCURRENCY=1 by default, based on available CPUs in the instance
2026-10-20T10:17:10.518034351Z 2026-10-20 10:17:10,517 | INFO | PingTask started
2026-10-20T10:17:10.970351319Z 2026-10-20 10:17:10,970 | INFO | Started 12 HandlerTasks
2026-10-20T10:17:11.166672053Z 2026-10-20 10:17:11,166 | INFO | تم الاتصال بنجاح | user_id=7690444209 | username=Omahmad11112
2026-10-20T10:17:11.166753595Z 2026-10-20 10:17:11,166 | INFO | جارٍ مزامنة المحادثات...
2026-10-20T10:17:12.333331897Z 2026-10-20 10:17:12,333 | INFO | اكتملت مزامنة المحادثات | count=39
2026-10-20T10:17:12.333377378Z 2026-10-20 10:17:12,333 | INFO | البوت جاهز. ردي على صوتية بالأمر /play.
2026-10-20T10:17:13.710165752Z ==> Your service is live 🎉
2026-10-20T10:17:13.758431985Z 2026-10-20 10:17:13,758 | INFO | 127.0.0.1 - - [20/Oct/2026 10:17:13] "GET / HTTP/1.1" 200 -
2026-10-20T10:17:13.915448282Z ==> 
2026-10-20T10:17:13.917675136Z ==> ///////////////////////////////////////////////////////////
2026-10-20T10:17:13.919300565Z ==> 
2026-10-20T10:17:13.921655773Z ==> Available at your primary URL https://quran-audio-bot-561s.onrender.com
2026-10-20T10:17:13.923579839Z ==> 
2026-10-20T10:17:13.925251369Z ==> ///////////////////////////////////////////////////////////
2026-10-20T10:22:12.767244705Z ==> Detected service running on port 10000
2026-10-20T10:22:12.904622732Z ==> Docs on specifying a port: https://render.com/docs/web-services#port-binding
2026-10-20T10:40:48.772284084Z ==> Deploying...
2026-10-20T10:40:48.880542273Z ==> Setting WEB_CONCURRENCY=1 by default, based on available CPUs in the instance
2026-10-20T10:41:04.149424595Z ==> Running 'python main.py'
2026-10-20T10:41:15.459705027Z  * Serving Flask app 'main'
2026-10-20T10:41:15.459729207Z  * Debug mode: off
2026-10-20T10:41:15.461565632Z 2026-10-20 10:41:15,461 | INFO | quran_audio_bot | STARTING_TELEGRAM_CLIENT
2026-10-20T10:41:15.556371594Z 2026-10-20 10:41:15,556 | INFO | werkzeug | WARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.
2026-10-20T10:41:15.556392844Z  * Running on all addresses (0.0.0.0)
2026-10-20T10:41:15.556396384Z  * Running on http://127.0.0.1:10000
2026-10-20T10:41:15.556399134Z  * Running on http://10.27.24.212:10000
2026-10-20T10:41:15.556402054Z 2026-10-20 10:41:15,556 | INFO | werkzeug | Press CTRL+C to quit
2026-10-20T10:41:15.653422998Z 2026-10-20 10:41:15,653 | INFO | werkzeug | 127.0.0.1 - - [20/Oct/2026 10:41:15] "HEAD / HTTP/1.1" 200 -
2026-10-20T10:41:17.762173695Z 2026-10-20 10:41:17,761 | INFO | pyrogram.connection.connection | Connecting...
2026-10-20T10:41:17.918902993Z 2026-10-20 10:41:17,918 | INFO | pyrogram.connection.connection | Connected! Production DC4 - IPv4
2026-10-20T10:41:17.919370452Z 2026-10-20 10:41:17,919 | INFO | pyrogram.session.session | NetworkTask started
2026-10-20T10:41:18.720730862Z 2026-10-20 10:41:18,718 | INFO | pyrogram.session.session | Session initialized: Layer 158
2026-10-20T10:41:18.720759932Z 2026-10-20 10:41:18,719 | INFO | pyrogram.session.session | Device: CPython 3.11.0 - Pyrogram 2.0.106
2026-10-20T10:41:18.720764683Z 2026-10-20 10:41:18,719 | INFO | pyrogram.session.session | System: Linux 7.0.0-1009-aws (en)
2026-10-20T10:41:18.720810883Z 2026-10-20 10:41:18,719 | INFO | pyrogram.session.session | Session started
2026-10-20T10:41:18.72117659Z 2026-10-20 10:41:18,721 | INFO | pyrogram.session.session | PingTask started
2026-10-20T10:41:19.079549341Z 2026-10-20 10:41:19,079 | INFO | pyrogram.dispatcher | Started 12 HandlerTasks
2026-10-20T10:41:19.259343218Z 2026-10-20 10:41:19,259 | INFO | quran_audio_bot | TELEGRAM_CONNECTED | user_id=7690444209 | username=Omahmad11112
2026-10-20T10:41:19.259368318Z 2026-10-20 10:41:19,259 | INFO | quran_audio_bot | SYNCING_DIALOGS
2026-10-20T10:41:19.983216018Z ==> Your service is live 🎉
2026-10-20T10:41:20.076958317Z 2026-10-20 10:41:20,076 | INFO | werkzeug | 127.0.0.1 - - [20/Oct/2026 10:41:20] "GET / HTTP/1.1" 200 -
2026-10-20T10:41:20.18269626Z ==> 
2026-10-20T10:41:20.184996406Z ==> ///////////////////////////////////////////////////////////
2026-10-20T10:41:20.18891622Z ==> 
2026-10-20T10:41:20.191519243Z ==> Available at your primary URL https://quran-audio-bot-561s.onrender.com
2026-10-20T10:41:20.194686239Z ==> 
2026-10-20T10:41:20.19679673Z ==> ///////////////////////////////////////////////////////////
2026-10-20T10:41:20.283360629Z 2026-10-20 10:41:20,283 | INFO | quran_audio_bot | DIALOG_SYNC_COMPLETED | count=39
2026-10-20T10:41:20.28340128Z 2026-10-20 10:41:20,283 | INFO | quran_audio_bot | BOT_READY | account_id=7690444209 | instructions=Reply to an audio message with /play
