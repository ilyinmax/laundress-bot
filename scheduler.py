from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
from database import cleanup_old_bookings

scheduler = AsyncIOScheduler()

def setup_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            cleanup_old_bookings,
            "cron",
            hour=0, minute=0,
            id="cleanup_daily", replace_existing=True
        )
        scheduler.start()
    return scheduler

async def schedule_reminder(bot, user_id, machine_name, date_str, hour):
    """
    Создаёт задачу на отправку напоминания за 1 час до начала записи.
    """
    date_obj = datetime.fromisoformat(date_str)
    reminder_time = datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=hour - 1)

    # Если время уже прошло — не создаём напоминание
    if reminder_time < datetime.now():
        return

    trigger = DateTrigger(run_date=reminder_time)
    scheduler.add_job(
        send_reminder,
        trigger=trigger,
        args=[bot, user_id, machine_name, date_str, hour],
        id=f"reminder_{user_id}_{date_str}_{hour}",
        replace_existing=True,
    )

async def send_reminder(bot, user_id, machine_name, date_str, hour):
    """
    Отправка напоминания пользователю.
    """
    try:
        msg = (
            f"⏰ <b>Напоминание</b>\n\n"
            f"Вы записаны на <b>{machine_name}</b>\n"
            f"📅 {date_str}, ⏰ {hour}:00\n\n"
            f"Не забудьте вовремя прийти 🧺"
        )
        await bot.send_message(user_id, msg, parse_mode="HTML")
    except Exception as e:
        print(f"[!] Ошибка отправки напоминания пользователю {user_id}: {e}")
