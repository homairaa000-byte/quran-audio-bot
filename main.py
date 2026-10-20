import asyncio
import logging
import os
import threading

from flask import Flask
from pyrogram import Client, filters

# =========================
# Logging
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("test_userbot")

# =========================
# Render Web Service
# =========================

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Userbot Running", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )

threading.Thread(
    target=run_web,
    daemon=True
).start()

# =========================
# Telegram
# =========================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

app = Client(
    "test_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# =========================
# Debug all updates
# =========================

@app.on_message(filters.all)
async def all_messages(client, message):
    try:
        logger.info(
            "UPDATE_RECEIVED | chat=%s | from=%s | text=%s",
            message.chat.id if message.chat else None,
            message.from_user.id if message.from_user else None,
            message.text or message.caption or "<media>"
        )
    except Exception:
        logger.exception("UPDATE_ERROR")

# =========================
# Test command
# =========================

@app.on_message(filters.command("ping", prefixes="/"))
async def ping(_, message):
    logger.info("PING_COMMAND_DETECTED")
    await message.reply_text("pong")

# =========================
# Main
# =========================

async def main():
    await app.start()

    me = await app.get_me()

    logger.info(
        "CONNECTED | id=%s | username=%s",
        me.id,
        me.username
    )

    logger.info("READY")

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
