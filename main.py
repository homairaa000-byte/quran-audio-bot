import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil

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
    force=True
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

if shutil.which("ffmpeg") is None:
    raise RuntimeError(
        "لم يُعثر على ffmpeg. يجب توفيره في بيئة Render."
    )


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
        "telegram_client": "configured"
    }


def run_web():
    try:
        server = make_server("0.0.0.0", PORT, web)
        logger.info("خادم الويب يعمل على المنفذ %s", PORT)
        server.serve_forever()
    except Exception:
        logger.exception("تعذر تشغيل خادم الويب")


threading.Thread(
    target=run_web,
    daemon=True,
    name="web-server"
).start()


# =====================================
# إعداد Pyrogram
# =====================================

app = Client(
    "quran_audio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True
)


# =====================================
# إعداد pytgcalls
# =====================================

group_call = GroupCallFactory(app).get_file_group_call(
    play_on_repeat=False
)

os.makedirs("downloads", exist_ok=True)

current_chat_id = None
current_input_path = None
current_converted_path = None
connected_chat_id = None

play_lock = asyncio.Lock()
connection_event = asyncio.Event()


# =====================================
# متابعة حالة اتصال المحادثة الصوتية
# =====================================

@group_call.on_network_status_changed
async def network_status_changed(context, is_connected):
    global connected_chat_id

    chat_id = None

    try:
        # في هذا الإصدار يُستخدم full_chat.id.
        full_chat = getattr(context, "full_chat", None)

        if full_chat is not None:
            full_chat_id = getattr(full_chat, "id", None)

            if full_chat_id is not None:
                chat_id = MAX_CHANNEL_ID - int(full_chat_id)

    except (AttributeError, TypeError, ValueError):
        logger.exception("تعذر تحديد المجموعة من سياق اتصال الصوت")

    if is_connected:
        connected_chat_id = chat_id
        connection_event.set()

        logger.info(
            "وصل حدث اتصال شبكة البث. المجموعة=%s",
            chat_id
        )
    else:
        connected_chat_id = None
        connection_event.clear()

        logger.warning(
            "انقطع اتصال شبكة البث. المجموعة=%s",
            chat_id
        )


# =====================================
# تحويل الصوت إلى PCM
# =====================================

def convert_audio(input_path, output_path):
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i", input_path,
        "-vn",
        "-ac", "2",
        "-ar", "48000",
        "-f", "s16le",
        output_path
    ]

    logger.info("بدء تحويل الصوت: %s", input_path)

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180
    )

    if result.returncode != 0:
        logger.error(
            "فشل FFmpeg: %s",
            result.stderr[-3000:]
        )
        raise RuntimeError("فشل تحويل الملف الصوتي.")

    if not os.path.isfile(output_path):
        raise RuntimeError("لم يُنشأ الملف الصوتي المحوّل.")

    if os.path.getsize(output_path) == 0:
        raise RuntimeError("الملف الصوتي المحوّل فارغ.")

    logger.info(
        "اكتمل التحويل. الحجم=%s بايت",
        os.path.getsize(output_path)
    )


# =====================================
# حذف الملفات
# =====================================

def delete_file(path):
    if not path:
        return

    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("حُذف الملف المؤقت: %s", path)
    except OSError:
        logger.exception("تعذر حذف الملف: %s", path)


async def cleanup():
    global current_input_path
    global current_converted_path

    input_path = current_input_path
    converted_path = current_converted_path

    current_input_path = None
    current_converted_path = None

    await asyncio.to_thread(delete_file, input_path)

    if converted_path != input_path:
        await asyncio.to_thread(delete_file, converted_path)


# =====================================
# إيقاف البث
# =====================================

async def stop_current_broadcast():
    global current_chat_id
    global connected_chat_id

    logger.info("جارٍ إيقاف البث وتنظيف الملفات")

    try:
        await group_call.stop()
    except Exception:
        logger.exception("تعذر إيقاف اتصال pytgcalls")

    current_chat_id = None
    connected_chat_id = None

    connection_event.clear()

    await cleanup()

    logger.info("اكتملت محاولة إيقاف البث")


# =====================================
# تسجيل الأوامر الواردة
# =====================================

@app.on_message(filters.command("ping"))
async def ping_command(client, message):
    logger.info(
        "وصل أمر ping. chat_id=%s user_id=%s outgoing=%s",
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None),
        message.outgoing
    )

    try:
        await message.reply_text("البوت يعمل ✅")
        logger.info("تم الرد على أمر ping")
    except Exception:
        logger.exception("فشل الرد على أمر ping")


# =====================================
# أمر play
# =====================================

@app.on_message(filters.command("play") & filters.group)
async def play_command(client, message):
    global current_chat_id
    global current_input_path
    global current_converted_path
    global connected_chat_id

    logger.info(
        "وصل أمر play. chat_id=%s outgoing=%s",
        getattr(message.chat, "id", None),
        message.outgoing
    )

    async with play_lock:
        status = None
        downloaded_path = None
        converted_path = None
        broadcast_started = False

        try:
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
                    and (target.document.mime_type or "").startswith(
                        "audio/"
                    )
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

            logger.info("جارٍ تنزيل الملف الصوتي")

            downloaded_path = await client.download_media(
                target,
                file_name="downloads/"
            )

            if (
                not downloaded_path
                or not os.path.isfile(downloaded_path)
            ):
                raise RuntimeError("تعذر تنزيل الملف الصوتي.")

            filename = os.path.splitext(
                os.path.basename(downloaded_path)
            )[0]

            converted_path = os.path.join(
                "downloads",
                f"{filename}_{int(time.time())}.raw"
            )

            await asyncio.to_thread(
                convert_audio,
                downloaded_path,
                converted_path
            )

            current_input_path = downloaded_path
            current_converted_path = converted_path

            downloaded_path = None
            converted_path = None

            connection_event.clear()
            connected_chat_id = None

            await status.edit_text(
                "🔊 تم تجهيز الملف الصوتي.\n"
                "⏳ جارٍ الاتصال بالمحادثة الصوتية..."
            )

            logger.info(
                "جارٍ طلب الاتصال بالمحادثة %s",
                message.chat.id
            )

            group_call.input_filename = current_converted_path

            await group_call.start(message.chat.id)

            logger.info(
                "انتهى استدعاء group_call.start؛ ننتظر حدث الاتصال"
            )

            await asyncio.wait_for(
                connection_event.wait(),
                timeout=30
            )

            current_chat_id = message.chat.id
            broadcast_started = True

            await status.edit_text(
                "✅ وصل حدث اتصال البث بالمحادثة الصوتية.\n"
                "إذا لم يُسمع الصوت، أرسلي السجلات الجديدة "
                "للتأكد من تشغيل الملف داخل الاتصال."
            )

            logger.info(
                "وصل حدث الاتصال للمجموعة %s",
                current_chat_id
            )

        except asyncio.TimeoutError:
            logger.error(
                "انتهت مهلة انتظار الاتصال بالمحادثة الصوتية"
            )

            if status:
                try:
                    await status.edit_text(
                        "❌ لم يصل تأكيد الاتصال بالمحادثة الصوتية خلال 30 ثانية.\n"
                        "راجعي Render Logs وأرسلي الأسطر التي ظهرت بعد /play."
                    )
                except Exception:
                    logger.exception("تعذر تحديث رسالة الخطأ")

            await stop_current_broadcast()

        except Exception as error:
            logger.exception("حدث خطأ داخل أمر play")

            await stop_current_broadcast()

            if status:
                try:
                    await status.edit_text(
                        "❌ تعذر بدء البث.\n"
                        f"الخطأ: {str(error)[:500]}"
                    )
                except Exception:
                    logger.exception("تعذر إرسال تفاصيل الخطأ")

        finally:
            if downloaded_path:
                await asyncio.to_thread(delete_file, downloaded_path)

            if converted_path:
                await asyncio.to_thread(delete_file, converted_path)


# =====================================
# أمر stop
# =====================================

@app.on_message(filters.command("stop") & filters.group)
async def stop_command(client, message):
    global current_chat_id

    logger.info(
        "وصل أمر stop. chat_id=%s outgoing=%s",
        getattr(message.chat, "id", None),
        message.outgoing
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
                "⏹️ تمت محاولة إيقاف البث وتنظيف الملفات."
            )

        except Exception:
            logger.exception("فشل تنفيذ أمر stop")

            await message.reply_text(
                "❌ حدث خطأ أثناء إيقاف البث. راجعي السجلات."
            )


# =====================================
# التقاط الأخطاء غير المتوقعة
# =====================================

@app.on_message()
async def log_other_messages(client, message):
    # لا نسجل نصوص الرسائل الخاصة أو محتواها؛ نسجل فقط وصولها.
    if message.text and message.text.startswith("/"):
        logger.info(
            "وصلت رسالة أمر غير معالجة: chat_id=%s outgoing=%s",
            getattr(message.chat, "id", None),
            message.outgoing
        )


# =====================================
# التشغيل والإغلاق
# =====================================

async def main():
    logger.info("بدء تشغيل البوت...")

    try:
        await app.start()

        logger.info("تم الاتصال بتيليجرام بنجاح.")
        logger.info("البوت جاهز لاستقبال الأوامر.")

        await idle()

    except Exception:
        logger.exception("حدث خطأ في التشغيل الرئيسي")
        raise

    finally:
        try:
            await stop_current_broadcast()
        except Exception:
            logger.exception("حدث خطأ أثناء تنظيف البث عند الإغلاق")

        try:
            await app.stop()
        except Exception:
            logger.exception("حدث خطأ أثناء إيقاف Pyrogram")


if __name__ == "__main__":
    app.run(main())
