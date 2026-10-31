import os
import asyncio
import logging
import threading
import subprocess
import shutil
import uuid
import time
import urllib.request

from flask import Flask
from werkzeug.serving import make_server
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait
from pyrogram.enums import ChatMemberStatus
from pytgcalls import GroupCallFactory

# =========================================================
# Logging
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    force=True,
)

logger = logging.getLogger("quran_audio_bot")
logging.getLogger("pyrogram").setLevel(logging.INFO)
logging.getLogger("pytgcalls").setLevel(logging.INFO)

# =========================================================
# Render configuration
# =========================================================
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "تأكدي من وجود API_ID وAPI_HASH وSESSION_STRING في Render."
    )

API_ID = int(API_ID)

FFMPEG = shutil.which("ffmpeg")

if not FFMPEG:
    raise RuntimeError("لم يُعثر على ffmpeg في بيئة التشغيل.")

DOWNLOAD_DIR = "/tmp/quran_audio_bot"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# =========================================================
# Broadcast Settings
# =========================================================

# انتظار إضافي بعد إشعار انتهاء تشغيل الملف
PLAYOUT_DRAIN_SECONDS = 8

# مهلة احتياطية بعد المدة المحسوبة للملف
FALLBACK_EXTRA_SECONDS = 120

# مهلة انتظار الاتصال بالمحادثة الصوتية
CONNECTION_TIMEOUT = 45

# =========================================================
# Web server & Self-Ping
# =========================================================
web = Flask(__name__)

app = Client(
    "quran_audio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

current_chat_id = None
current_input_path = None
current_converted_path = None

connected_chat_id = None
shutting_down = False
is_stopping = False

play_lock = None
connection_event = None
playout_ended_event = None

last_message_at = None
last_network_event_at = None
last_flood_wait_until = 0.0
network_event_chat_id = None

auto_stop_task = None


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
        "voice_network": (
            "connected"
            if connected_chat_id is not None
            else "not_confirmed"
        ),
    }


def run_web():
    try:
        server = make_server("0.0.0.0", PORT, web)

        logger.info(
            "خادم الويب يعمل على المنفذ %s",
            PORT
        )

        server.serve_forever()

    except Exception:
        logger.exception("تعذر تشغيل خادم الويب.")


def self_ping_worker():

    target_url = (
        RENDER_EXTERNAL_URL
        or f"http://127.0.0.1:{PORT}/ping"
    )

    if not target_url.startswith("http"):
        target_url = f"https://{target_url}/ping"

    elif not target_url.endswith("/ping"):
        target_url = target_url.rstrip("/") + "/ping"

    while not shutting_down:

        time.sleep(240)

        try:

            req = urllib.request.Request(
                target_url,
                headers={"User-Agent": "SelfPingBot"}
            )

            with urllib.request.urlopen(
                req,
                timeout=10
            ) as response:
                pass

        except Exception:
            pass


# =========================================================
# PyTgCalls
# =========================================================
group_call = GroupCallFactory(
    app,
    enable_logs_to_console=True,
).get_file_group_call(
    play_on_repeat=False
)


# =========================================================
# Network Status
# =========================================================
@group_call.on_network_status_changed
async def network_status_changed(context, is_connected):

    global connected_chat_id
    global last_network_event_at

    from datetime import datetime, timezone

    last_network_event_at = datetime.now(
        timezone.utc
    ).isoformat()

    chat_id = current_chat_id

    if is_connected:

        connected_chat_id = chat_id

        if connection_event is not None:
            connection_event.set()

        logger.info(
            "تم تأكيد الاتصال بالمحادثة الصوتية."
        )

    else:

        connected_chat_id = None

        if connection_event is not None:
            connection_event.clear()

        logger.warning(
            "انقطع الاتصال بالمحادثة الصوتية."
        )

        # عدم إعادة الاتصال أثناء الإيقاف المقصود
        if (
            chat_id
            and not shutting_down
            and not is_stopping
        ):

            asyncio.create_task(
                attempt_reconnect(chat_id)
            )


# =========================================================
# Playout Ended Event
# =========================================================
@group_call.on_playout_ended
async def on_playout_ended(context, filename):

    global current_converted_path

    logger.info(
        "وصل إشعار انتهاء تشغيل الملف الصوتي: %s",
        filename
    )

    if not current_converted_path:
        return

    try:

        received_path = os.path.abspath(filename)
        current_path = os.path.abspath(
            current_converted_path
        )

    except Exception:

        return

    # التأكد من أن الإشعار يخص الملف الحالي
    if received_path != current_path:

        logger.warning(
            "تم تجاهل إشعار انتهاء يخص ملفًا مختلفًا."
        )

        return

    if playout_ended_event is not None:

        playout_ended_event.set()

        logger.info(
            "تم تأكيد انتهاء تشغيل الملف الحالي."
        )


# =========================================================
# Reconnect
# =========================================================
async def attempt_reconnect(chat_id):

    for attempt in range(1, 4):

        if (
            connected_chat_id == chat_id
            or current_chat_id != chat_id
            or is_stopping
            or shutting_down
        ):
            return

        try:

            await asyncio.sleep(3)

            if (
                current_chat_id != chat_id
                or is_stopping
                or shutting_down
            ):
                return

            logger.warning(
                "محاولة إعادة الاتصال رقم %s",
                attempt
            )

            await group_call.start(chat_id)

            if (
                current_converted_path
                and os.path.exists(current_converted_path)
            ):

                group_call.input_filename = (
                    current_converted_path
                )

            return

        except Exception:

            logger.warning(
                "فشلت محاولة إعادة الاتصال رقم %s",
                attempt
            )


# =========================================================
# Helpers
# =========================================================
async def is_admin(client, chat_id, user_id):

    try:

        member = await client.get_chat_member(
            chat_id,
            user_id
        )

        return member.status in [
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]

    except Exception:

        return False


def delete_path(path):

    if not path:
        return

    try:

        if os.path.lexists(path):
            os.remove(path)

    except OSError:

        pass


async def cleanup_files():

    global current_input_path
    global current_converted_path

    input_path = current_input_path
    converted_path = current_converted_path

    current_input_path = None
    current_converted_path = None

    try:

        group_call.input_filename = ""

    except Exception:

        pass

    await asyncio.to_thread(
        delete_path,
        converted_path
    )

    await asyncio.to_thread(
        delete_path,
        input_path
    )


# =========================================================
# Stop Broadcast
# =========================================================
async def stop_current_broadcast(send_kaffarat=False):

    global current_chat_id
    global connected_chat_id
    global auto_stop_task
    global is_stopping

    current_task = asyncio.current_task()

    # إلغاء المؤقت السابق إذا كان هناك مؤقت آخر
    if (
        auto_stop_task
        and not auto_stop_task.done()
        and auto_stop_task is not current_task
    ):

        auto_stop_task.cancel()

    if auto_stop_task is not current_task:
        auto_stop_task = None

    chat_to_notify = current_chat_id

    logger.info(
        "بدء إيقاف البث. send_kaffarat=%s",
        send_kaffarat
    )

    # منع إعادة الاتصال أثناء الإيقاف
    is_stopping = True

    # إيقاف مصدر الصوت
    try:

        group_call.input_filename = ""

    except Exception:

        pass

    # إيقاف المحادثة الصوتية
    try:

        await asyncio.wait_for(
            group_call.stop(),
            timeout=15
        )

        logger.info(
            "تم إيقاف الاتصال الصوتي."
        )

    except Exception as e:

        logger.warning(
            "ملاحظة أثناء إيقاف الاتصال: %s",
            e
        )

    # تنظيف الملفات بعد الإيقاف
    await cleanup_files()

    current_chat_id = None
    connected_chat_id = None

    if connection_event is not None:
        connection_event.clear()

    if playout_ended_event is not None:
        playout_ended_event.clear()

    is_stopping = False

    # إرسال كفارة المجلس
    if send_kaffarat and chat_to_notify:

        try:

            logger.info(
                "جارٍ إرسال كفارة المجلس إلى المجموعة: %s",
                chat_to_notify
            )

            await app.send_message(
                chat_to_notify,
                "«سُبْحَانَكَ اللَّهُمَّ وَبِحَمْدِكَ، "
                "أَشْهَدُ أَنْ لَا إِلَهَ إِلَّا أَنْتَ "
                "أَسْتَغْفِرُكَ وَأَتُوبُ إِلَيْكَ» 🌸"
            )

            logger.info(
                "تم إرسال كفارة المجلس بنجاح."
            )

        except Exception as e:

            logger.error(
                "فشل إرسال كفارة المجلس: %s",
                e
            )

    return True


# =========================================================
# Audio Duration
# =========================================================
async def get_audio_duration(file_path):

    ffprobe_cmd = (
        FFMPEG.replace("ffmpeg", "ffprobe")
        if "ffmpeg" in FFMPEG
        else "ffprobe"
    )

    command = [
        ffprobe_cmd,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]

    try:

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout, _ = await process.communicate()

        if process.returncode == 0:
            return float(stdout.decode().strip())

    except Exception:

        pass

    return 0.0


# =========================================================
# Automatic Stop Timer
# =========================================================
async def schedule_auto_stop(duration, chat_id):

    global auto_stop_task

    try:

        # المهلة الاحتياطية لا تستخدم للإيقاف المبكر
        fallback_wait = (
            duration + FALLBACK_EXTRA_SECONDS
        )

        logger.info(
            "تم تشغيل مراقبة انتهاء الصوت."
        )

        logger.info(
            "مدة PCM: %.2f ثانية",
            duration
        )

        logger.info(
            "المهلة الاحتياطية: %.2f ثانية",
            fallback_wait
        )

        try:

            # انتظار إشعار انتهاء تشغيل الملف فعليًا
            await asyncio.wait_for(
                playout_ended_event.wait(),
                timeout=fallback_wait
            )

            logger.info(
                "انتهى تشغيل الملف وفق إشعار PyTgCalls."
            )

        except asyncio.TimeoutError:

            logger.warning(
                "لم يصل إشعار انتهاء التشغيل. "
                "تم الوصول إلى المهلة الاحتياطية."
            )

        # مهلة إضافية لتصريف الصوت المرسل
        logger.info(
            "انتظار %s ثوانٍ قبل إيقاف المحادثة.",
            PLAYOUT_DRAIN_SECONDS
        )

        await asyncio.sleep(
            PLAYOUT_DRAIN_SECONDS
        )

        async with play_lock:

            # التأكد أن البث ما زال هو نفسه
            if current_chat_id != chat_id:
                return

            logger.info(
                "انتهى البث. جارٍ الإيقاف التلقائي."
            )

            await stop_current_broadcast(
                send_kaffarat=True
            )

            logger.info(
                "اكتمل الإيقاف التلقائي وإرسال الدعاء."
            )

    except asyncio.CancelledError:

        logger.info(
            "تم إلغاء مؤقت الإيقاف التلقائي."
        )

    except Exception:

        logger.exception(
            "حدث خطأ في مؤقت الإيقاف التلقائي."
        )

    finally:

        if auto_stop_task is asyncio.current_task():
            auto_stop_task = None


# =========================================================
# Audio Conversion
# =========================================================
async def convert_audio_for_call(
    input_path,
    output_path
):

    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-err_detect", "ignore_err",
        "-i", input_path,
        "-vn",
        "-ac", "2",
        "-ar", "48000",
        "-acodec", "pcm_s16le",
        "-f", "s16le",
        output_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:

        err_msg = stderr.decode(
            "utf-8",
            errors="ignore"
        )

        raise RuntimeError(
            "فشل معالجة الملف الصوتي عبر FFmpeg: "
            + err_msg[:150]
        )


# =========================================================
# FloodWait Protection
# =========================================================
async def start_group_call_with_floodwait(
    chat_id,
    max_attempts=2
):

    global last_flood_wait_until

    for attempt in range(1, max_attempts + 1):

        try:

            await group_call.start(chat_id)
            return

        except FloodWait as error:

            wait_seconds = int(
                getattr(error, "value", 0)
                or getattr(error, "x", 0)
                or 0
            )

            last_flood_wait_until = (
                time.time() + wait_seconds
            )

            if wait_seconds <= 0 or attempt >= max_attempts:
                raise

            await asyncio.sleep(
                wait_seconds + 2
            )


# =========================================================
# Commands
# =========================================================

# -------------------------
# /ping
# -------------------------
@app.on_message(
    filters.command("ping") & filters.group
)
async def ping_command(client, message):

    await message.reply_text(
        "البوت يعمل ✅"
    )


# -------------------------
# /status
# -------------------------
@app.on_message(
    filters.command("status") & filters.group
)
async def status_command(client, message):

    broadcast = (
        "نشط ومستقر"
        if current_chat_id is not None
        else "لا يوجد بث"
    )

    await message.reply_text(
        f"📊 حالة البوت: {broadcast}"
    )


# -------------------------
# /play
# -------------------------
@app.on_message(
    filters.command("play") & filters.group
)
async def play_command(client, message):

    global current_chat_id
    global current_input_path
    global current_converted_path
    global connected_chat_id
    global auto_stop_task

    if (
        not message.from_user
        or not await is_admin(
            client,
            message.chat.id,
            message.from_user.id
        )
    ):

        await message.reply_text(
            "⚠️ هذا الأمر مقتصر على مشرفي المجموعة فقط."
        )

        return

    status = None
    downloaded_path = None
    converted_path = None

    async with play_lock:

        try:

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
                "⏳ جارٍ تنزيل ومعالجة الملف الصوتي..."
            )

            # إيقاف أي بث سابق
            if current_chat_id is not None:

                await stop_current_broadcast(
                    send_kaffarat=False
                )

            fresh_message = await client.get_messages(
                message.chat.id,
                target.id
            )

            fresh_target = (
                fresh_message
                if fresh_message
                else target
            )

            # تنزيل الملف الصوتي
            downloaded_path = await client.download_media(
                fresh_target,
                file_name=DOWNLOAD_DIR + os.sep,
            )

            if (
                not downloaded_path
                or not os.path.isfile(downloaded_path)
                or os.path.getsize(downloaded_path) == 0
            ):

                raise RuntimeError(
                    "تعذر تنزيل الملف الصوتي."
                )

            # إنشاء مسار الملف المحوّل
            converted_path = os.path.join(
                DOWNLOAD_DIR,
                f"audio_{uuid.uuid4().hex}.pcm"
            )

            await status.edit_text(
                "⚙️ جارٍ تحويل الصوت للتوافق مع البث..."
            )

            # تحويل الصوت
            await convert_audio_for_call(
                downloaded_path,
                converted_path
            )

            # =================================================
            # حساب مدة ملف PCM
            # =================================================

            pcm_size = os.path.getsize(
                converted_path
            )

            # 48000 عينة في الثانية
            # قناتان صوتيتان
            # 2 بايت لكل عينة

            audio_duration = pcm_size / 192000

            logger.info(
                "حجم ملف PCM: %s بايت",
                pcm_size
            )

            logger.info(
                "مدة الصوت بعد التحويل: %.2f ثانية",
                audio_duration
            )

            if audio_duration <= 0:

                raise RuntimeError(
                    "تعذر تحديد مدة الملف الصوتي."
                )

            # حفظ مسارات الملفات
            current_input_path = downloaded_path
            current_converted_path = converted_path

            downloaded_path = None
            converted_path = None

            current_chat_id = message.chat.id
            connected_chat_id = None

            connection_event.clear()
            playout_ended_event.clear()

            await status.edit_text(
                "🔊 جارٍ الاتصال بالمحادثة الصوتية وبدء البث..."
            )

            # الاتصال بالمحادثة الصوتية
            await start_group_call_with_floodwait(
                current_chat_id,
                max_attempts=2
            )

            # انتظار تأكيد الاتصال
            try:

                await asyncio.wait_for(
                    connection_event.wait(),
                    timeout=CONNECTION_TIMEOUT
                )

            except asyncio.TimeoutError:

                raise RuntimeError(
                    "استغرق الاتصال وقتًا طويلًا، "
                    "تأكدي من فتح المكالمة في المجموعة."
                )

            # =================================================
            # بدء تشغيل الملف الصوتي
            # =================================================

            logger.info(
                "جارٍ إرسال ملف PCM إلى نظام البث."
            )

            group_call.input_filename = (
                current_converted_path
            )

            logger.info(
                "تم تعيين الملف الصوتي للبث."
            )

            # =================================================
            # تشغيل مراقبة انتهاء الصوت
            # =================================================

            auto_stop_task = asyncio.create_task(
                schedule_auto_stop(
                    audio_duration,
                    current_chat_id
                )
            )

            logger.info(
                "تم تشغيل مراقبة انتهاء الصوت بنجاح."
            )

            await status.edit_text(
                "✅ بدأ البث بنجاح"
            )

        except Exception as error:

            logger.exception(
                "خطأ في /play: %s",
                error
            )

            await stop_current_broadcast(
                send_kaffarat=False
            )

            if status:

                try:

                    await status.edit_text(
                        f"❌ تعذر بدء البث: {str(error)[:200]}"
                    )

                except Exception:

                    pass

        finally:

            if downloaded_path:

                await asyncio.to_thread(
                    delete_path,
                    downloaded_path
                )

            if converted_path:

                await asyncio.to_thread(
                    delete_path,
                    converted_path
                )


# -------------------------
# /stop
# -------------------------
@app.on_message(
    filters.command("stop") & filters.group
)
async def stop_command(client, message):

    if (
        not message.from_user
        or not await is_admin(
            client,
            message.chat.id,
            message.from_user.id
        )
    ):

        await message.reply_text(
            "⚠️ هذا الأمر مقتصر على مشرفي المجموعة فقط."
        )

        return

    async with play_lock:

        if current_chat_id is None:

            await message.reply_text(
                "ℹ️ لا يوجد بث نشط حاليًا."
            )

            return

        stopped = await stop_current_broadcast(
            send_kaffarat=True
        )

        if stopped:

            await message.reply_text(
                "⏹️ تم إيقاف البث وتنظيف الملفات "
                "وإرسال الدعاء."
            )


# =========================================================
# Main
# =========================================================
async def main():

    global shutting_down
    global play_lock
    global connection_event
    global playout_ended_event

    play_lock = asyncio.Lock()
    connection_event = asyncio.Event()
    playout_ended_event = asyncio.Event()

    try:

        await app.start()

        logger.info(
            "تم تشغيل بوت التلاوة بنجاح."
        )

        await idle()

    finally:

        shutting_down = True

        try:

            await stop_current_broadcast(
                send_kaffarat=False
            )

        except Exception:

            logger.exception(
                "حدث خطأ أثناء إغلاق البوت."
            )

        try:

            if app.is_connected:
                await app.stop()

        except Exception:

            pass


# =========================================================
# Start Application
# =========================================================
if __name__ == "__main__":

    threading.Thread(
        target=run_web,
        daemon=True,
        name="web-server"
    ).start()

    threading.Thread(
        target=self_ping_worker,
        daemon=True,
        name="self-ping"
    ).start()

    app.run(main())
