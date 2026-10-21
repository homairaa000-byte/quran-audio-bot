import logging
import os
import subprocess
import threading
import inspect

from flask import Flask
from werkzeug.serving import make_server

from pyrogram import Client, filters
from pyrogram import utils as pyrogram_utils
from pytgcalls import GroupCallFactory


# =========================================================
# 1. إعداد السجلات
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("quran_lectures_bot")


# =========================================================
# 2. إعداد معرّفات تيليجرام
# =========================================================

_original_get_peer_type = pyrogram_utils.get_peer_type


def fixed_get_peer_type(peer_id):
    if (
        isinstance(peer_id, int)
        and peer_id < pyrogram_utils.MAX_CHANNEL_ID
        and peer_id >= -1007852516352
    ):
        return "channel"

    return _original_get_peer_type(peer_id)


pyrogram_utils.MIN_CHANNEL_ID = -1007852516352
pyrogram_utils.MIN_CHAT_ID = -999999999999
pyrogram_utils.get_peer_type = fixed_get_peer_type


# =========================================================
# 3. خادم الويب لـ Render
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Quran Lectures Userbot Running", 200


@web_app.route("/health")
def health():
    return {"status": "ok", "service": "quran_lectures_bot"}, 200


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    server = make_server("0.0.0.0", port, web_app, threaded=True)
    logger.info("WEB_SERVER_STARTED | port=%s", port)
    server.serve_forever()


web_thread = threading.Thread(target=run_web, daemon=True)
web_thread.start()


# =========================================================
# 4. إعداد حساب تيليجرام
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

app = Client(
    "quran_lectures_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

group_call = GroupCallFactory(app).get_group_call()


# تسجيل معلومات واجهة مكتبة البث

logger.info("GROUP_CALL_TYPE: %s", type(group_call))

logger.info(
    "GROUP_CALL_METHODS: %s",
    [
        name for name in dir(group_call)
        if any(word in name.lower() for word in (
            "join", "leave", "play", "input", "pause", "stop"
        ))
    ],
)

try:
    logger.info(
        "GROUP_CALL_JOIN_SIGNATURE: %s",
        inspect.signature(group_call.join),
    )
except Exception:
    logger.exception("JOIN_SIGNATURE_ERROR")


# =========================================================
# 5. أمر الفحص
# =========================================================

@app.on_message(filters.command("ping") & (filters.me | filters.incoming))
async def ping_handler(client, message):
    await message.reply_text(
        "🟢 بوت مقرأة القرآن الكريم يعمل!"
    )


# =========================================================
# 6. أمر البث
# =========================================================

@app.on_message(filters.command("play") & (filters.me | filters.incoming))
async def play_handler(client, message):
    input_path = None
    output_path = None

    try:
        target_message = message.reply_to_message

        if not target_message:
            await message.reply_text(
                "⚠️ ردي على الرسالة الصوتية بالأمر /play."
            )
            return

        audio_file = (
            target_message.audio
            or target_message.voice
            or (
                target_message.document
                if target_message.document
                and target_message.document.mime_type
                and target_message.document.mime_type.startswith("audio/")
                else None
            )
        )

        if not audio_file:
            await message.reply_text(
                "⚠️ يجب الرد على رسالة صوتية أو ملف صوتي بالأمر /play."
            )
            return

        await message.reply_text("📥 جارٍ تحميل الملف الصوتي...")

        input_path = await client.download_media(
            target_message,
            file_name="downloads/"
        )

        if not input_path or not os.path.isfile(input_path):
            await message.reply_text("❌ تعذر تحميل الملف الصوتي.")
            return

        output_path = os.path.splitext(input_path)[0] + "_converted.raw"

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i", input_path,
            "-vn",
            "-ac", "2",
            "-ar", "48000",
            "-f", "s16le",
            output_path,
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=
