# scheduler.py — с Postgres JobStore (напоминания переживают перезапуск)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from config import TIMEZONE
from database import cleanup_old_bookings
import os

TZ = ZoneInfo(TIMEZONE)

# --- Подключаем PostgreSQL JobStore ---
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    jobstores = {"default": SQLAlchemyJobStore(url=DATABASE_URL)}
else:
    jobstores = None  # если БД нет, APScheduler просто будет в памяти

scheduler = AsyncIOScheduler(timezone=TZ, jobstores=jobstores)


def setup_scheduler():
    """Запуск планировщика с ежедневной очисткой."""
    if not scheduler.running:
        scheduler.add_job(
            cleanup_old_bookings,
            trigger="cron",
            hour=0, minute=0,
            id="cleanup_daily",
            replace_existing=True,
        )
        scheduler.start()
    return scheduler


async def schedule_reminder(bot, user_id: int, machine_name: str, date_str: str, hour: int, minutes_before: int = 30):
    """Создаёт задачу на отправку напоминания за minutes_before минут до слота."""
    try:
        d = datetime.fromisoformat(date_str).date()
    except Exception:
        d = date_str if hasattr(date_str, "year") else datetime.now(TZ).date()

    slot_dt = datetime.combine(d, time(hour=hour), tzinfo=TZ)
    reminder_dt = slot_dt - timedelta(minutes=minutes_before)
    if reminder_dt <= datetime.now(TZ):
        return

    job_id = f"rem_{user_id}_{d.isoformat()}_{hour}"

    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=reminder_dt),
        id=job_id,
        args=[bot, user_id, machine_name, d.isoformat(), hour, minutes_before],
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )


async def send_reminder(bot, user_id: int, machine_name: str, date_iso: str, hour: int, minutes_before: int):
    """Отправка напоминания пользователю."""
    text = (
        "⏰ <b>Напоминание</b>\n\n"
        f"Через <b>{minutes_before} мин</b> у вас стирка.\n"
        f"🧺 Машина: <b>{machine_name}</b>\n"
        f"📅 Дата: {date_iso}\n"
        f"🕒 Время: {hour:02d}:00"
    )
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception:
        pass
