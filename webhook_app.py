# webhook_app.py — aiogram v3 + aiohttp, Web Service на Render
import os
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from database import init_db, add_machine, get_machines_by_type
from config import WASHING_MACHINES, DRYERS
from scheduler import setup_scheduler, rebuild_reminders_for_horizon, attach_bot  # важно

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

async def _retry_set_webhook(bot: Bot, url: str):
    for delay in (5, 10, 20, 40):
        try:
            await asyncio.sleep(delay)
            await bot.set_webhook(url, drop_pending_updates=False, request_timeout=20)
            print(f"✅ Webhook установлен (retry): {url}")
            return
        except Exception as e:
            print(f"⚠️ Повторная попытка через {delay}s не удалась: {e}")
    print("❗ Не удалось установить вебхук после нескольких попыток.")

# === on_startup: ставим вебхук ===
async def on_startup(app: web.Application):
    init_db()
    ensure_config_machines()
    setup_scheduler()
    attach_bot(bot)  # ← привязали единственный Bot для задач
    #await rebuild_reminders_for_horizon(hours=48, minutes_before=30)
    # сбрасываем хвост обновлений и ставим вебхук на наш публичный URL
    #await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

    await rebuild_reminders_for_horizon(hours=48, minutes_before=30)

    # Ставим вебхук с коротким таймаутом; при неудаче ретраим в фоне,
    # чтобы не заваливать старт приложения и не блокировать порт.

    try:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True, request_timeout=20)
        print(f"✅ Webhook установлен: {WEBHOOK_URL}")
    except Exception as e:
        print(f"⚠️ Не удалось поставить вебхук на старте: {e}. Запускаю ретраии в фоне.")
        app['wh_retry_task'] = asyncio.create_task(_retry_set_webhook(bot, WEBHOOK_URL))

   # print(f"🌍 External URL: {BASE_URL}")
   # print(f"✅ Webhook установлен: {WEBHOOK_URL}")

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
