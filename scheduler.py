# scheduler.py
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

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

# --- Запрещаем «догонять» пропущенные напоминания слишком поздно ---
job_defaults = {
    "misfire_grace_time": 120,
    "coalesce": True,
    "max_instances": 1,
}

scheduler = AsyncIOScheduler(
    timezone=TZ,
    job_defaults=job_defaults,
)

def setup_scheduler():
    if not scheduler.running:
        # ежедневная очистка старых бронирований
        scheduler.add_job(
            cleanup_old_bookings,
            trigger="cron",
            hour=0,
            minute=5,
            id="cleanup_daily",
            replace_existing=True,
        )
        from database import record_usage_history
        from waitlist_service import (
            process_night_round,
            expire_holds,
            send_pending_notifications,
            check_active_waitlist,
        )
        scheduler.add_job(
            process_night_round,
            trigger="cron",
            hour=23,
            minute=0,
            id="waitlist_night_round",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            expire_holds,
            trigger="interval",
            seconds=20,
            id="waitlist_expire_holds",
            replace_existing=True,
            misfire_grace_time=60,
        )
        scheduler.add_job(
            send_pending_notifications,
            trigger="interval",
            seconds=20,
            id="waitlist_pending_notifications",
            replace_existing=True,
            misfire_grace_time=60,
        )
        scheduler.add_job(
            check_active_waitlist,
            trigger="interval",
            minutes=1,
            id="waitlist_active_poll",
            replace_existing=True,
            misfire_grace_time=60,
        )
        scheduler.add_job(
            record_usage_history,
            trigger="interval",
            minutes=10,
            id="usage_history_tick",
            replace_existing=True,
            misfire_grace_time=600,
        )
        # сторож: каждую минуту проверяем, не пришло ли время напоминания
        '''
        scheduler.add_job(
            watchdog_tick,
            trigger="interval",
            seconds=60,
            id="watchdog_reminders",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        '''
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
    Постановка обычного напоминания (tg_id - именно Telegram ID, а не users.id).
    """
    try:
        d = datetime.fromisoformat(date_str).date()
    except Exception:
        d = datetime.now(TZ).date()

    slot_dt = datetime.combine(d, time(hour=hour), tzinfo=TZ)
    reminder_dt = slot_dt - timedelta(minutes=minutes_before)
    now = datetime.now(TZ)

    # если уже пора / чуть опоздали - шлём сразу
    if now >= reminder_dt:
        if (now - reminder_dt).total_seconds() <= LATE_WINDOW_SEC:
            await send_reminder(tg_id, machine_name, d.isoformat(), hour, minutes_before)
        return

    safe_name = str(machine_name).replace(" ", "_")
    job_id = f"rem_{tg_id}_{safe_name}_{d.isoformat()}_{hour}_{minutes_before}"

    #job_id = f"rem_{tg_id}_{d.isoformat()}_{hour}"
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
    allow_late: bool = False
):
    """
    Отправка напоминания. tg_id - Telegram ID.

    Здесь:
    - проверяем, что бронь ещё существует;
    - если это сушка и за час до неё есть стирка, не шлём напоминание;
    - текст зависит от типа машины (wash/dry).
    """
    now = datetime.now(TZ)
    slot_dt = datetime.combine(
        datetime.fromisoformat(date_iso).date(), time(hour=hour), tzinfo=TZ
    )
    reminder_dt = slot_dt - timedelta(minutes=minutes_before)

    # слишком рано
    if now < reminder_dt:
        return

    if allow_late:
        # шлём “догоняющее” напоминание до начала слота
        if now >= slot_dt:
            return
    else:
        # строгая политика: только в окне LATE_WINDOW_SEC
        if (now - reminder_dt).total_seconds() > LATE_WINDOW_SEC:
            return
    '''
    # сильно опоздали - выходим
    if (now - reminder_dt).total_seconds() > LATE_WINDOW_SEC:
        return
    '''
    if BOT_REF is None:
        return

    # определяем машину и её тип
    m_id = get_machine_id_by_name(machine_name)
    if m_id is None:
        # если по имени не нашли машину - лучше вообще ничего не слать
        return

    with get_conn() as conn:
        row = conn.execute(
            "SELECT type FROM machines WHERE id=?",
            (m_id,),
        ).fetchone()
    machine_type = row[0] if row else None

    # 1) проверка: бронь всё ещё существует?
    with get_conn() as conn:
        exists = conn.execute(
            """
            SELECT 1
              FROM bookings b
              JOIN users   u ON u.id = b.user_id
             WHERE u.tg_id = ?
               AND b.machine_id = ?
               AND b.date = ?
               AND b.hour = ?
             LIMIT 1
        """,
            (tg_id, m_id, date_iso, hour),
        ).fetchone()

    if not exists:
        # запись отменена или перенесена - не шлём
        return

    # 2) если это СУШКА и сразу перед ней есть СТИРКА этого же пользователя,
    # то напоминание на сушку не отправляем
    if machine_type == "dry" and hour > 0:
        prev_hour = hour - 1
        with get_conn() as conn:
            has_wash_prev = conn.execute(
                """
                SELECT 1
                  FROM bookings b
                  JOIN users   u ON u.id = b.user_id
                  JOIN machines m ON m.id = b.machine_id
                 WHERE u.tg_id = ?
                   AND b.date = ?
                   AND b.hour = ?
                   AND m.type = 'wash'
                 LIMIT 1
            """,
                (tg_id, date_iso, prev_hour),
            ).fetchone()
        if has_wash_prev:
            # сразу после стирки идёт сушка - напоминание для сушилки не нужно
            return

    # 3) антидубли (фиксируем по tg_id + machine_id + дате/часу)
    if was_reminder_sent(tg_id, m_id, date_iso, hour, minutes_before):
        return

    # подбираем текст под тип машины
    if machine_type == "wash":
        kind = "стирка"
        emoji = "🧺"
    elif machine_type == "dry":
        kind = "сушка"
        emoji = "🌬️"
    else:
        kind = "стирка"
        emoji = "🧺"

    text = (
        "⏰ <b>Напоминание</b>\n\n"
        f"Через <b>{minutes_before} мин</b> у вас {kind}.\n"
        f"{emoji} Машина: <b>{machine_name}</b>\n"
        f"📅 Дата: {date_iso}\n"
        f"🕒 Время: {hour:02d}:00"
    )
    '''
    try:
        await BOT_REF.send_message(tg_id, text, parse_mode="HTML")
        mark_reminder_sent(tg_id, m_id, date_iso, hour, minutes_before)
    except Exception:
        # молча, если не удалось отправить
        pass
    '''
    # 1) сначала пробуем отправить сообщение
    try:
        await BOT_REF.send_message(tg_id, text, parse_mode="HTML")
    except Exception:
        # если не смогли отправить (пользователь заблокировал бота и т.п.) -
        # просто выходим, чтобы не спамить ретраями
        return

    # 2) затем фиксируем факт отправки в БД (без try/except, чтобы
    mark_reminder_sent(tg_id, m_id, date_iso, hour, minutes_before)


# =========================================================
#   Восстановление напоминаний после рестарта
# =========================================================
async def rebuild_reminders_for_horizon(
    hours: int = 48, minutes_before: int = 30
):
    """
    При старте сервиса пробегаем по записям в горизонте `hours`
    и ставим напоминания. ВАЖНО: здесь берём u.tg_id.
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
#        Сторож: если джоба умерла - добьём вручную
# =========================================================
async def watchdog_tick(minutes_before: int = 30):
    """
    Каждую минуту смотрим все брони на сегодня и завтра.
    Если сейчас попали в окно [reminder_dt, reminder_dt + LATE_WINDOW_SEC]
    и отметки в reminders_sent ещё нет - шлём напоминание.

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

        # вместо окна 0..LATE_WINDOW_SEC
        if reminder_dt <= now < slot_dt:
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
                    allow_late=True
                )

        '''
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
        '''
