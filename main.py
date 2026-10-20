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
    "fresh_userbot_v3",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

# -------------------------
# Listen to ALL updates (Chats + Channels)
# -------------------------

@app.on_message()
@app.on_channel_post()
async def handle_all_updates(client, message):
    try:
        text = message.text or message.caption or ""
        chat_id = message.chat.id if message.chat else None
        
        logger.info(
            "UPDATE_RECEIVED | chat_id=%s | text=%s",
            chat_id,
            text
        )

        # استجابة لأمر /ping
        if text.strip().startswith("/ping"):
            logger.info("PING_COMMAND_TRIGGERED")
            await message.reply_text("pong 🟢 - البوت متصل ويستجيب بنجاح!")

    except Exception:
        logger.exception("UPDATE_HANDLER_ERROR")

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
