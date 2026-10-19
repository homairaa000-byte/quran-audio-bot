import os
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped

# تهيئة حلقة الأحداث
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client("quran_audio_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
call_py = PyTgCalls(app)

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "أهلاً بك في بوت بث صوتيات المقرأة 🎙\n\n"
        "للدخول وبث الصوت في المكالمة الصوتية، قم بالرد على الملف الصوتي بـ `/play`."
    )

@app.on_message(filters.command("play") & (filters.group | filters.channel))
async def play_audio(client, message):
    if message.reply_to_message and (message.reply_to_message.audio or message.reply_to_message.voice):
        msg = await message.reply_text("🔄 جاري تحميل الملف والدخول للمكالمة الصوتية...")

        # تحميل الملف الصوتي
        file_path = await message.reply_to_message.download()

        try:
            # انضمام البوت للمكالمة الصوتية وبث الصوت
            await call_py.join_group_call(
                message.chat.id,
                AudioPiped(file_path)
            )
            await msg.edit_text("▶️ جاري بث الدرس الآن داخل المكالمة الصوتية...")
        except Exception as e:
            await msg.edit_text(f"❌ حدث خطأ أثناء الانضمام للمكالمة: {e}")
    else:
        await message.reply_text("الرجاء الرد على ملف صوتي بالأمر `/play`")

@app.on_message(filters.command("stop") & (filters.group | filters.channel))
async def stop_audio(client, message):
    try:
        await call_py.leave_group_call(message.chat.id)
        await message.reply_text("⏹ تم إيقاف البث ومغادرة المكالمة الصوتية.")
    except Exception as e:
        await message.reply_text(f"❌ خطأ: {e}")

# خادم الويب للحفاظ على نشاط الخدمة على Render
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
    loop.run_until_complete(call_py.start())
    app.run()
