import asyncio
from aiogram import Bot, Dispatcher

from config import BOT_TOKEN, WASHING_MACHINES, DRYERS
from database import init_db, add_machine, get_machines_by_type
from scheduler import setup_scheduler, schedule_reminder

from handlers import registration, booking, admin_access, admin_extra, admin
from handlers.bot_commands import setup_bot_commands
from database import init_db
init_db()


async def main():
    init_db()
    # добавляем машины, если ещё нет
    if not get_machines_by_type("wash"):
        for w in WASHING_MACHINES:
            add_machine("wash", w)
    if not get_machines_by_type("dry"):
        for d in DRYERS:
            add_machine("dry", d)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(registration.router)
    dp.include_router(booking.router)
    # Управление админами подключаем явно, без побочных эффектов импорта.
    dp.include_router(admin_access.router)
    # Новая админ-панель должна идти раньше старого admin.py,
    # чтобы перехватить /admin и добавить раннюю запись, не ломая старые callback'и.
    dp.include_router(admin_extra.router)
    dp.include_router(admin.router)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await setup_bot_commands(bot)
    except Exception as exc:
        print(f"⚠️ Не удалось обновить меню команд: {exc}")

    setup_scheduler()
    print("Бот запущен 🚀")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        print("⛔️ Бот остановлен вручную.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
