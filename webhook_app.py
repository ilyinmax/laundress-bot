import os
import asyncio
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from database import init_db, add_machine, get_machines_by_type, DBUnavailable
from config import WASHING_MACHINES, DRYERS
from scheduler import setup_scheduler, attach_bot
from handlers.bot_commands import setup_bot_commands

REMINDERS_TASK: asyncio.Task | None = None
WH_RETRY_TASK: asyncio.Task | None = None


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
    if request.path == WEBHOOK_PATH and not request.app["ready"].is_set():
        return web.Response(status=503, text="starting")
    return await handler(request)


session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

# === Подключаем роутеры ===
from handlers.registration import router as registration_router  # noqa: E402
from handlers.booking import router as booking_router  # noqa: E402
from handlers.admin_access import router as admin_access_router, sync_dynamic_admins  # noqa: E402
from handlers.admin_extra import router as admin_extra_router  # noqa: E402
from handlers.admin import router as admin_router  # noqa: E402
from handlers.laundry_features import (  # noqa: E402
    router as laundry_features_router,
    attach_feature_bot,
    init_feature_tables,
    install_feature_hooks,
    rebuild_feature_jobs,
)

# Подменяем только нужные точки старой логики: дневной лимит админов,
# постановку новых карточек-напоминаний и кнопку пользователей в /admin.
install_feature_hooks()

# laundry_features идёт раньше booking_router, чтобы именно на кнопке
# «🧺 Записаться» обновлять username, а затем запускать старый сценарий записи.
dp.include_routers(
    registration_router,
    laundry_features_router,
    booking_router,
    admin_access_router,
    admin_extra_router,
    admin_router,
)


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
            init_feature_tables()
            sync_dynamic_admins()
            return
        except DBUnavailable:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def background_init(app: web.Application):
    try:
        await init_db_with_retries()

        setup_scheduler()
        attach_bot(bot)
        attach_feature_bot(bot)

        try:
            await setup_bot_commands(bot)
        except Exception as exc:
            print(f"⚠️ Не удалось обновить меню команд: {exc}")

        app["ready"].set()
        print("✅ Init: ready")

        global REMINDERS_TASK, WH_RETRY_TASK
        REMINDERS_TASK = asyncio.create_task(rebuild_feature_jobs(hours=48))

        try:
            await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=False, request_timeout=20)
            print(f"✅ Webhook установлен: {WEBHOOK_URL}")
        except Exception as e:
            print(f"⚠️ Не удалось поставить вебхук на старте: {e}. Запускаю ретраи.")
            WH_RETRY_TASK = asyncio.create_task(_retry_set_webhook(bot, WEBHOOK_URL))

    except Exception as e:
        print(f"❌ Ошибка инициализации: {e}")


async def on_startup(app: web.Application):
    app["init_task"] = asyncio.create_task(background_init(app))


async def on_cleanup(app: web.Application):
    t = app.get("init_task")
    if t and not t.done():
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    for task in (WH_RETRY_TASK, REMINDERS_TASK):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        from scheduler import scheduler as _sched  # noqa: E402
        if getattr(_sched, "running", False):
            _sched.shutdown(wait=False)
    except Exception:
        pass

    await bot.session.close()


app = web.Application(middlewares=[readiness_middleware])
app["ready"] = asyncio.Event()

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

app.router.add_get("/health", health)

SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)
