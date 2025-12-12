# webhook_app.py
import os
import asyncio
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from database import init_db, add_machine, get_machines_by_type, DBUnavailable
from config import WASHING_MACHINES, DRYERS
from scheduler import setup_scheduler, rebuild_reminders_for_horizon, attach_bot

'''
def ensure_config_machines():
    # добавим стиралки, если их ещё нет
    if not get_machines_by_type("wash"):
        for name in WASHING_MACHINES:
            add_machine("wash", name)
    # добавим сушилки, если их ещё нет
    if not get_machines_by_type("dry"):
        for name in DRYERS:
            add_machine("dry", name)
'''
def ensure_config_machines():
    for name in WASHING_MACHINES:
        add_machine("wash", name)
    for name in DRYERS:
        add_machine("dry", name)


# === ENV ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

BASE_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_BASE_URL") or "").rstrip("/")
if not BASE_URL:
    raise RuntimeError("Не задан BASE_URL (RENDER_EXTERNAL_URL или WEBHOOK_BASE_URL)")

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"


@web.middleware
async def readiness_middleware(request: web.Request, handler):
    # Пока не готовы — НЕ принимаем апдейты (Telegram будет ретраить)
    if request.path == WEBHOOK_PATH and not request.app["ready"].is_set():
        return web.Response(status=503, text="starting")
    return await handler(request)


# === Telegram client с таймаутами ===
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

# === Подключаем твои роутеры ===
from handlers.registration import router as registration_router  # noqa: E402
from handlers.booking import router as booking_router  # noqa: E402
from handlers.admin import router as admin_router  # noqa: E402

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

async def init_db_with_retries():
    delay = 1
    while True:
        try:
            init_db()
            ensure_config_machines()
            return
        except DBUnavailable as e:
            # print(f"⏳ DB недоступна (Neon sleep): {e}. Повтор через {delay}s…")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)  # 1s → 2s → 4s → … → 60s


# === Фоновая инициализация бота ===
async def background_init(app: web.Application):
    try:
        # КРИТИЧНЫЙ МИНИМУМ: должен завершиться до приёма апдейтов
        '''
        init_db()
        ensure_config_machines()
        setup_scheduler()
        attach_bot(bot)
        '''
        await init_db_with_retries()

        setup_scheduler()
        attach_bot(bot)

        # Теперь можно принимать апдейты: таблицы/машины/планировщик готовы
        app["ready"].set()
        print("✅ Init: ready")

        # НЕ критично: восстанавливаем напоминания отдельной задачей
        app["reminders_task"] = asyncio.create_task(
            rebuild_reminders_for_horizon(hours=48, minutes_before=30)
        )

        # Ставим вебхук
        try:
            await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=False, request_timeout=20)
            print(f"✅ Webhook установлен: {WEBHOOK_URL}")
        except Exception as e:
            print(f"⚠️ Не удалось поставить вебхук на старте: {e}. Запускаю ретраи.")
            app["wh_retry_task"] = asyncio.create_task(_retry_set_webhook(bot, WEBHOOK_URL))

    except Exception as e:
        # ready НЕ ставим → /webhook будет отдавать 503, Telegram будет ретраить
        print(f"❌ Ошибка инициализации: {e}")


# === on_startup / on_cleanup ===
async def on_startup(app: web.Application):
    # Стартуем инициализацию в фоне, но апдейты не примем, пока app["ready"] не set()
    app["init_task"] = asyncio.create_task(background_init(app))


async def on_cleanup(app: web.Application):
    # Гасим фоновые задачи
    for key in ("wh_retry_task", "reminders_task", "init_task"):
        task = app.get(key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # (опционально) гасим APScheduler, если он был запущен
    try:
        from scheduler import scheduler as _sched  # noqa: E402
        if getattr(_sched, "running", False):
            _sched.shutdown(wait=False)
    except Exception:
        pass

    # Закрываем сессию бота
    await bot.session.close()


# === aiohttp-приложение ===
app = web.Application(middlewares=[readiness_middleware])
app["ready"] = asyncio.Event()

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

# маршруты
app.router.add_get("/health", health)

# вебхук
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)  # корректное завершение

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)
