# webhook_app.py — aiogram v3 + aiohttp, Web Service на Render
import os
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from database import init_db
from config import WASHING_MACHINES, DRYERS
from database import add_machine, get_machines_by_type

def ensure_config_machines():
    # добавим стиралки, если их ещё нет
    if not get_machines_by_type("wash"):
        for name in WASHING_MACHINES:
            add_machine("wash", name)
    # добавим сушилки, если их ещё нет
    if not get_machines_by_type("dry"):
        for name in DRYERS:
            add_machine("dry", name)


# === ENV ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

# Render обычно сам проставляет RENDER_EXTERNAL_URL,
# если нет — задай WEBHOOK_BASE_URL вручную в переменных окружения.
BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL")
if not BASE_URL:
    raise RuntimeError("Не задан BASE_URL (RENDER_EXTERNAL_URL или WEBHOOK_BASE_URL)")

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# === Telegram client с таймаутами ===
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

# === Подключаем твои роутеры ===
from handlers.registration import router as registration_router
from handlers.booking import router as booking_router
from handlers.admin import router as admin_router
dp.include_routers(registration_router, booking_router, admin_router)

# === /health для Render и пингов ===
async def health(_):
    return web.json_response({"ok": True})

# === on_startup: ставим вебхук ===
async def on_startup(app: web.Application):
    init_db()
    ensure_config_machines()
    # сбрасываем хвост обновлений и ставим вебхук на наш публичный URL
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    print(f"🌍 External URL: {BASE_URL}")
    print(f"✅ Webhook установлен: {WEBHOOK_URL}")

# === on_cleanup ===
async def on_cleanup(app: web.Application):
    await bot.session.close()

# === aiohttp-приложение ===
app = web.Application()
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

# маршруты
app.router.add_get("/health", health)
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)  # корректное завершение

if __name__ == "__main__":
    # ОБЯЗАТЕЛЬНО слушаем порт от Render
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)
