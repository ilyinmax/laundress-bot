import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.session.aiohttp import AiohttpSession

from handlers.booking import router as booking_router
from handlers.registration import router as registration_router
from handlers.admin import router as admin_router


# --- Токен и URL ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
#BOT_TOKEN = "8300246721:AAHp6A7VIuqwmOfuHkDyLz5yek9FvIiIgbM"
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render сам задаёт этот URL
#assert BOT_TOKEN, "❌ BOT_TOKEN не задан"

WEBHOOK_PATH = "/webhook"                    # путь на нашем сервисе
WEBHOOK_URL  = f"{BASE_URL}{WEBHOOK_PATH}"   # полный URL, который увидит Telegram


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_routers(booking_router, registration_router, admin_router)

# === health и статусы ===
async def health(_):
    return web.json_response({"ok": True})

# --- Webhook сервер ---
async def on_startup(app: web.Application):
    await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    print(f"✅ Webhook установлен: {WEBHOOK_URL}/webhook")

async def on_cleanup(app: web.Application):
    await bot.session.close()

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()

app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
