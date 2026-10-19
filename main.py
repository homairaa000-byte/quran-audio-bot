import os
import asyncio
from pyrogram import Client, filters

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client("quran_audio_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "أهلاً بك في بوت بث صوتيات المقرأة 🎙\n\n"
        "أرسل أو رد على أي ملف صوتي أو درس بـ `/play` ليقوم البوت بنشره في القناة أو المجموعة وكأنه محاضرة مباشرة."
    )

@app.on_message(filters.command("play") & (filters.group | filters.channel))
async def play_audio(client, message):
    if message.reply_to_message and (message.reply_to_message.audio or message.reply_to_message.voice):
        msg = await message.reply_text("🔄 جاري نشر وبث الدرس في المحادثة...")
        # إعادة توجيه أو إرسال الصوت للبث المباشر في القناة
        await message.reply_to_message.copy(message.chat.id)
        await msg.delete()
    else:
        await message.reply_text("الرجاء الرد على ملف صوتي أو تسجيل للدرس بالأمر `/play`")

if __name__ == "__main__":
    print("البوت يعمل الآن...")
    app.run()
