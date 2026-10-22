import os
import asyncio
import logging
import threading
import shutil
import uuid
import time

from flask import Flask
from werkzeug.serving import make_server
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait
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
# Web server
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
connected_chat_id = None
shutting_down = False
play_lock = None
connection_event = None
last_message_at = None
last_network_event_at = None
last_flood_wait_until = 0.0
network_event_chat_id = None


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
        logger.info(
            "حدث اتصال شبكة الصوت: connected=True chat_id=%s context_type=%s",
            chat_id, type(context).__name__,
        )
    else:
        connected_chat_id = None
        if connection_event is not None:
            connection_event.clear()
        logger.warning(
            "حدث اتصال شبكة الصوت: connected=False chat_id=%s context_type=%s",
            chat_id, type(context).__name__,
        )


# =========================================================
# Temporary-file helpers
# =========================================================
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
    global current_input_path
    input_path = current_input_path
    current_input_path = None

    try:
        group_call.input_filename = ""
    except Exception:
        logger.exception("تعذر إعادة ضبط مصدر الصوت.")

    await asyncio.to_thread(delete_path, input_path)


async def stop_current_broadcast():
    global current_chat_id, connected_chat_id
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

    await cleanup_files()
    current_chat_id = None
    connected_chat_id = None
    if connection_event is not None:
        connection_event.clear()
    logger.info("اكتملت محاولة تنظيف البث. stop_succeeded=%s", stop_succeeded)
    return stop_succeeded


async def start_group_call_with_floodwait(chat_id, max_attempts=2):
    """Retry join only after Telegram's required FloodWait interval."""
    global last_flood_wait_until
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("محاولة الانضمام للمحادثة الصوتية %s (attempt=%s).", chat_id, attempt)
            await group_call.start(chat_id)
            return
        except FloodWait as error:
            wait_seconds = int(getattr(error, "value", 0) or getattr(error, "x", 0) or 0)
            last_flood_wait_until = time.time() + wait_seconds
            logger.warning(
                "تيليجرام فرض FloodWait لمدة %s ثانية عند JoinGroupCall.",
                wait_seconds,
            )
            if wait_seconds <= 0:
                raise
            if attempt >= max_attempts:
                raise
            await asyncio.sleep(wait_seconds + 2)


# =========================================================
# Incoming-message visibility
# =========================================================
@app.on_message(filters.incoming & filters.group, group=-10)
async def log_incoming_group_messages(client, message):
    global last_message_at
    from datetime import datetime, timezone
    last_message_at = datetime.now(timezone.utc).isoformat()
    text = message.text or message.caption or ""
    logger.info(
        "رسالة واردة: chat_id=%s user_id=%s text=%r",
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None) if message.from_user else None,
        text[:120],
    )


@app.on_message(filters.group & filters.text & filters.regex(r"^/"), group=-5)
async def log_group_commands(client, message):
    logger.info(
        "تم رصد أمر في المجموعة: text=%r chat_id=%s outgoing=%s reply_id=%s",
        (message.text or "")[:100],
        getattr(message.chat, "id", None),
        getattr(message, "outgoing", None),
        getattr(message.reply_to_message, "id", None) if message.reply_to_message else None,
    )


# =========================================================
# Commands
# =========================================================
@app.on_message(filters.command("ping") & filters.group)
async def ping_command(client, message):
    logger.info(
        "وصل /ping. chat_id=%s outgoing=%s connected=%s",
        message.chat.id, message.outgoing, app.is_connected,
    )
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
    global current_chat_id, current_input_path, connected_chat_id
    status = None
    downloaded_path = None

    reply = message.reply_to_message
    logger.info(
        "تم استدعاء /play: chat_id=%s outgoing=%s message_id=%s reply_id=%s "
        "reply_audio=%s reply_voice=%s reply_document=%s",
        getattr(message.chat, "id", None),
        getattr(message, "outgoing", None),
        getattr(message, "id", None),
        getattr(reply, "id", None) if reply else None,
        bool(reply and reply.audio),
        bool(reply and reply.voice),
        bool(reply and reply.document),
    )

    if play_lock is None or connection_event is None:
        logger.error("لم تتم تهيئة قفل التشغيل أو حدث اتصال الصوت.")
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
                logger.warning("/play وصل دون الرد على رسالة أخرى.")
                await message.reply_text("⚠️ ردّي على رسالة صوتية أو ملف صوتي ثم أرسلي /play.")
                return

            is_audio = bool(
                target.audio or target.voice or
                (target.document and (target.document.mime_type or "").startswith("audio/"))
            )
            if not is_audio:
                await message.reply_text("⚠️ الرسالة التي رددتِ عليها لا تحتوي على ملف صوتي.")
                return

            status = await message.reply_text("⏳ جارٍ تجهيز الملف الصوتي...")

            if current_chat_id is not None:
                stopped = await stop_current_broadcast()
                if not stopped:
                    await status.edit_text("❌ تعذر إيقاف البث السابق بأمان. راجعي سجلات Render.")
                    return

            downloaded_path = await client.download_media(
                target,
                file_name=DOWNLOAD_DIR + os.sep,
            )
            if not downloaded_path or not os.path.isfile(downloaded_path):
                raise RuntimeError("تعذر تنزيل الملف الصوتي.")

            logger.info("اكتمل تنزيل الصوت. الحجم=%s بايت، المسار=%s", os.path.getsize(downloaded_path), downloaded_path)

            current_input_path = downloaded_path
            downloaded_path = None
            current_chat_id = message.chat.id
            connected_chat_id = None
            connection_event.clear()

            await status.edit_text("🔊 تم تنزيل الملف.\n⏳ جارٍ الاتصال بالمحادثة الصوتية...")

            # البدء بالانضمام للمحادثة الصوتية أولاً
            await start_group_call_with_floodwait(current_chat_id, max_attempts=2)

            try:
                await asyncio.wait_for(connection_event.wait(), timeout=45)
            except asyncio.TimeoutError:
                logger.warning(
                    "انتهت مهلة انتظار حدث الشبكة بعد نجاح group_call.start؛ "
                    "connected_chat_id=%s network_event_chat_id=%s",
                    connected_chat_id, network_event_chat_id,
                )
                await status.edit_text(
                    "⚠️ طلب الانضمام نُفّذ، لكن لم يصل تأكيد شبكة الصوت بعد.\n"
                    "راجعي سجلات Render قبل إعادة المحاولة."
                )
                return

            # تمرير ملف الصوت الأصلي مباشرة إلى PyTgCalls وتوليد تفاصيل الأخطاء عند التشغيل
            try:
                group_call.input_filename = current_input_path
                logger.info("تم تعيين ملف الصوت المباشر بنجاح إلى PyTgCalls: %s", current_input_path)
            except Exception as e:
                logger.exception("حدث خطأ أثناء تعيين ملف الصوت لـ PyTgCalls: %s", e)
                raise RuntimeError(f"تعذر تشغيل الملف الصوتي مباشرة: {str(e)}")

            logger.info("تم تأكيد اتصال شبكة البث وبدء تشغيل الملف الصوتي مباشرة.")
            await status.edit_text(
                "✅ اتصل البث بالمحادثة الصوتية وبدأ تشغيل الملف."
            )

        except FloodWait as error:
            wait_seconds = int(getattr(error, "value", 0) or getattr(error, "x", 0) or 0)
            logger.exception("فرض تيليجرام FloodWait أثناء بدء البث. مدة الانتظار=%s", wait_seconds)
            if status:
                try:
                    await status.edit_text(
                        f"⏳ تيليجرام طلب الانتظار {wait_seconds} ثانية قبل محاولة الانضمام.\n"
                        "لم أكرر المحاولة تلقائيًا لتجنب زيادة التقييد."
                    )
                except Exception:
                    logger.exception("تعذر تحديث رسالة FloodWait.")
            await stop_current_broadcast()
        except asyncio.TimeoutError:
            logger.exception("انتهت مهلة تشغيل البث.")
            if status:
                try:
                    await status.edit_text("❌ انتهت مهلة الاتصال بالمحادثة الصوتية. راجعي سجلات Render.")
                except Exception:
                    logger.exception("تعذر تحديث رسالة الحالة.")
            await stop_current_broadcast()
        except Exception as error:
            logger.exception("حدث خطأ تفصيلي أثناء تنفيذ /play: %s", error)
            await stop_current_broadcast()
            if status:
                try:
                    await status.edit_text(f"❌ تعذر بدء البث.\nالخطأ التفصيلي: {str(error)[:250]}")
                except Exception:
                    logger.exception("تعذر تحديث رسالة الخطأ.")
        finally:
            if downloaded_path:
                await asyncio.to_thread(delete_path, downloaded_path)


@app.on_message(filters.command("stop") & filters.group)
async def stop_command(client, message):
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
        stopped = await stop_current_broadcast()
        if stopped:
            await message.reply_text("⏹️ تم إيقاف البث وتنظيف الملفات المؤقتة.")
        else:
            await message.reply_text("⚠️ تعذر التأكد من إيقاف اتصال الصوت بالكامل. راجعي سجلات Render.")


@app.on_message(filters.incoming & filters.private, group=-10)
async def log_incoming_private_messages(client, message):
    global last_message_at
    from datetime import datetime, timezone
    last_message_at = datetime.now(timezone.utc).isoformat()
    text = message.text or message.caption or ""
    logger.info(
        "رسالة خاصة واردة: user_id=%s text=%r",
        getattr(message.from_user, "id", None) if message.from_user else None,
        text[:120],
    )


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
        logger.info(
            "تم الاتصال بتيليجرام. account_id=%s username=%s",
            me.id, me.username,
        )
        logger.info("البوت جاهز لاستقبال الأوامر.")
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
                await monitor_task
            except asyncio.CancelledError:
                pass
        try:
            await stop_current_broadcast()
        except Exception:
            logger.exception("حدث خطأ أثناء تنظيف البث عند الإغلاق.")
        try:
            if app.is_connected:
                await app.stop()
        except Exception:
            logger.exception("حدث خطأ أثناء إيقاف Pyrogram.")


async def telegram_watchdog():
    logger.info("بدأت مراقبة اتصال Pyrogram.")
    while not shutting_down:
        try:
            if not app.is_connected:
                logger.error("Pyrogram غير متصل.")
            else:
                me = await asyncio.wait_for(app.get_me(), timeout=15)
                logger.debug("فحص اتصال Telegram API ناجح للحساب %s.", me.id)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("فشل فحص Telegram API في مراقبة الاتصال.")
            await asyncio.sleep(10)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True, name="web-server").start()
    app.run(main())
