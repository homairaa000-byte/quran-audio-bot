import asyncio
import logging
import os
import threading

from flask import Flask
from pyrogram import Client, filters
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("quran_userbot")

# -------------------------
# Web Server for Render
# -------------------------

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Quran Audio Userbot Running", 200

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
STREAM_URL = "http://stream.radiojar.com/8s44324461uvv"  # رابط إذاعة القرآن الكريم

app = Client(
    "fresh_userbot_v3",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

call_py = PyTgCalls(app)

# -------------------------
# Handlers (Incoming & Outgoing)
# -------------------------

@app.on_message(filters.command("ping") & (filters.me | filters.incoming))
async def ping_handler(client, message):
    logger.info("PING_DETECTED")
    await message.reply_text("pong 🟢 - البوت يعمل ويستجيب بنجاح!")

@app.on_message(filters.command("play") & (filters.me | filters.incoming))
async def play_handler(client, message):
    chat_id = message.chat.id
    logger.info("PLAY_COMMAND_DETECTED | chat=%s", chat_id)
    try:
        await call_py.play(chat_id, AudioPiped(STREAM_URL))
        await message.reply_text("🔊 تم بدء بث إذاعة القرآن الكريم بنجاح!")
    except Exception as e:
        logger.exception("PLAY_ERROR")
        await message.reply_text(f"❌ حدث خطأ أثناء الانضمام: {e}")

@app.on_message(filters.command("stop") & (filters.me | filters.incoming))
async def stop_handler(client, message):
    chat_id = message.chat.id
    try:
        await call_py.leave_call(chat_id)
        await message.reply_text("⏹ تم إيقاف البث والخروج من المكالمة.")
    except Exception as e:
        await message.reply_text(f"❌ خطأ: {e}")

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

        asyncio.create_task(heartbeat())
        await asyncio.Event().wait()

    except Exception:
        logger.exception("FATAL_ERROR")
        raise

if __name__ == "__main__":
    asyncio.run(main())
