import os
import asyncio
import logging
import threading
import subprocess

from flask import Flask
from werkzeug.serving import make_server

from pyrogram import Client, filters, idle
from pytgcalls import GroupCallFactory


# =====================================
# التسجيل
# =====================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# =====================================
# إعدادات Render
# =====================================

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
PORT = int(os.getenv("PORT", "10000"))

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError(
        "تأكدي من إضافة API_ID وAPI_HASH وSESSION_STRING في Render."
    )

API_ID = int(API_ID)


# =====================================
# خادم الويب
# =====================================

web = Flask(__name__)


@web.route("/")
def home():
    return "Quran Audio Bot is running."


@web.route("/ping")
def ping():
    return "pong"


def run_web():
    server = make_server("0.0.0.0", PORT, web)
    server.serve_forever()


threading.Thread(
    target=run_web,
    daemon=True
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
play_lock = asyncio.Lock()


# =====================================
# تحويل الملف الصوتي
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

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180
    )

    if result.returncode != 0:
        logger.error("FFmpeg: %s", result.stderr[-2500:])
        raise RuntimeError("فشل تحويل الملف الصوتي.")

    if not os.path.exists(output_path):
        raise RuntimeError("لم يُنشأ الملف الصوتي المحوّل.")

    if os.path.getsize(output_path) == 0:
        raise RuntimeError("الملف الصوتي المحوّل فارغ.")

    logger.info("اكتمل تحويل الملف الصوتي بنجاح.")


# =====================================
# حذف الملفات المؤقتة
# =====================================

def delete_file(path):
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            logger.exception("تعذر حذف الملف: %s", path)


async def cleanup():
    global current_input_path

    path = current_input_path
    current_input_path = None

    if path:
        await asyncio.to_thread(delete_file, path)


# =====================================
# أمر ping
# =====================================

@app.on_message(filters.command("ping"))
async def ping_command(client, message):
    await message.reply_text("البوت يعمل ✅")


# =====================================
# أمر play
# =====================================

@app.on_message(filters.command("play") & filters.group)
async def play_command(client, message):
    global current_chat_id
    global current_input_path

    async with play_lock:
        status = await message.reply_text(
            "⏳ جارٍ تجهيز الملف الصوتي..."
        )

        downloaded_path = None
        converted_path = None

        try:
            target = message.reply_to_message

            if not target:
                await status.edit_text(
                    "ردّي على ملف صوتي أو رسالة صوتية بالأمر /play."
                )
                return

            if not (target.audio or target.voice or target.document):
                await status.edit_text(
                    "الرسالة التي رددتِ عليها لا تحتوي على ملف صوتي."
                )
                return

            # إيقاف البث السابق عند الحاجة.
            if current_chat_id is not None:
                try:
                    await group_call.stop()
                except Exception:
                    logger.exception("تعذر إيقاف البث السابق.")

                current_chat_id = None
                await cleanup()

            # تنزيل الملف.
            downloaded_path = await client.download_media(
                target,
                file_name="downloads/"
            )

            if not downloaded_path or not os.path.isfile(downloaded_path):
                raise RuntimeError("تعذر تنزيل الملف الصوتي.")

            # تجهيز مسار التحويل.
            filename = os.path.splitext(
                os.path.basename(downloaded_path)
            )[0]

            converted_path = os.path.join(
                "downloads",
                filename + "_converted.raw"
            )

            await asyncio.to_thread(
                convert_audio,
                downloaded_path,
                converted_path
            )

            await status.edit_text(
                "🔊 تم تجهيز الملف. جارٍ بدء البث..."
            )

            # تعيين ملف الإدخال قبل بدء الاتصال.
            group_call.input_filename = converted_path

            # start() يتولى بدء الاتصال بالمحادثة الصوتية.
            await group_call.start(message.chat.id)

            current_chat_id = message.chat.id
            current_input_path = downloaded_path

            # لا نحذف الملف المحوّل أثناء استمرار البث.
            downloaded_path = None

            await status.edit_text(
                "✅ أُرسل طلب بدء البث الصوتي."
            )

            logger.info(
                "تم بدء طلب البث في المجموعة %s",
                current_chat_id
            )

        except Exception as error:
            logger.exception("خطأ أثناء تشغيل الصوت.")

            try:
                await status.edit_text(
                    "❌ تعذّر تشغيل الملف الصوتي.\n"
                    f"الخطأ: {str(error)[:500]}"
                )
            except Exception:
                pass

            try:
                await group_call.stop()
            except Exception:
                pass

            current_chat_id = None
            await cleanup()

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

    async with play_lock:
        if current_chat_id is None:
            await message.reply_text("لا يوجد بث نشط حاليًا.")
            return

        if current_chat_id != message.chat.id:
            await message.reply_text(
                "البث النشط موجود في مجموعة أخرى."
            )
            return

        try:
            await group_call.stop()
            current_chat_id = None

            await cleanup()

            await message.reply_text("⏹️ تم إيقاف البث الصوتي.")

        except Exception as error:
            logger.exception("خطأ أثناء إيقاف البث.")

            await message.reply_text(
                f"تعذر إيقاف البث: {str(error)[:400]}"
            )


# =====================================
# التشغيل
# =====================================

async def main():
    logger.info("جارٍ تشغيل البوت...")

    await app.start()

    logger.info("تم الاتصال بتيليجرام بنجاح.")

    try:
        await idle()
    finally:
        try:
            await group_call.stop()
        except Exception:
            pass

        await app.stop()


if __name__ == "__main__":
    app.run(main())
