import os
import asyncio
from aiohttp import web

# تهيئة حلقة الأحداث لبايثون الحديث
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client("quran_audio_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "أهلاً بك في بوت بث صوتيات المقرأة 🎙\n\n"
        "أرسل أو رد على أي ملف صوتي بـ `/play` لنشره في الدردشة."
    )

@app.on_message(filters.command("play") & (filters.group | filters.channel))
async def play_audio(client, message):
    if message.reply_to_message and (message.reply_to_message.audio or message.reply_to_message.voice):
        msg = await message.reply_text("🔄 جاري نشر وبث الدرس...")
        await message.reply_to_message.copy(message.chat.id)
        await msg.delete()
    else:
        await message.reply_text("الرجاء الرد على ملف صوتي أو تسجيل بالأمر `/play`")

# خادم ويب وهمي لإبقاء الخدمة مفعلة على Render بدون أخطاء
async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    server = web.Application()
    server.add_routes([web.get("/", handle)])
    runner = web.AppRunner(server)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    print("البوت يعمل الآن...")
    app.run()
