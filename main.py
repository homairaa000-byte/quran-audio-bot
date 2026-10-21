import os
import asyncio
import logging
import threading
import subprocess
import time

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
current_converted_path = None

play_lock = asyncio.Lock()
connection_event = asyncio.Event()
connected_chat_id = None


# =====================================
# متابعة حالة الاتصال
# =====================================

@group_call.on_network_status_changed
async def network_status_changed(context, is_connected):
    global connected_chat_id

    try:
        chat_id = int(context.chat_id)
    except (AttributeError, TypeError, ValueError):
        chat_id = None

    if is_connected:
        connected_chat_id = chat_id
        connection_event.set()

        logger.info(
            "تم تأكيد اتصال البث. chat_id=%s",
            chat_id
        )
    else:
        connection_event.clear()
        connected_chat_id = None

        logger.warning(
            "انقطع اتصال البث بالمحادثة الصوتية. chat_id=%s",
            chat_id
        )


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
        logger.error(
            "خطأ FFmpeg: %s",
            result.stderr[-2500:]
        )
        raise RuntimeError("فشل تحويل الملف الصوتي.")

    if not os.path.isfile(output_path):
        raise RuntimeError("لم يُنشأ الملف الصوتي المحوّل.")

    if os.path.getsize(output_path) == 0:
        raise RuntimeError("الملف الصوتي المحوّل فارغ.")

    logger.info(
        "اكتمل تحويل الملف الصوتي بنجاح. الحجم=%s بايت",
        os.path.getsize(output_path)
    )


# =====================================
# حذف الملفات
# =====================================

def delete_file(path):
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            logger.info("حُذف الملف المؤقت: %s", path)
        except OSError:
            logger.exception(
                "تعذر حذف الملف: %s",
                path
            )


async def cleanup():
    global current_input_path
    global current_converted_path

    input_path = current_input_path
    converted_path = current_converted_path

    current_input_path = None
    current_converted_path = None

    if input_path:
        await asyncio.to_thread(delete_file, input_path)

    if converted_path:
        await asyncio.to_thread(delete_file, converted_path)


# =====================================
# إيقاف البث
# =====================================

async def stop_current_broadcast():
    global current_chat_id
    global connected_chat_id

    try:
        await group_call.stop()
    except Exception:
        logger.exception("حدث خطأ أثناء إيقاف الاتصال.")

    current_chat_id = None
    connected_chat_id = None

    connection_event.clear()

    await cleanup()


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
    global current_converted_path

    async with play_lock:
        status = await message.reply_text(
            "⏳ جارٍ تجهيز الملف الصوتي..."
        )

        downloaded_path = None
        converted_path = None
        broadcast_started = False

        try:
            target = message.reply_to_message

            if not target:
                await status.edit_text(
                    "⚠️ ردّي على رسالة تحتوي على ملف صوتي، "
                    "ثم أرسلي /play."
                )
                return

            if not (
                target.audio
                or target.voice
                or (
                    target.document
                    and (
                        target.document.mime_type or ""
                    ).startswith("audio/")
                )
            ):
                await status.edit_text(
                    "⚠️ الرسالة التي رددتِ عليها لا تحتوي "
                    "على ملف صوتي مدعوم."
                )
                return

            # إيقاف أي بث سابق وتنظيف ملفاته.
            if current_chat_id is not None:
                await stop_current_broadcast()

            # تنزيل الملف.
            downloaded_path = await client.download_media(
                target,
                file_name="downloads/"
            )

            if (
                not downloaded_path
                or not os.path.isfile(downloaded_path)
            ):
                raise RuntimeError(
                    "تعذر تنزيل الملف الصوتي من تيليجرام."
                )

            filename = os.path.splitext(
                os.path.basename(downloaded_path)
            )[0]

            # اسم فريد حتى لا تتداخل الملفات.
            converted_path = os.path.join(
                "downloads",
                f"{filename}_{int(time.time())}.raw"
            )

            # تحويل الملف إلى PCM.
            await asyncio.to_thread(
                convert_audio,
                downloaded_path,
                converted_path
            )

            # الاحتفاظ بالملفين إلى حين إيقاف البث.
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

            # تعيين مصدر الصوت قبل طلب الاتصال.
            group_call.input_filename = current_converted_path

            # بدء الاتصال بالمحادثة الصوتية.
            await group_call.start(message.chat.id)

            # لا نعتبر البث ناجحًا إلا عند ورود إشارة اتصال.
            try:
                await asyncio.wait_for(
                    connection_event.wait(),
                    timeout=25
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "لم تصل إشارة تأكيد الاتصال بالمحادثة الصوتية "
                    "خلال 25 ثانية. راجعي سجل Render."
                )

            current_chat_id = message.chat.id
            broadcast_started = True

            await status.edit_text(
                "✅ تم الاتصال بالمحادثة الصوتية.\n"
                "🔊 أُرسل طلب تشغيل الملف الصوتي."
            )

            logger.info(
                "تم تأكيد الاتصال بالمحادثة %s",
                current_chat_id
            )

        except Exception as error:
            logger.exception(
                "فشل بدء البث الصوتي."
            )

            if not broadcast_started:
                await stop_current_broadcast()

            try:
                await status.edit_text(
                    "❌ تعذّر بدء البث الصوتي.\n\n"
                    f"التفاصيل: {str(error)[:600]}"
                )
            except Exception:
                logger.exception(
                    "تعذر تحديث رسالة حالة البث."
                )

        finally:
            # لا تحذفي الملفين إذا كان البث مستمرًا.
            # إذا لم يبدأ البث، احذفي الملفات المحلية المؤقتة.
            if downloaded_path:
                await asyncio.to_thread(
                    delete_file,
                    downloaded_path
                )

            if converted_path:
                await asyncio.to_thread(
                    delete_file,
                    converted_path
                )


# =====================================
# أمر stop
# =====================================

@app.on_message(filters.command("stop") & filters.group)
async def stop_command(client, message):
    async with play_lock:
        if current_chat_id is None:
            await message.reply_text(
                "ℹ️ لا يوجد بث نشط حاليًا."
            )
            return

        if current_chat_id != message.chat.id:
            await message.reply_text(
                "⚠️ البث النشط موجود في مجموعة أخرى."
            )
            return

        try:
            await stop_current_broadcast()

            await message.reply_text(
                "⏹️ تم إيقاف البث الصوتي وتنظيف الملفات المؤقتة."
            )

        except Exception as error:
            logger.exception(
                "حدث خطأ أثناء إيقاف البث."
            )

            await message.reply_text(
                "❌ تعذر إيقاف البث.\n"
                f"{str(error)[:400]}"
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
            await stop_current_broadcast()
        except Exception:
            logger.exception(
                "حدث خطأ أثناء تنظيف البث عند الإغلاق."
            )

        await app.stop()


if __name__ == "__main__":
    app.run(main())
