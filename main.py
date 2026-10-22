import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil
import signal

from flask import Flask
from werkzeug.serving import make_server

from pyrogram import Client, filters, idle
from pyrogram.utils import MAX_CHANNEL_ID
from pytgcalls import GroupCallFactory


# =====================================
# التسجيل
# =====================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    force=True,
)

logger = logging.getLogger("quran_audio_bot")


# =====================================
# إعدادات Render
# =====================================

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

PORT = int(os.getenv("PORT", "10000"))

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "تأكدي من وجود API_ID وAPI_HASH وSESSION_STRING في Render."
    )

API_ID = int(API_ID)

FFMPEG = shutil.which("ffmpeg")

if not FFMPEG:
    raise RuntimeError(
        "لم يُعثر على ffmpeg في بيئة التشغيل."
    )


# =====================================
# المجلدات
# =====================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# =====================================
# خادم الويب
# =====================================

web = Flask(__name__)


@web.route("/")
def home():
    return "Quran Audio Bot is running."


@web.route("/ping")
def web_ping():
    return "pong"


@web.route("/health")
def health():
    return {
        "status": "running",
        "telegram_client": "configured",
    }


def run_web():
    try:
        server = make_server("0.0.0.0", PORT, web)

        logger.info(
            "خادم الويب يعمل على المنفذ %s",
            PORT,
        )

        server.serve_forever()

    except Exception:
        logger.exception("تعذر تشغيل خادم الويب")


threading.Thread(
    target=run_web,
    daemon=True,
    name="web-server",
).start()


# =====================================
# إعداد Pyrogram
# =====================================

app = Client(
    "quran_audio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)


# =====================================
# إعداد pytgcalls
# =====================================

group_call = GroupCallFactory(app).get_file_group_call(
    play_on_repeat=False,
)


# =====================================
# حالة البث
# =====================================

current_chat_id = None
current_input_path = None
current_fifo_path = None
current_ffmpeg_process = None

connected_chat_id = None

play_lock = asyncio.Lock()
connection_event = asyncio.Event()

shutting_down = False


# =====================================
# متابعة اتصال المحادثة الصوتية
# =====================================

@group_call.on_network_status_changed
async def network_status_changed(context, is_connected):
    global connected_chat_id

    chat_id = None

    try:
        full_chat = getattr(context, "full_chat", None)

        if full_chat is not None:
            full_chat_id = getattr(full_chat, "id", None)

            if full_chat_id is not None:
                chat_id = MAX_CHANNEL_ID - int(full_chat_id)

    except (AttributeError, TypeError, ValueError):
        logger.exception(
            "تعذر تحديد المجموعة من حدث اتصال الصوت."
        )

    if is_connected:
        connected_chat_id = chat_id
        connection_event.set()

        logger.info(
            "اتصل البث بالمحادثة الصوتية. chat_id=%s",
            chat_id,
        )

    else:
        connected_chat_id = None
        connection_event.clear()

        logger.warning(
            "انقطع اتصال شبكة البث. chat_id=%s",
            chat_id,
        )


# =====================================
# أدوات حذف الملفات
# =====================================

def delete_path(path):
    if not path:
        return

    try:
        # lexists يتعامل أيضًا مع Named Pipe.
        if os.path.lexists(path):
            os.remove(path)

            logger.info(
                "حُذف الملف المؤقت: %s",
                path,
            )

    except OSError:
        logger.exception(
            "تعذر حذف المسار المؤقت: %s",
            path,
        )


# =====================================
# إنهاء عملية FFmpeg
# =====================================

async def terminate_ffmpeg():
    global current_ffmpeg_process

    process = current_ffmpeg_process

    if process is None:
        return

    current_ffmpeg_process = None

    try:
        if process.returncode is None:
            logger.info("جارٍ إيقاف عملية FFmpeg.")

            process.terminate()

            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=5,
                )

            except asyncio.TimeoutError:
                logger.warning(
                    "لم تتوقف FFmpeg؛ سيتم إنهاؤها."
                )

                process.kill()
                await process.wait()

        else:
            await process.wait()

    except ProcessLookupError:
        pass

    except Exception:
        logger.exception(
            "حدث خطأ أثناء إنهاء FFmpeg."
        )


# =====================================
# تنظيف الملفات
# =====================================

async def cleanup_files():
    global current_input_path
    global current_fifo_path

    input_path = current_input_path
    fifo_path = current_fifo_path

    current_input_path = None
    current_fifo_path = None

    await terminate_ffmpeg()

    # إعادة ضبط مصدر الصوت قبل حذف FIFO.
    try:
        group_call.input_filename = ""
    except Exception:
        logger.exception(
            "تعذر إعادة ضبط مصدر الصوت."
        )

    await asyncio.to_thread(
        delete_path,
        fifo_path,
    )

    await asyncio.to_thread(
        delete_path,
        input_path,
    )


# =====================================
# إيقاف البث
# =====================================

async def stop_current_broadcast():
    global current_chat_id
    global connected_chat_id

    logger.info(
        "جارٍ إيقاف البث وتنظيف الملفات."
    )

    try:
        await group_call.stop()

    except Exception:
        logger.exception(
            "حدث خطأ أثناء إيقاف اتصال pytgcalls."
        )

    current_chat_id = None
    connected_chat_id = None

    connection_event.clear()

    await cleanup_files()

    logger.info(
        "اكتملت محاولة إيقاف البث."
    )


# =====================================
# إنشاء FIFO
# =====================================

def create_fifo(path):
    if os.path.lexists(path):
        os.remove(path)

    os.mkfifo(path, 0o600)

    logger.info(
        "تم إنشاء مسار البث التدريجي: %s",
        path,
    )


# =====================================
# تشغيل FFmpeg في FIFO
# =====================================

async def start_ffmpeg_stream(input_path, fifo_path):
    global current_ffmpeg_process

    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",
        "-i", input_path,
        "-vn",
        "-ac", "2",
        "-ar", "48000",
        "-acodec", "pcm_s16le",
        "-f", "s16le",
        fifo_path,
    ]

    logger.info(
        "بدء تحويل الصوت مباشرة إلى مسار البث."
    )

    logger.info(
        "لن يتم إنشاء ملف PCM كامل على القرص."
    )

    # لا نستخدم PIPE لمخرجات FFmpeg كي لا تمتلئ
    # ذاكرة المخزن المؤقت إذا طال البث.
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    current_ffmpeg_process = process

    logger.info(
        "بدأت عملية FFmpeg. pid=%s",
        process.pid,
    )

    return process


# =====================================
# أمر ping
# =====================================

@app.on_message(filters.command("ping"))
async def ping_command(client, message):
    logger.info(
        "وصل أمر ping. chat_id=%s outgoing=%s",
        getattr(message.chat, "id", None),
        message.outgoing,
    )

    try:
        await message.reply_text(
            "البوت يعمل ✅"
        )

        logger.info(
            "تم الرد على أمر ping."
        )

    except Exception:
        logger.exception(
            "فشل الرد على أمر ping."
        )


# =====================================
# أمر play
# =====================================

@app.on_message(
    filters.command("play") & filters.group
)
async def play_command(client, message):
    global current_chat_id
    global current_input_path
    global current_fifo_path
    global connected_chat_id

    logger.info(
        "وصل أمر play. chat_id=%s outgoing=%s",
        getattr(message.chat, "id", None),
        message.outgoing,
    )

    async with play_lock:
        status = None
        downloaded_path = None
        fifo_path = None

        try:
            if shutting_down:
                await message.reply_text(
                    "⚠️ البوت يستعد للإغلاق؛ حاولي مجددًا بعد تشغيله."
                )
                return

            target = message.reply_to_message

            if target is None:
                await message.reply_text(
                    "⚠️ ردّي على رسالة تحتوي على ملف صوتي، "
                    "ثم أرسلي /play."
                )
                return

            is_audio = bool(
                target.audio
                or target.voice
                or (
                    target.document
                    and (
                        target.document.mime_type or ""
                    ).startswith("audio/")
                )
            )

            if not is_audio:
                await message.reply_text(
                    "⚠️ الرسالة التي رددتِ عليها لا تحتوي على ملف صوتي."
                )
                return

            status = await message.reply_text(
                "⏳ جارٍ تجهيز الملف الصوتي..."
            )

            if current_chat_id is not None:
                await stop_current_broadcast()

            # تنزيل الملف الأصلي فقط.
            logger.info(
                "جارٍ تنزيل الملف الصوتي."
            )

            downloaded_path = await client.download_media(
                target,
                file_name=DOWNLOAD_DIR + os.sep,
            )

            if (
                not downloaded_path
                or not os.path.isfile(downloaded_path)
            ):
                raise RuntimeError(
                    "تعذر تنزيل الملف الصوتي."
                )

            file_size = os.path.getsize(
                downloaded_path
            )

            logger.info(
                "اكتمل تنزيل الملف. الحجم=%s بايت",
                file_size,
            )

            # مسار FIFO فريد لكل عملية بث.
            fifo_path = os.path.join(
                DOWNLOAD_DIR,
                f"audio_stream_{int(time.time())}_{os.getpid()}.fifo",
            )

            await asyncio.to_thread(
                create_fifo,
                fifo_path,
            )

            current_input_path = downloaded_path
            current_fifo_path = fifo_path

            downloaded_path = None
            fifo_path = None

            connection_event.clear()
            connected_chat_id = None

            await status.edit_text(
                "🔊 تم تجهيز الملف.\n"
                "⏳ جارٍ الاتصال بالمحادثة الصوتية..."
            )

            # نحدد المجموعة قبل بدء الاتصال حتى يبقى
            # أمر /stop قادرًا على التعرف إلى البث الجاري.
            current_chat_id = message.chat.id

            logger.info(
                "تجهيز مصدر الصوت للمجموعة %s",
                current_chat_id,
            )

            # يبدأ ffmpeg بالكتابة إلى FIFO.
            # سيبقى فتح FIFO منتظرًا حتى يفتحه قارئ البث.
            await start_ffmpeg_stream(
                current_input_path,
                current_fifo_path,
            )

            group_call.input_filename = (
                current_fifo_path
            )

            logger.info(
                "جارٍ الاتصال بالمحادثة %s",
                current_chat_id,
            )

            await group_call.start(
                current_chat_id
            )

            await asyncio.wait_for(
                connection_event.wait(),
                timeout=30,
            )

            logger.info(
                "تم تأكيد اتصال شبكة البث."
            )

            await status.edit_text(
                "✅ اتصل البث بالمحادثة الصوتية.\n"
                "🎧 يجري إرسال الصوت تدريجيًا."
            )

            # لا ننتظر نهاية FFmpeg داخل معالج الأمر.
            # بذلك يستطيع البوت متابعة استقبال الأوامر.
            logger.info(
                "اكتمل إعداد البث؛ البوت مستعد لاستقبال الأوامر."
            )

        except asyncio.TimeoutError:
            logger.error(
                "انتهت مهلة انتظار اتصال المحادثة الصوتية."
            )

            await stop_current_broadcast()

            if status:
                try:
                    await status.edit_text(
                        "❌ لم يصل تأكيد اتصال البث خلال 30 ثانية.\n"
                        "راجعي سجلات Render."
                    )
                except Exception:
                    logger.exception(
                        "تعذر تحديث رسالة الحالة."
                    )

        except Exception as error:
            logger.exception(
                "حدث خطأ أثناء تنفيذ /play."
            )

            await stop_current_broadcast()

            if status:
                try:
                    await status.edit_text(
                        "❌ تعذر بدء البث.\n"
                        f"الخطأ: {str(error)[:400]}"
                    )
                except Exception:
                    logger.exception(
                        "تعذر إرسال تفاصيل الخطأ."
                    )

        finally:
            if downloaded_path:
                await asyncio.to_thread(
                    delete_path,
                    downloaded_path,
                )

            if fifo_path:
                await asyncio.to_thread(
                    delete_path,
                    fifo_path,
                )


# =====================================
# أمر stop
# =====================================

@app.on_message(
    filters.command("stop") & filters.group
)
async def stop_command(client, message):
    logger.info(
        "وصل أمر stop. chat_id=%s outgoing=%s",
        getattr(message.chat, "id", None),
        message.outgoing,
    )

    async with play_lock:
        if current_chat_id is None:
            await message.reply_text(
                "ℹ️ لا يوجد بث نشط مسجل حاليًا."
            )
            return

        if current_chat_id != message.chat.id:
            await message.reply_text(
                "⚠️ البث المسجل موجود في مجموعة أخرى."
            )
            return

        try:
            await stop_current_broadcast()

            await message.reply_text(
                "⏹️ تم إيقاف البث وتنظيف الملفات المؤقتة."
            )

        except Exception:
            logger.exception(
                "فشل تنفيذ أمر stop."
            )

            await message.reply_text(
                "❌ حدث خطأ أثناء إيقاف البث. راجعي السجلات."
            )


# =====================================
# تسجيل الأوامر الأخرى
# =====================================

@app.on_message()
async def log_other_messages(client, message):
    if (
        message.text
        and message.text.startswith("/")
    ):
        logger.info(
            "وصلت رسالة أمر. chat_id=%s outgoing=%s",
            getattr(message.chat, "id", None),
            message.outgoing,
        )


# =====================================
# التشغيل والإغلاق
# =====================================

async def main():
    global shutting_down

    logger.info(
        "بدء تشغيل البوت..."
    )

    try:
        await app.start()

        logger.info(
            "تم الاتصال بتيليجرام بنجاح."
        )

        logger.info(
            "البوت جاهز لاستقبال الأوامر."
        )

        await idle()

    except Exception:
        logger.exception(
            "حدث خطأ في التشغيل الرئيسي."
        )
        raise

    finally:
        shutting_down = True

        try:
            await stop_current_broadcast()

        except Exception:
            logger.exception(
                "حدث خطأ أثناء تنظيف البث عند الإغلاق."
            )

        try:
            await app.stop()

        except Exception:
            logger.exception(
                "حدث خطأ أثناء إيقاف Pyrogram."
            )


if __name__ == "__main__":
    app.run(main())
