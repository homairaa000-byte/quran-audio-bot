import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil
import uuid

from flask import Flask
from werkzeug.serving import make_server

from pyrogram import Client, filters, idle
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

logging.getLogger("pyrogram").setLevel(logging.INFO)
logging.getLogger("pytgcalls").setLevel(logging.DEBUG)


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

DOWNLOAD_DIR = os.path.join(
    "/tmp",
    "quran_audio_bot",
)

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
        "telegram_client": (
            "connected" if app.is_connected else "disconnected"
        ),
        "broadcast": (
            "active" if current_chat_id is not None else "inactive"
        ),
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
        logger.exception(
            "تعذر تشغيل خادم الويب."
        )


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

group_call = GroupCallFactory(
    app,
    enable_logs_to_console=True,
).get_file_group_call(
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
            chat_id = getattr(full_chat, "id", None)

    except Exception:
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
# أدوات الملفات
# =====================================

def delete_path(path):
    if not path:
        return

    try:
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


def create_fifo(path):
    if os.path.lexists(path):
        os.remove(path)

    os.mkfifo(path, 0o600)

    logger.info(
        "تم إنشاء FIFO: %s",
        path,
    )


# =====================================
# إنهاء FFmpeg
# =====================================

async def terminate_ffmpeg():
    global current_ffmpeg_process

    process = current_ffmpeg_process

    if process is None:
        return

    current_ffmpeg_process = None

    try:
        if process.returncode is None:
            logger.info(
                "جارٍ إيقاف FFmpeg. pid=%s",
                process.pid,
            )

            process.terminate()

            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=5,
                )

            except asyncio.TimeoutError:
                logger.warning(
                    "لم تتوقف FFmpeg خلال المهلة؛ "
                    "سيتم إنهاؤها."
                )

                process.kill()

                await asyncio.wait_for(
                    process.wait(),
                    timeout=5,
                )

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

    # إعادة ضبط المصدر قبل حذف FIFO.
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
        "بدء إيقاف البث وتنظيف الملفات."
    )

    stop_succeeded = False

    try:
        await asyncio.wait_for(
            group_call.stop(),
            timeout=15,
        )

        stop_succeeded = True

        logger.info(
            "تم إيقاف اتصال pytgcalls."
        )

    except asyncio.TimeoutError:
        logger.error(
            "انتهت مهلة إيقاف pytgcalls بعد 15 ثانية."
        )

    except Exception:
        logger.exception(
            "تعذر إيقاف اتصال pytgcalls."
        )

    # لا نحذف مصدر الصوت قبل محاولة إيقاف اتصال البث.
    await cleanup_files()

    current_chat_id = None
    connected_chat_id = None

    connection_event.clear()

    logger.info(
        "اكتملت محاولة تنظيف البث. stop_succeeded=%s",
        stop_succeeded,
    )

    return stop_succeeded


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
        "بدء تحويل الصوت تدريجيًا إلى FIFO."
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    current_ffmpeg_process = process

    logger.info(
        "بدأت FFmpeg. pid=%s",
        process.pid,
    )

    return process


# =====================================
# مراقبة عملية FFmpeg
# =====================================

async def monitor_ffmpeg(process, fifo_path):
    global current_ffmpeg_process

    try:
        return_code = await process.wait()

        if return_code != 0:
            logger.error(
                "انتهت FFmpeg بخطأ. return_code=%s",
                return_code,
            )

            if process.stderr:
                error_text = await process.stderr.read()

                if error_text:
                    logger.error(
                        "تفاصيل FFmpeg: %s",
                        error_text.decode(
                            "utf-8",
                            errors="replace",
                        )[-3000:],
                    )

        else:
            logger.info(
                "انتهت FFmpeg من قراءة الملف الصوتي."
            )

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "حدث خطأ أثناء مراقبة FFmpeg."
        )


# =====================================
# فحص حالة البوت
# =====================================

@app.on_message(
    filters.command("ping") & filters.group
)
async def ping_command(client, message):
    logger.info(
        "وصل /ping. chat_id=%s outgoing=%s connected=%s",
        message.chat.id,
        message.outgoing,
        app.is_connected,
    )

    try:
        await message.reply_text(
            "البوت يعمل ✅\n"
            f"اتصال تيليجرام: "
            f"{'متصل' if app.is_connected else 'غير متصل'}"
        )

    except Exception:
        logger.exception(
            "فشل الرد على /ping."
        )


# =====================================
# حالة البث
# =====================================

@app.on_message(
    filters.command("status") & filters.group
)
async def status_command(client, message):
    logger.info(
        "وصل /status. connected=%s chat_id=%s",
        app.is_connected,
        current_chat_id,
    )

    try:
        if current_chat_id is None:
            broadcast_status = "لا يوجد بث مسجل حاليًا."

        elif connected_chat_id is not None:
            broadcast_status = "اتصال البث مؤكد."

        else:
            broadcast_status = (
                "هناك بث مسجل، لكن اتصال الصوت غير مؤكد."
            )

        await message.reply_text(
            "📊 حالة البوت\n"
            f"تيليجرام: "
            f"{'متصل' if app.is_connected else 'غير متصل'}\n"
            f"البث: {broadcast_status}"
        )

    except Exception:
        logger.exception(
            "فشل الرد على /status."
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
        "وصل /play. chat_id=%s outgoing=%s",
        message.chat.id,
        message.outgoing,
    )

    status = None
    downloaded_path = None
    fifo_path = None

    async with play_lock:
        try:
            if shutting_down:
                await message.reply_text(
                    "⚠️ البوت يستعد للإغلاق. حاولي لاحقًا."
                )
                return

            if not app.is_connected:
                await message.reply_text(
                    "❌ اتصال تيليجرام غير متاح حاليًا."
                )
                logger.error(
                    "تم رفض /play لأن Pyrogram غير متصل."
                )
                return

            target = message.reply_to_message

            if target is None:
                await message.reply_text(
                    "⚠️ ردّي على رسالة صوتية أو ملف صوتي "
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
                    "⚠️ الرسالة التي رددتِ عليها "
                    "لا تحتوي على ملف صوتي."
                )
                return

            status = await message.reply_text(
                "⏳ جارٍ تجهيز الملف الصوتي..."
            )

            # تنظيف البث السابق إن وجد.
            if current_chat_id is not None:
                logger.info(
                    "يوجد بث سابق؛ ستتم محاولة إيقافه."
                )

                stopped = await stop_current_broadcast()

                if not stopped:
                    await status.edit_text(
                        "❌ تعذر التأكد من إيقاف البث السابق.\n"
                        "لن أبدأ بثًا جديدًا لتجنب تداخل الاتصالات.\n"
                        "راجعي Logs في Render."
                    )
                    return

            logger.info(
                "بدء تنزيل الملف الصوتي."
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
                "اكتمل التنزيل. الحجم=%s بايت",
                file_size,
            )

            fifo_path = os.path.join(
                DOWNLOAD_DIR,
                f"audio_{uuid.uuid4().hex}.fifo",
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

            current_chat_id = message.chat.id

            await status.edit_text(
                "🔊 تم تجهيز الملف الصوتي.\n"
                "⏳ جارٍ الاتصال بالمحادثة الصوتية..."
            )

            # إعداد FFmpeg.
            process = await start_ffmpeg_stream(
                current_input_path,
                current_fifo_path,
            )

            # مراقبة عملية FFmpeg في مهمة منفصلة.
            asyncio.create_task(
                monitor_ffmpeg(
                    process,
                    current_fifo_path,
                )
            )

            # ضبط مصدر الصوت.
            group_call.input_filename = current_fifo_path

            logger.info(
                "بدء اتصال البث بالمجموعة %s",
                current_chat_id,
            )

            await asyncio.wait_for(
                group_call.start(current_chat_id),
                timeout=40,
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
                "🎧 بدأ تجهيز إرسال الصوت.\n"
                "إذا انقطع الصوت، أرسلي /status."
            )

        except asyncio.TimeoutError:
            logger.exception(
                "انتهت مهلة الاتصال أو تأكيد البث."
            )

            if status:
                try:
                    await status.edit_text(
                        "❌ انتهت مهلة الاتصال بالمحادثة الصوتية.\n"
                        "راجعي Logs في Render لمعرفة السبب."
                    )
                except Exception:
                    logger.exception(
                        "تعذر تحديث رسالة الحالة."
                    )

            await stop_current_broadcast()

        except Exception as error:
            logger.exception(
                "حدث خطأ أثناء تنفيذ /play."
            )

            await stop_current_broadcast()

            if status:
                try:
                    await status.edit_text(
                        "❌ تعذر بدء البث.\n"
                        f"الخطأ: {str(error)[:300]}"
                    )
                except Exception:
                    logger.exception(
                        "تعذر تحديث رسالة الخطأ."
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
        "وصل /stop. chat_id=%s outgoing=%s",
        message.chat.id,
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

        stopped = await stop_current_broadcast()

        if stopped:
            await message.reply_text(
                "⏹️ تمت محاولة إيقاف البث وتنظيف الملفات المؤقتة."
            )

        else:
            await message.reply_text(
                "⚠️ تعذر التأكد من إيقاف اتصال الصوت بالكامل.\n"
                "راجعي Logs في Render."
            )


# =====================================
# تسجيل الأوامر غير المعالجة
# =====================================

@app.on_message()
async def log_other_messages(client, message):
    if message.text and message.text.startswith("/"):
        logger.info(
            "رسالة أمر وصلت إلى معالج الرسائل العام. "
            "text=%r chat_id=%s outgoing=%s",
            message.text[:100],
            getattr(message.chat, "id", None),
            message.outgoing,
        )


# =====================================
# مراقبة اتصال Pyrogram
# =====================================

async def monitor_telegram():
    logger.info(
        "بدأت مراقبة اتصال Pyrogram."
    )

    while not shutting_down:
        try:
            if app.is_connected:
                logger.debug(
                    "اتصال Pyrogram يعمل."
                )
            else:
                logger.error(
                    "اكتُشف أن Pyrogram غير متصل."
                )

            await asyncio.sleep(15)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "خطأ في مراقبة اتصال Pyrogram."
            )
            await asyncio.sleep(5)


# =====================================
# التشغيل والإغلاق
# =====================================

async def main():
    global shutting_down

    logger.info(
        "بدء تشغيل البوت..."
    )

    monitor_task = None

    try:
        await app.start()

        logger.info(
            "تم الاتصال بتيليجرام بنجاح."
        )

        logger.info(
            "البوت جاهز لاستقبال الأوامر."
        )

        monitor_task = asyncio.create_task(
            monitor_telegram()
        )

        await idle()

    except Exception:
        logger.exception(
            "حدث خطأ في التشغيل الرئيسي."
        )
        raise

    finally:
        shutting_down = True

        if monitor_task:
            monitor_task.cancel()

            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        try:
            await stop_current_broadcast()

        except Exception:
            logger.exception(
                "حدث خطأ أثناء تنظيف البث عند الإغلاق."
            )

        try:
            if app.is_connected:
                await app.stop()

        except Exception:
            logger.exception(
                "حدث خطأ أثناء إيقاف Pyrogram."
            )


# =====================================
# نقطة الدخول
# =====================================

if __name__ == "__main__":
    threading.Thread(
        target=run_web,
        daemon=True,
        name="web-server",
    ).start()

    app.run(main())
