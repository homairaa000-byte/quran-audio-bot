import asyncio
import os
from threading import Thread
from flask import Flask
from pyrogram import Client, filters
from pytgcalls import GroupCallFactory

# --- سيرفر الويب المجاني لإرضاء منصة Render ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Quran Audio Bot is Running 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

Thread(target=run_web, daemon=True).start()
# ---------------------------------------------

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

STREAM_URL = "http://stream.radiojar.com/8s44324461uvv"

app = Client(
    "quran_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

call_py = GroupCallFactory(app).get_group_call()

@app.on_message(filters.command("play", prefixes="/"))
async def start_stream(client, message):
    chat_id = message.chat.id
    try:
        await message.reply("⏳ جاري الانضمام إلى المكالمة الصوتية وبث القرآن الكريم...")
        await call_py.join_group_call(chat_id, STREAM_URL)
        await message.edit_text("🔊 **تم بدء بث القرآن الكريم بنقاء وجودة عالية!**")
    except Exception as e:
        await message.reply(f"❌ حدث خطأ أثناء التشغيل: `{e}`")

@app.on_message(filters.command("stop", prefixes="/"))
async def stop_stream(client, message):
    chat_id = message.chat.id
    try:
        await call_py.leave_group_call(chat_id)
        await message.reply("⏹ **تم إيقاف البث والخروج من المكالمة.**")
    except Exception as e:
        await message.reply(f"❌ حدث خطأ: `{e}`")

async def main():
    await app.start()
    print("✅ البوت وسيرفر الويب يعملان بنجاح وبدون توقف!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
