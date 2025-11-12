# scheduler.py
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from config import TIMEZONE
from database import cleanup_old_bookings, get_conn

TZ = ZoneInfo(TIMEZONE)

# --- Опциональный SQLAlchemy JobStore (persist) ---
DATABASE_URL = os.getenv("DATABASE_URL")
SQLA_JobStore = None
if DATABASE_URL:
    # SQLAlchemy 2.x требует корректную схему
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
    try:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        SQLA_JobStore = SQLAlchemyJobStore(url=DATABASE_URL)
    except Exception:
        # Тихий фолбэк на in-memory
        pass

jobstores = {"default": SQLA_JobStore} if SQLA_JobStore else None

# --- Запрещаем «догонять» пропущенные напоминания ---
job_defaults = {
    "misfire_grace_time": 1,
    "coalesce": True,
    "max_instances": 1,
}

scheduler = AsyncIOScheduler(timezone=TZ, jobstores=jobstores, job_defaults=job_defaults)

def setup_scheduler():
    """Запуск планировщика + ежедневная очистка."""
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
    """Создаёт задачу на отправку напоминания."""
    try:
        d = datetime.fromisoformat(date_str).date()
    except Exception:
        d = datetime.now(TZ).date()

    slot_dt = datetime.combine(d, time(hour=hour), tzinfo=TZ)
    reminder_dt = slot_dt - timedelta(minutes=minutes_before)
    now = datetime.now(TZ)
    if reminder_dt <= now:
        return  # поздно напоминать — задачу не ставим

    job_id = f"rem_{user_id}_{d.isoformat()}_{hour}"
    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=reminder_dt),
        id=job_id,
        args=[bot, user_id, machine_name, d.isoformat(), hour, minutes_before],
        replace_existing=True,
    )

async def send_reminder(bot, user_id: int, machine_name: str, date_iso: str, hour: int, minutes_before: int):
    """Отправка напоминания. Если момент напоминания уже прошёл — не шлём."""
    now = datetime.now(TZ)
    slot_dt = datetime.combine(datetime.fromisoformat(date_iso).date(), time(hour=hour), tzinfo=TZ)
    reminder_dt = slot_dt - timedelta(minutes=minutes_before)
    if now > reminder_dt:
        return  # уже позже «-30 минут»: ничего не отправляем

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
        # молчим, если не удалось отправить (пользователь мог заблокировать бота и т.п.)
        pass

# --- Восстановление напоминаний после рестарта/пробуждения ---
async def rebuild_reminders_for_horizon(bot, hours: int = 48, minutes_before: int = 30):
    now = datetime.now(TZ)
    end = now + timedelta(hours=hours)

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT b.user_id, m.name, b.date, b.hour
            FROM bookings b
            JOIN machines m ON m.id = b.machine_id
            WHERE (b.date > ? OR (b.date = ? AND b.hour >= ?))
              AND (b.date < ? OR (b.date = ? AND b.hour <= ?))
        """, (
            now.date().isoformat(), now.date().isoformat(), now.hour,
            end.date().isoformat(), end.date().isoformat(), end.hour
        )).fetchall()

    for user_id, machine_name, date_iso, hour in rows:
        await schedule_reminder(bot, user_id, machine_name, date_iso, int(hour), minutes_before)
