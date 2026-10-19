import os
import asyncio
from pyrogram import Client, filters
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped

# جلب البيانات من متغيرات البيئة
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "") # جلسة حساب مساعد للانضمام للمحادثة الصوتية

app = Client("audio_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_app = Client("user_helper", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
call_py = PyTgCalls(user_app)

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "أهلاً بك في بوت بث الدروس والصوتيات المباشرة 🎙\n\n"
        "أرسل الأوامر التالية:\n"
        "`/play رابط_الصوت` أو ارفع ملف صوتي مع الأمر `/play` لبدء البث في المحادثة الصوتية.\n"
        "`/stop` لإيقاف البث."
    )

@app.on_message(filters.command("play") & (filters.group | filters.channel))
async def play_audio(client, message):
    chat_id = message.chat.id
    
    # التحقق من وجود رابط أو ملف صوتي مرفق
    audio_url = None
    if message.reply_to_message and message.reply_to_message.audio:
        audio_file = await message.reply_to_message.download()
        audio_url = audio_file
    elif len(message.command) > 1:
        audio_url = message.command[1]
    else:
        await message.reply_text("الرجاء الرد على ملف صوتي أو وضع رابط الصوت بعد الأمر `/play`")
        return

    msg = await message.reply_text("🔄 جاري تشغيل الدرس في المحادثة الصوتية...")
    
    try:
        await call_py.join_group_call(
            chat_id,
            AudioPiped(audio_url)
        )
        await msg.edit_text("▶️ بدأ بث الدرس الآن بنجاح وكأنه محاضرة مباشرة!")
    except Exception as e:
        await msg.edit_text(f"حدث خطأ أثناء التشغيل: {e}")

@app.on_message(filters.command("stop") & (filters.group | filters.channel))
async me_stop(client, message):
    chat_id = message.chat.id
    try:
        await call_py.leave_group_call(chat_id)
        await message.reply_text("⏹ تم إيقاف البث المباشر.")
    except Exception as e:
        await message.reply_text(f"خطأ: {e}")

async def main():
    await app.start()
    await user_app.start()
    await call_py.start()
    print("البوت يعمل الآن...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
