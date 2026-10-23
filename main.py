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
    raise RuntimeError("تأكدي من وجود API_ID وAPI_HASH وSESSION_STRING في Render.")

API_ID = int(API_ID)
FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    raise RuntimeError("لم يُعثر على ffmpeg في بيئة التشغيل.")

DOWNLOAD_DIR = "/tmp/quran_audio_bot"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
play_lock = None
connection_event = None
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
        "telegram_client": "connected" if app.is_connected else "disconnected",
        "broadcast": "active" if current_chat_id is not None else "inactive",
        "voice_network": "connected" if connected_chat_id is not None else "not_confirmed",
        "last_message_seen": last_message_at,
        "last_network_event_at": last_network_event_at,
        "last_flood_wait_until": last_flood_wait_until or None,
    }


def run_web():
    try:
        server = make_server("0.0.0.0", PORT, web)
        logger.info("خادم الويب يعمل على المنفذ %s", PORT)
        server.serve_forever()
    except Exception:
        logger.exception("تعذر تشغيل خادم الويب.")


def self_ping_worker():
    """إرسال طلب دوري كل 4 دقائق لمنع خمول الخدمة على Render"""
    target_url = RENDER_EXTERNAL_URL or f"http://127.0.0.1:{PORT}/ping"
    if not target_url.startswith("http"):
        target_url = f"https://{target_url}/ping"
    elif not target_url.endswith("/ping"):
        target_url = target_url.rstrip("/") + "/ping"

    logger.info("تم بدء خيط الإنقاذ الذاتي (Self-Ping) على الرابط: %s", target_url)
    while not shutting_down:
        time.sleep(240)
        try:
            req = urllib.request.Request(target_url, headers={"User-Agent": "SelfPingBot"})
            with urllib.request.urlopen(req, timeout=10) as response:
                logger.debug("إشعار الإنعاش الذاتي ناجح: status=%s", response.status)
        except Exception:
            logger.debug("فشل مؤقت في إشعار الإنعاش الذاتي.")


# =========================================================
# PyTgCalls
# =========================================================
group_call = GroupCallFactory(
    app,
    enable_logs_to_console=True,
).get_file_group_call(play_on_repeat=False)


@group_call.on_network_status_changed
async def network_status_changed(context, is_connected):
    global connected_chat_id, last_network_event_at, network_event_chat_id
    from datetime import datetime, timezone

    last_network_event_at = datetime.now(timezone.utc).isoformat()
    chat_id = current_chat_id
    network_event_chat_id = chat_id

    if is_connected:
        connected_chat_id = chat_id
        if connection_event is not None:
            connection_event.set()
        logger.info("حدث اتصال شبكة الصوت: connected=True chat_id=%s", chat_id)
    else:
        connected_chat_id = None
        if connection_event is not None:
            connection_event.clear()
        logger.warning("حدث اتصال شبكة الصوت: connected=False chat_id=%s", chat_id)


@group_call.on_playout_ended
async def stream_end_handler(*args, **kwargs):
    """إصلاح الوسائط الممررة مرونة لمنع انهيار البرنامج واستدعاء الكفارة"""
    logger.info("انتهى الملف الصوتي عبر حدث PyTgCalls. جارٍ إيقاف البث وإرسال دعاء كفارة المجلس.")
    await stop_current_broadcast(send_kaffarat=True)


# =========================================================
# Helpers
# =========================================================
async def is_admin(client, chat_id, user_id):
    """التحقق مما إذا كان المستخدم مشرفاً أو مالكاً في المجموعة"""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.exception("تعذر الفحص عن صلاحيات المستخدم: %s", e)
        return False


def delete_path(path):
    if not path:
        return
    try:
        if os.path.lexists(path):
            os.remove(path)
            logger.info("حُذف الملف المؤقت: %s", path)
    except OSError:
        logger.exception("تعذر حذف المسار المؤقت: %s", path)


async def cleanup_files():
    global current_input_path, current_converted_path
    input_path = current_input_path
    converted_path = current_converted_path
    current_input_path = None
    current_converted_path = None

    try:
        group_call.input_filename = ""
    except Exception:
        logger.exception("تعذر إعادة ضبط مصدر الصوت.")

    await asyncio.to_thread(delete_path, converted_path)
    await asyncio.to_thread(delete_path, input_path)


async def stop_current_broadcast(send_kaffarat=False):
    global current_chat_id, connected_chat_id, auto_stop_task
    
    if auto_stop_task and not auto_stop_task.done():
        auto_stop_task.cancel()
        auto_stop_task = None

    logger.info("بدء إيقاف البث وتنظيف الملفات.")
    stop_succeeded = False
    try:
        await asyncio.wait_for(group_call.stop(), timeout=15)
        stop_succeeded = True
        logger.info("تم تنفيذ group_call.stop().")
    except asyncio.TimeoutError:
        logger.error("انتهت مهلة إيقاف pytgcalls بعد 15 ثانية.")
    except Exception:
        logger.exception("تعذر إيقاف pytgcalls.")

    chat_to_notify = current_chat_id
    await cleanup_files()
    current_chat_id = None
    connected_chat_id = None
    if connection_event is not None:
        connection_event.clear()
    
    if send_kaffarat and chat_to_notify:
        try:
            logger.info("محاولة إرسال دعاء كفارة المجلس إلى المجموعة: %s", chat_to_notify)
            await app.send_message(
                chat_to_notify,
                "«سُبْحَانَكَ اللَّهُمَّ وَبِحَمْدِكَ، أَشْهَدُ أَنْ لَا إِلَهَ إِلَّا أَنْتَ أَسْتَغْفِرُكَ وَأَتُوبُ إِلَيْكَ» 🌸"
            )
            logger.info("تم إرسال دعاء كفارة المجلس بنجاح.")
        except Exception as e:
            logger.exception("تعذر إرسال رسالة كفارة المجلس للمجموعة بسبب الخطأ: %s", e)

    logger.info("اكتملت محاولة تنظيف البث. stop_succeeded=%s", stop_succeeded)
    return stop_succeeded


async def get_audio_duration(file_path):
    """استخراج مدة الملف الصوتي بالثواني"""
    ffprobe_cmd = FFMPEG.replace("ffmpeg", "ffprobe") if "ffmpeg" in FFMPEG else "ffprobe"
    command = [
        ffprobe_cmd, "-v", "error",
        "-show_entries", "format=duration", "-of",
        "default=noprint_wrappers=1:nokey=1", file_path
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0:
            duration = float(stdout.decode().strip())
            return duration
    except Exception:
        logger.exception("تعذر استخراج مدة الصوت عبر ffprobe.")
    return 0.0


async def schedule_auto_stop(duration, chat_id):
    """مؤقت خلفي ينتهي بانتهاء مدة الصوت ليوقف البث ويرسل الدعاء تلقائياً"""
    try:
        logger.info("تم جدولة إيقاف البث التلقائي بعد %.1f ثانية.", duration)
        await asyncio.sleep(duration + 2)
        if current_chat_id == chat_id:
            logger.info("انتهت مدة الملف الصوتي تماماً. جارٍ الإيقاف التلقائي وإرسال الكفارة...")
            await stop_current_broadcast(send_kaffarat=True)
    except asyncio.CancelledError:
        logger.info("تم إلغاء مؤقت الإيقاف التلقائي.")
    except Exception:
        logger.exception("خطأ في مؤقت الإيقاف التلقائي.")


async def convert_audio_for_call(input_path, output_path):
    """تحويل الملف الصوتي عبر FFmpeg إلى صيغة PCM بشكل مرن يغطي الأخطاء الهيكلية للملفات"""
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-err_detect", "ignore_err",  # التغاضي عن الإطارات التالفة
        "-i", input_path, "-vn", "-ac", "2", "-ar", "48000",
        "-acodec", "pcm_s16le", "-f", "s16le", output_path,
    ]
    logger.info("بدء تحويل الملف الصوتي عبر FFmpeg...")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        err_msg = stderr.decode('utf-8', errors='ignore')
        logger.error("فشل تحويل FFmpeg: %s", err_msg)
        raise RuntimeError(f"فشل معالجة الملف الصوتي عبر FFmpeg: {err_msg[:150]}")
    logger.info("اكتمل تحويل الملف الصوتي بنجاح.")


async def start_group_call_with_floodwait(chat_id, max_attempts=2):
    global last_flood_wait_until
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("محاولة الانضمام للمحادثة الصوتية %s (attempt=%s).", chat_id, attempt)
            await group_call.start(chat_id)
            return
        except FloodWait as error:
            wait_seconds = int(getattr(error, "value", 0) or getattr(error, "x", 0) or 0)
            last_flood_wait_until = time.time() + wait_seconds
            logger.warning("تيليجرام فرض FloodWait لمدة %s ثانية.", wait_seconds)
            if wait_seconds <= 0 or attempt >= max_attempts:
                raise
            await asyncio.sleep(wait_seconds + 2)


# =========================================================
# Commands
# =========================================================
@app.on_message(filters.command("ping") & filters.group)
async def ping_command(client, message):
    try:
        await message.reply_text(
            "البوت يعمل ✅\n"
            f"اتصال تيليجرام: {'متصل' if app.is_connected else 'غير متصل'}"
        )
    except Exception:
        logger.exception("فشل الرد على /ping.")


@app.on_message(filters.command("status") & filters.group)
async def status_command(client, message):
    if current_chat_id is None:
        broadcast = "لا يوجد بث مسجل حاليًا."
    elif connected_chat_id is not None:
        broadcast = "اتصال شبكة البث مؤكد."
    else:
        broadcast = "البث مسجل لكن اتصال الصوت غير مؤكد."
    try:
        await message.reply_text(
            "📊 حالة البوت\n"
            f"تيليجرام: {'متصل' if app.is_connected else 'غير متصل'}\n"
            f"البث: {broadcast}"
        )
    except Exception:
        logger.exception("فشل الرد على /status.")


@app.on_message(filters.command("play") & filters.group)
async def play_command(client, message):
    global current_chat_id, current_input_path, current_converted_path, connected_chat_id, auto_stop_task
    
    # فحص صلاحية المشرف
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        await message.reply_text("⚠️ هذا الأمر مقتصر على مشرفي المجموعة فقط.")
        return

    status = None
    downloaded_path = None
    converted_path = None

    if play_lock is None or connection_event is None:
        try:
            await message.reply_text("❌ البوت لم يكتمل تهيئته. راجعي سجلات Render.")
        except Exception:
            logger.exception("تعذر إرسال رسالة عدم التهيئة.")
        return

    async with play_lock:
        try:
            if shutting_down:
                await message.reply_text("⚠️ البوت يستعد للإغلاق. حاولي لاحقًا.")
                return
            if not app.is_connected:
                await message.reply_text("❌ اتصال تيليجرام غير متاح حاليًا.")
                return

            target = message.reply_to_message
            if target is None:
                await message.reply_text("⚠️ ردّي على رسالة صوتية أو ملف صوتي ثم أرسلي /play.")
                return

            is_audio = bool(
                target.audio or target.voice or
                (target.document and (target.document.mime_type or "").startswith("audio/"))
            )
            if not is_audio:
                await message.reply_text("⚠️ الرسالة التي رددتِ عليها لا تحتوي على ملف صوتي.")
                return

            status = await message.reply_text("⏳ جارٍ تنزيل ومعالجة الملف الصوتي...")

            if current_chat_id is not None:
                stopped = await stop_current_broadcast(send_kaffarat=False)
                if not stopped:
                    await status.edit_text("❌ تعذر إيقاف البث السابق بأمان.")
                    return

            # جلب الرسالة الحديثة للرد تجنباً لتنفيذ روابط ملفات منتهية الصلاحية
            fresh_message = await client.get_messages(message.chat.id, target.id)
            fresh_target = fresh_message if fresh_message else target

            downloaded_path = await client.download_media(
                fresh_target,
                file_name=DOWNLOAD_DIR + os.sep,
            )
            if not downloaded_path or not os.path.isfile(downloaded_path) or os.path.getsize(downloaded_path) == 0:
                raise RuntimeError("تعذر تنزيل الملف الصوتي بنجاح (قد تكون صلاحية الملف قد انتهت). أعد إعادة إرسال الملف.")

            audio_duration = await get_audio_duration(downloaded_path)
            converted_path = os.path.join(DOWNLOAD_DIR, f"audio_{uuid.uuid4().hex}.pcm")
            
            await status.edit_text("⚙️ جارٍ تحويل الصوت للتوافق مع البث...")
            await convert_audio_for_call(downloaded_path, converted_path)

            current_input_path = downloaded_path
            current_converted_path = converted_path
            downloaded_path = None
            converted_path = None
            current_chat_id = message.chat.id
            connected_chat_id = None
            connection_event.clear()

            await status.edit_text("🔊 تمت المعالجة.\n⏳ جارٍ الاتصال بالمحادثة الصوتية...")

            await start_group_call_with_floodwait(current_chat_id, max_attempts=2)

            try:
                await asyncio.wait_for(connection_event.wait(), timeout=45)
            except asyncio.TimeoutError:
                await status.edit_text(
                    "⚠️ طلب الانضمام نُفّذ، لكن لم يصل تأكيد شبكة الصوت بعد.\n"
                    "تأكدي من فتح المحادثة الصوتية في المجموعة."
                )
                return

            group_call.input_filename = current_converted_path

            if audio_duration > 0:
                auto_stop_task = asyncio.create_task(schedule_auto_stop(audio_duration, current_chat_id))

            await status.edit_text("✅ اتصل البث بالمحادثة الصوتية وبدأ بث التلاوة بنجاح.")

        except Exception as error:
            logger.exception("حدث خطأ أثناء تنفيذ /play: %s", error)
            await stop_current_broadcast(send_kaffarat=False)
            if status:
                try:
                    await status.edit_text(f"❌ تعذر بدء البث.\nالخطأ التفصيلي: {str(error)[:250]}")
                except Exception:
                    pass
        finally:
            if downloaded_path:
                await asyncio.to_thread(delete_path, downloaded_path)
            if converted_path:
                await asyncio.to_thread(delete_path, converted_path)


@app.on_message(filters.command("stop") & filters.group)
async def stop_command(client, message):
    # فحص صلاحية المشرف
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        await message.reply_text("⚠️ هذا الأمر مقتصر على مشرفي المجموعة فقط.")
        return

    if play_lock is None:
        await message.reply_text("❌ البوت لم يكتمل تهيئته.")
        return
    async with play_lock:
        if current_chat_id is None:
            await message.reply_text("ℹ️ لا يوجد بث نشط مسجل حاليًا.")
            return
        if current_chat_id != message.chat.id:
            await message.reply_text("⚠️ البث المسجل موجود في مجموعة أخرى.")
            return
        stopped = await stop_current_broadcast(send_kaffarat=True)
        if stopped:
            await message.reply_text("⏹️ تم إيقاف البث وتنظيف الملفات المؤقتة.")
        else:
            await message.reply_text("⚠️ تعذر التأكد من إيقاف اتصال الصوت بالكامل.")


# =========================================================
# Main lifecycle
# =========================================================
async def main():
    global shutting_down, play_lock, connection_event
    play_lock = asyncio.Lock()
    connection_event = asyncio.Event()
    logger.info("بدء تشغيل البوت...")
    monitor_task = None
    try:
        await app.start()
        me = await app.get_me()
        logger.info("تم الاتصال بتيليجرام. account_id=%s username=%s", me.id, me.username)
        monitor_task = asyncio.create_task(telegram_watchdog())
        await idle()
    except Exception:
        logger.exception("حدث خطأ في التشغيل الرئيسي.")
        raise
    finally:
        shutting_down = True
        if monitor_task:
            monitor_task.cancel()
        try:
            await stop_current_broadcast(send_kaffarat=False)
        except Exception:
            pass
        try:
            if app.is_connected:
                await app.stop()
        except Exception:
            pass


async def telegram_watchdog():
    while not shutting_down:
        try:
            if app.is_connected:
                await asyncio.wait_for(app.get_me(), timeout=15)
            await asyncio.sleep(60)
        except Exception:
            await asyncio.sleep(10)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True, name="web-server").start()
    threading.Thread(target=self_ping_worker, daemon=True, name="self-ping").start()
    app.run(main())
