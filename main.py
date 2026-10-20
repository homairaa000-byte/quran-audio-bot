import asyncio
import logging
import os
import threading

from flask import Flask
from pyrogram import Client, filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("test_userbot")

# -------------------------
# Web Server for Render
# -------------------------

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Userbot Running", 200

@web_app.route("/health")
def health():
    return {"status": "ok"}, 200

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

# -------------------------
# Telegram Config
# -------------------------

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

app = Client(
    "test_userbot_v2",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# -------------------------
# Catch Everything
# -------------------------

@app.on_message(filters.all)
async def all_messages(client, message):
    try:
        chat_id = message.chat.id if message.chat else None
        from_id = message.from_user.id if message.from_user else None

        logger.info(
            "UPDATE_RECEIVED | chat=%s | from=%s | text=%s",
            chat_id,
            from_id,
            message.text or message.caption or "<media>"
        )

    except Exception:
        logger.exception("UPDATE_HANDLER_ERROR")

# -------------------------
# Ping Command
# -------------------------

@app.on_message(filters.command("ping"))
async def ping(client, message):
    logger.info("PING_COMMAND_DETECTED")

    try:
        await message.reply_text("pong")
    except Exception:
        logger.exception("PING_REPLY_ERROR")

# -------------------------
# Alive Logs
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

        me = await app.get_me()

        logger.info(
            "CONNECTED | id=%s | username=%s",
            me.id,
            me.username
        )

        logger.info("READY")

        asyncio.create_task(heartbeat())

        await asyncio.Event().wait()

    except Exception:
        logger.exception("FATAL_ERROR")
        raise

if __name__ == "__main__":
    asyncio.run(main())
