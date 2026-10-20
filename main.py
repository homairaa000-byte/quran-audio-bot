import asyncio
import logging
import os
import threading

from flask import Flask
from pyrogram import Client, filters
from pytgcalls.py_tg_calls import PyTgCalls
from pytgcalls.types.input_stream import AudioPiped

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("quran_lectures_bot")

# -------------------------
# Web Server for Render
# -------------------------

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Quran Lectures Userbot Running", 200

@web_app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port, use_reloader=False)

threading.Thread(target=run_web, daemon=True).start()

# -------------------------
# Telegram & PyTgCalls Config
# -------------------------

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# رابط افتراضي لبث المحاضرات أو دروس المقراة
DEFAULT_AUDIO = "https://server8.mp3quran.net/afs/001.mp3"

app = Client(
    "fresh_userbot_v3",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

call_py = PyTgCalls(app)

# -------------------------
# Handlers with Safe Error Catching
# -------------------------

@app.on_message(filters.command("ping") & (filters.me | filters.incoming))
async def ping_handler(client, message):
    try:
        await message.reply_text("pong 🟢 - بوت بث الدروس والمحاضرات يعمل بنجاح!")
    except Exception:
        pass

@app.on_message(filters.command("play") & (filters.me | filters.incoming))
async def play_lecture_handler(client, message):
    try:
        chat_id = message.chat.id
        args = message.text.split(maxsplit=1)
        audio_source = args[1] if len(args) > 1 else DEFAULT_AUDIO

        logger.info("STARTING_LECTURE | chat=%s | source=%s", chat_id, audio_source)

        await call_py.join_group_call(
            chat_id,
            AudioPiped(audio_source)
        )
        await message.reply_text("🎙️ **تم الانضمام وبدء بث المحاضرة / الدرس في المقرأة بنجاح!**")
    except Exception as e:
        logger.exception("PLAY_LECTURE_ERROR")
        try:
            await message.reply_text(f"❌ تعذر بدء بث الدرس: {e}")
        except Exception:
            pass

@app.on_message(filters.command("stop") & (filters.me | filters.incoming))
async def stop_lecture_handler(client, message):
    try:
        chat_id = message.chat.id
        await call_py.leave_group_call(chat_id)
        await message.reply_text("⏹️ **تم إيقاف بث الدرس والخروج من المكالمة.**")
    except Exception as e:
        try:
            await message.reply_text(f"❌ خطأ عند الخروج: {e}")
        except Exception:
            pass

# -------------------------
# Alive Loop
# -------------------------

async def heartbeat():
    while True:
        logger.info("BOT_ALIVE")
        await asyncio.sleep(60)

# -------------------------
# Main
# -------------------------

async def main():
    try:
        await app.start()
        await call_py.start()

        me = await app.get_me()
        logger.info("CONNECTED | id=%s | username=%s", me.id, me.username)

        # تحديث وتخزين المحادثات مؤقتاً لتجنب أخطاء الـ Peer
        try:
            async for _ in app.get_dialogs():
                pass
        except Exception:
            pass

        asyncio.create_task(heartbeat())
        await asyncio.Event().wait()

    except Exception:
        logger.exception("FATAL_ERROR")
        raise

if __name__ == "__main__":
    asyncio.run(main())
