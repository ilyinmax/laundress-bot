import asyncio
from aiogram import Bot, Dispatcher

from config import BOT_TOKEN, WASHING_MACHINES, DRYERS
from database import init_db, add_machine, get_machines_by_type
from scheduler import setup_scheduler, schedule_reminder

from handlers import registration, booking, admin


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
    dp.include_router(admin.router)

    await bot.delete_webhook(drop_pending_updates=True)

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
