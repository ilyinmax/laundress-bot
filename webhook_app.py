import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from handlers.booking import router as booking_router
from handlers.registration import router as registration_router
from handlers.admin import router as admin_router


# --- Токен и URL ---
#BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_TOKEN = os.getenv("8300246721:AAHp6A7VIuqwmOfuHkDyLz5yek9FvIiIgbM")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render сам задаёт этот URL
#assert BOT_TOKEN, "❌ BOT_TOKEN не задан"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_routers(booking_router, registration_router, admin_router)

# --- Webhook сервер ---
async def on_startup(app: web.Application):
    await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    print(f"✅ Webhook установлен: {WEBHOOK_URL}/webhook")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()

app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
setup_application(app, dp, bot=bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
