import os
import logging
import asyncio
import threading
import subprocess

from flask import Flask
from werkzeug.serving import make_server

from pyrogram import Client, filters, idle
from pyrogram import utils as pyrogram_utils
from pytgcalls import GroupCallFactory


# =========================
# إعداد التسجيل
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# =========================
# متغيرات Render
# =========================

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

PORT = int(os.getenv("PORT", "10000"))


if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "يجب ضبط API_ID وAPI_HASH وSESSION_STRING في Environment على Render."
    )

API_ID = int(API_ID)


# =========================
# توافق Pyrogram
# =========================

if hasattr(pyrogram_utils, "MIN_CHANNEL_ID"):
    MIN_CHANNEL_ID = pyrogram_utils.MIN_CHANNEL_ID
else:
    MIN_CHANNEL_ID = -1000000000000

if hasattr(pyrogram_utils, "MIN_CHAT_ID"):
    MIN_CHAT_ID = pyrogram_utils.MIN_CHAT_ID
else:
    MIN_CHAT_ID = -999999999999

_original_get_peer_type = pyrogram_utils.get_peer_type


def patched_get_peer_type(peer_id):
    try:
        return _original_get_peer_type(peer_id)
    except ValueError:
        if peer_id < MIN_CHANNEL_ID:
            return "channel"
        if peer_id < 0:
            return "chat"
        raise


pyrogram_utils.get_peer_type = patched_get_peer_type


# =========================
# خادم Flask لإبقاء الخدمة حية
# =========================

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Audio streaming service is running."


@flask_app.route("/ping")
def ping():
    return "pong"


def run_flask():
    server = make_server("0.0.0.0", PORT, flask_app)
    server.serve_forever()


threading.Thread(
    target=run_flask,
    daemon=True
).start()


# =========================
# إعداد Pyrogram
# =========================

app = Client(
    "audio_stream_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True
)


# =========================
# إعداد البث الصوتي
# =========================

group_call = GroupCallFactory(app).get_group_call()

os.makedirs("downloads", exist_ok=True)

current_chat_id = None
current_input_path = None
current_output_path = None


# =========================
# أدوات مساعدة
# =========================

def convert_audio(input_path, output_path):
    """
    تحويل الملف إلى PCM خام:
    48 kHz، قناتان، signed 16-bit little-endian.
    """

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

    logger.info("بدء تحويل الملف الصوتي: %s", input_path)

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180
    )

    if result.returncode != 0:
        logger.error(
            "FFMPEG_ERROR: %s",
            result.stderr[-3000:]
        )
        raise RuntimeError("فشل تحويل الملف الصوتي.")

    if not os.path.exists(output_path):
        raise RuntimeError("لم يُنشأ الملف الصوتي المحوّل.")

    if os.path.getsize(output_path) == 0:
        raise RuntimeError("الملف الصوتي المحوّل فارغ.")

    logger.info(
        "اكتمل التحويل. حجم الملف: %s بايت",
        os.path.getsize(output_path)
    )


async def cleanup_files():
    global current_input_path, current_output_path

    paths = [current_input_path, current_output_path]

    current_input_path = None
    current_output_path = None

    for path in paths:
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                logger.info("تم حذف الملف المؤقت: %s", path)
            except OSError:
                logger.exception("تعذر حذف الملف: %s", path)


# =========================
# أمر /ping
# =========================

@app.on_message(filters.command("ping"))
async def ping_command(client, message):
    await message.reply_text("البوت يعمل ✅")


# =========================
# أمر /play
# =========================

@app.on_message(filters.command("play") & filters.group)
async def play_command(client, message):
    global current_chat_id
    global current_input_path
    global current_output_path

    if not message.reply_to_message:
        await message.reply_text(
            "ردّي على ملف صوتي أو رسالة صوتية بالأمر /play."
        )
        return

    target_message = message.reply_to_message

    media = (
        target_message.audio
        or target_message.voice
        or target_message.document
    )

    if not media:
        await message.reply_text(
            "الرسالة التي رددتِ عليها لا تحتوي على ملف صوتي."
        )
        return

    status_message = await message.reply_text(
        "⏳ جارٍ تنزيل الملف وتجهيزه..."
    )

    try:
        # إيقاف البث السابق إن كان في مجموعة أخرى.
        if current_chat_id is not None:
            try:
                await group_call.leave()
            except Exception:
                logger.exception("تعذر إنهاء الاتصال السابق.")

            await cleanup_files()
            current_chat_id = None

        # تنزيل الملف.
        downloaded_path = await client.download_media(
            target_message,
            file_name="downloads/"
        )

        if not downloaded_path or not os.path.isfile(downloaded_path):
            raise RuntimeError("تعذر تنزيل الملف من تيليجرام.")

        current_input_path = downloaded_path

        # إنشاء مسار الملف المحوّل.
        base_name = os.path.splitext(
            os.path.basename(downloaded_path)
        )[0]

        current_output_path = os.path.join(
            "downloads",
            base_name + "_converted.raw"
        )

        # التحويل لا يحجب حلقة الأحداث.
        await asyncio.to_thread(
            convert_audio,
            current_input_path,
            current_output_path
        )

        await status_message.edit_text(
            "🔊 تم تجهيز الملف، جارٍ الانضمام إلى المحادثة الصوتية..."
        )

        # الانضمام إلى المحادثة الصوتية.
        await group_call.join(message.chat.id)

        current_chat_id = message.chat.id

        # بدء بث الملف الصوتي.
        # هذا هو أسلوب البث المستخدم مع واجهة pytgcalls dev24.
        await group_call.start_audio(current_output_path)

        logger.info(
            "بدأ بث الصوت في المجموعة %s",
            message.chat.id
        )

        await status_message.edit_text(
            "✅ بدأ بث الملف الصوتي في المحادثة الصوتية."
        )

    except Exception as error:
        logger.exception("حدث خطأ أثناء تشغيل الصوت.")

        await status_message.edit_text(
            "❌ تعذّر تشغيل الملف الصوتي.\n"
            f"الخطأ: {str(error)[:700]}"
        )


# =========================
# أمر /stop
# =========================

@app.on_message(filters.command("stop") & filters.group)
async def stop_command(client, message):
    global current_chat_id

    try:
        if current_chat_id is None:
            await message.reply_text("لا يوجد بث صوتي نشط.")
            return

        if current_chat_id != message.chat.id:
            await message.reply_text(
                "البث الحالي يعمل في مجموعة أخرى."
            )
            return

        try:
            await group_call.stop_audio()
        except Exception:
            logger.exception("تعذر إيقاف الصوت.")

        try:
            await group_call.leave()
        except Exception:
            logger.exception("تعذر مغادرة المحادثة الصوتية.")

        current_chat_id = None

        await cleanup_files()

        await message.reply_text("⏹️ تم إيقاف البث الصوتي.")

    except Exception as error:
        logger.exception("حدث خطأ في أمر الإيقاف.")

        await message.reply_text(
            f"تعذر إيقاف البث: {str(error)[:500]}"
        )


# =========================
# تشغيل البوت
# =========================

async def main():
    logger.info("جارٍ تشغيل Pyrogram...")

    await app.start()

    logger.info("تم تشغيل Pyrogram بنجاح.")

    try:
        await idle()
    finally:
        try:
            await group_call.leave()
        except Exception:
            pass

        await app.stop()


if __name__ == "__main__":
    app.run(main())
