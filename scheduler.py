# scheduler.py
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from config import TIMEZONE
from database import (
    cleanup_old_bookings,
    get_conn,
    get_machine_id_by_name,
    was_reminder_sent,
    mark_reminder_sent,
)

from aiogram import Bot

BOT_REF: Bot | None = None


def attach_bot(bot: Bot):
    """Сохраняем ссылку на Bot для задач APScheduler (без пиклинга объекта)."""
    global BOT_REF
    BOT_REF = bot


TZ = ZoneInfo(TIMEZONE)
LATE_WINDOW_SEC = 300  # окно опоздания для напоминания (секунд)

# --- Опциональный SQLAlchemy JobStore (persist) ---
DATABASE_URL = os.getenv("DATABASE_URL")
SQLA_JobStore = None
if DATABASE_URL:
    # SQLAlchemy 2.x требует корректную схему
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace(
            "postgres://", "postgresql+psycopg2://", 1
        )
    try:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

        SQLA_JobStore = SQLAlchemyJobStore(url=DATABASE_URL)
    except Exception:
        # Тихий фолбэк на in-memory
        SQLA_JobStore = None

jobstores = {"default": SQLA_JobStore} if SQLA_JobStore else None

# --- Запрещаем «догонять» пропущенные напоминания слишком поздно ---
job_defaults = {
    "misfire_grace_time": 1,
    "coalesce": True,
    "max_instances": 1,
}

scheduler = AsyncIOScheduler(
    timezone=TZ,
    jobstores=jobstores,
    job_defaults=job_defaults,
)


def setup_scheduler():
    if not scheduler.running:
        # ежедневная очистка старых бронирований
        scheduler.add_job(
            cleanup_old_bookings,
            trigger="cron",
            hour=0,
            minute=0,
            id="cleanup_daily",
            replace_existing=True,
        )
        # сторож: каждую минуту проверяем, не пришло ли время напоминания
        scheduler.add_job(
            watchdog_tick,
            trigger="interval",
            seconds=60,
            id="watchdog_reminders",
            replace_existing=True,
        )
        scheduler.start()
    return scheduler


# =========================================================
#        Базовая постановка напоминания
# =========================================================
async def schedule_reminder(
    tg_id: int,
    machine_name: str,
    date_str: str,
    hour: int,
    minutes_before: int = 30,
):
    """
    Постановка обычного напоминания (tg_id — именно Telegram ID, а не users.id).
    """
    try:
        d = datetime.fromisoformat(date_str).date()
    except Exception:
        d = datetime.now(TZ).date()

    slot_dt = datetime.combine(d, time(hour=hour), tzinfo=TZ)
    reminder_dt = slot_dt - timedelta(minutes=minutes_before)
    now = datetime.now(TZ)

    # если уже пора / чуть опоздали — шлём сразу
    if now >= reminder_dt:
        if (now - reminder_dt).total_seconds() <= LATE_WINDOW_SEC:
            await send_reminder(tg_id, machine_name, d.isoformat(), hour, minutes_before)
        return

    job_id = f"rem_{tg_id}_{d.isoformat()}_{hour}"
    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=reminder_dt),
        id=job_id,
        args=[tg_id, machine_name, d.isoformat(), hour, minutes_before],
        replace_existing=True,
        misfire_grace_time=LATE_WINDOW_SEC,
    )


async def send_reminder(
    tg_id: int,
    machine_name: str,
    date_iso: str,
    hour: int,
    minutes_before: int,
):
    """
    Отправка напоминания. tg_id — Telegram ID.
    """
    now = datetime.now(TZ)
    slot_dt = datetime.combine(
        datetime.fromisoformat(date_iso).date(), time(hour=hour), tzinfo=TZ
    )
    reminder_dt = slot_dt - timedelta(minutes=minutes_before)

    # сильно опоздали — выходим
    if (now - reminder_dt).total_seconds() > LATE_WINDOW_SEC:
        return

    if BOT_REF is None:
        return

    # антидубли (фиксируем по tg_id + machine_id + дате/часу)
    m_id = get_machine_id_by_name(machine_name)
    if m_id is not None and was_reminder_sent(
        tg_id, m_id, date_iso, hour, minutes_before
    ):
        return

    text = (
        "⏰ <b>Напоминание</b>\n\n"
        f"Через <b>{minutes_before} мин</b> у вас стирка.\n"
        f"🧺 Машина: <b>{machine_name}</b>\n"
        f"📅 Дата: {date_iso}\n"
        f"🕒 Время: {hour:02d}:00"
    )
    try:
        await BOT_REF.send_message(tg_id, text, parse_mode="HTML")
        if m_id is not None:
            mark_reminder_sent(tg_id, m_id, date_iso, hour, minutes_before)
    except Exception:
        # молча, если не удалось отправить
        pass


# =========================================================
#   Восстановление напоминаний после рестарта
# =========================================================
async def rebuild_reminders_for_horizon(
    hours: int = 48, minutes_before: int = 30
):
    """
    При старте сервиса пробегаем по записям в горизонте `hours`
    и ставим напоминания. ВАЖНО: здесь теперь берём u.tg_id.
    """
    now = datetime.now(TZ)
    end = now + timedelta(hours=hours)

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.tg_id, m.name, b.date, b.hour
              FROM bookings b
              JOIN machines m ON m.id = b.machine_id
              JOIN users   u ON u.id = b.user_id
             WHERE (b.date > ? OR (b.date = ? AND b.hour >= ?))
               AND (b.date < ? OR (b.date = ? AND b.hour <= ?))
        """,
            (
                now.date().isoformat(),
                now.date().isoformat(),
                now.hour,
                end.date().isoformat(),
                end.date().isoformat(),
                end.hour,
            ),
        ).fetchall()

    for tg_id, machine_name, date_iso, hour in rows:
        await schedule_reminder(
            int(tg_id),
            machine_name,
            str(date_iso),
            int(hour),
            minutes_before,
        )


# =========================================================
#             Тестовые напоминания (/test_reminder)
# =========================================================
async def send_test_message(tg_id: int, text: str):
    if BOT_REF is None:
        return
    try:
        await BOT_REF.send_message(
            tg_id,
            text,
            parse_mode="HTML",
            disable_notification=True,
        )
    except Exception:
        pass


async def schedule_test_message(
    tg_id: int,
    minutes: int = 1,
    text: str = "⏰ Тестовое напоминание: всё работает ✅",
):
    run_at = datetime.now(TZ) + timedelta(minutes=minutes)
    scheduler.add_job(
        send_test_message,
        trigger=DateTrigger(run_date=run_at),
        id=f"test_{tg_id}_{int(run_at.timestamp())}",
        args=[tg_id, text],
        replace_existing=True,
        misfire_grace_time=120,  # до 2 мин терпим задержку
    )


# =========================================================
#        Сторож: если джоба умерла — добьём вручную
# =========================================================
async def watchdog_tick(minutes_before: int = 30):
    """
    Каждую минуту смотрим все брони на сегодня и завтра.
    Если сейчас попали в окно [reminder_dt, reminder_dt + LATE_WINDOW_SEC]
    и отметки в reminders_sent ещё нет — шлём напоминание.

    Здесь тоже ВАЖНО: используем u.tg_id, а не bookings.user_id.
    """
    now = datetime.now(TZ)
    # сегодня и, на случай границы суток, завтра
    today = now.date().isoformat()
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    dates = (today, tomorrow)

    placeholders = ",".join(["?"] * len(dates))
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT u.tg_id,
                   b.machine_id,
                   m.name,
                   b.date,
                   b.hour
              FROM bookings b
              JOIN machines m ON m.id = b.machine_id
              JOIN users   u ON u.id = b.user_id
             WHERE b.date IN ({placeholders})
        """,
            dates,
        ).fetchall()

    for tg_id, machine_id, m_name, date_iso, hour in rows:
        # date_iso может быть date или str
        d = datetime.fromisoformat(str(date_iso)).date()
        slot_dt = datetime.combine(d, time(hour=int(hour)), tzinfo=TZ)
        reminder_dt = slot_dt - timedelta(minutes=minutes_before)

        delta_sec = (now - reminder_dt).total_seconds()

        # окно «пора напоминать»: [reminder_dt, reminder_dt + LATE_WINDOW_SEC]
        if 0 <= delta_sec <= LATE_WINDOW_SEC:
            if not was_reminder_sent(
                int(tg_id),
                int(machine_id),
                str(date_iso),
                int(hour),
                minutes_before,
            ):
                await send_reminder(
                    int(tg_id),
                    m_name,
                    str(date_iso),
                    int(hour),
                    minutes_before,
                )
