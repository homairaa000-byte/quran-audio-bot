import asyncio
import os
from pyrogram import Client, filters
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped, HighQualityAudio
from pytgcalls.types.stream import StreamAudioEnded

# قراءة المتغيرات من إعدادات Render
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

# رابط بث القرآن الكريم (صوت نقاح وعالي الجودة)
STREAM_URL = "http://stream.radiojar.com/8s44324461uvv"

# إعداد عميل تيليجرام (Userbot)
app = Client(
    "quran_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# تشغيل مكالمات تيليجرام
call_py = PyTgCalls(app)

@call_py.on_stream_end()
async def stream_end_handler(client, update):
    # إعادة تشغيل البث تلقائياً عند انتهائه ليعمل بشكل دائم
    if isinstance(update, StreamAudioEnded):
        chat_id = update.chat_id
        await call_py.play(
            chat_id,
            AudioPiped(
                STREAM_URL,
                audio_parameters=HighQualityAudio()
            )
        )

@app.on_message(filters.command("play", prefixes="/") & filters.me)
async def start_stream(client, message):
    chat_id = message.chat.id
    try:
        await call_py.play(
            chat_id,
            AudioPiped(
                STREAM_URL,
                audio_parameters=HighQualityAudio()
            )
        )
        await message.edit_text("🔊 **تم بدء بث القرآن الكريم بنقاء وجودة عالية!**")
    except Exception as e:
        await message.edit_text(f"❌ حدث خطأ أثناء التشغيل: `{e}`")

@app.on_message(filters.command("stop", prefixes="/") & filters.me)
async def stop_stream(client, message):
    chat_id = message.chat.id
    try:
        await call_py.leave_call(chat_id)
        await message.edit_text("⏹ **تم إيقاف البث والخروج من المكالمة.**")
    except Exception as e:
        await message.edit_text(f"❌ حدث خطأ: `{e}`")

async def main():
    await app.start()
    await call_py.start()
    print("✅ البوت يعمل بنجاح وجاهز لبث الصوت!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
