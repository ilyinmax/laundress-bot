import asyncio
from aiogram import Bot, Dispatcher

from config import BOT_TOKEN, WASHING_MACHINES, DRYERS
from database import init_db, add_machine, get_machines_by_type
from scheduler import setup_scheduler, attach_bot

from handlers import registration, booking, admin_access, admin_extra, admin
from handlers.bot_commands import setup_bot_commands
from handlers.laundry_features import (
    router as laundry_features_router,
    attach_feature_bot,
    init_feature_tables,
    install_feature_hooks,
    rebuild_feature_jobs,
)


async def main():
    init_db()
    init_feature_tables()
    admin_access.sync_dynamic_admins()
    install_feature_hooks()

    if not get_machines_by_type("wash"):
        for w in WASHING_MACHINES:
            add_machine("wash", w)
    if not get_machines_by_type("dry"):
        for d in DRYERS:
            add_machine("dry", d)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(registration.router)
    dp.include_router(laundry_features_router)
    dp.include_router(booking.router)
    dp.include_router(admin_access.router)
    dp.include_router(admin_extra.router)
    dp.include_router(admin.router)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await setup_bot_commands(bot)
    except Exception as exc:
        print(f"⚠️ Не удалось обновить меню команд: {exc}")

    setup_scheduler()
    attach_bot(bot)
    attach_feature_bot(bot)
    await rebuild_feature_jobs(hours=48)

    print("Бот запущен 🚀")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        print("⛔️ Бот остановлен вручную.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
