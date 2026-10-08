from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramRetryAfter

from config import TIMEZONE, WORKING_HOURS
from database import (
    get_conn,
    get_notification_settings,
    usage_penalties_for_users,
    get_free_hours_effective,
    active_hold_for_user,
    is_banned,
    record_usage_history,
)
from booking_service import create_booking_safe, BookingError, get_booking
from dryer_service import find_next_dryer
from keyboards import build_main_menu

TZ = ZoneInfo(TIMEZONE)
BOT: Bot | None = None
_DISTRIBUTION_LOCK = asyncio.Lock()
MONTHS = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


def pretty_date(date_iso: str) -> str:
    d = datetime.fromisoformat(str(date_iso)).date()
    return f"{d.day} {MONTHS[d.month]}"

HOLD_MINUTES = 5
URGENT_HOLD_MINUTES = 2
MIN_HOLD_LEAD_MINUTES = 5
MIN_AUTO_LEAD_MINUTES = 30
WAITLIST_ENABLED = os.getenv("WAITLIST_ENABLED", "true").lower() not in {"0", "false", "off", "no"}


@dataclass
class Request:
    id: int
    user_id: int
    tg_id: int
    mode: str
    any_machine: bool
    priority_since: datetime
    intervals: list[tuple[int, int]]
    machines: set[int]
    weekdays: set[int] = field(default_factory=set)
    schedule: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    usage_points: int = 0

    def intervals_for_date(self, date_iso: str) -> list[tuple[int, int]]:
        weekday = datetime.fromisoformat(str(date_iso)).date().weekday()
        if self.schedule:
            return list(self.schedule.get(weekday, []))
        if self.weekdays and weekday not in self.weekdays:
            return []
        return list(self.intervals)

    def accepts_hour(self, hour: int, date_iso: str | None = None) -> bool:
        intervals = self.intervals_for_date(date_iso) if date_iso else self.intervals
        return any(start <= hour < end for start, end in intervals)

    def accepts_machine(self, machine_id: int) -> bool:
        return self.any_machine or machine_id in self.machines

    def accepts_date(self, date_iso: str) -> bool:
        if self.schedule:
            weekday = datetime.fromisoformat(str(date_iso)).date().weekday()
            return bool(self.schedule.get(weekday))
        if not self.weekdays:
            return True
        return datetime.fromisoformat(str(date_iso)).date().weekday() in self.weekdays

    def queue_score(self, now: datetime) -> int:
        waiting = min(168, max(0, int((now - self.priority_since).total_seconds() // 3600)))
        return waiting - self.usage_points * 12


def attach_bot(bot: Bot) -> None:
    global BOT
    BOT = bot


def _dt(value) -> datetime:
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _slot_start(date_iso: str, hour: int) -> datetime:
    d = datetime.fromisoformat(str(date_iso)).date()
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ).replace(hour=int(hour))


def _auto_booking_allowed(
    date_iso: str,
    hour: int,
    now: datetime | None = None,
) -> bool:
    """AUTO may book silently only when the user has at least 30 minutes' notice."""
    now = now or datetime.now(TZ)
    return _slot_start(date_iso, hour) - now >= timedelta(minutes=MIN_AUTO_LEAD_MINUTES)


def _booking_end(date_iso: str, hour: int) -> datetime:
    return _slot_start(date_iso, hour) + timedelta(hours=1)


def _hold_duration_minutes(
    date_iso: str, hour: int, now: datetime | None = None
) -> int | None:
    """5 min normally; 2 min with 5–30 min lead; no offer under 5 min."""
    now = now or datetime.now(TZ)
    lead = _slot_start(date_iso, hour) - now
    if lead < timedelta(minutes=MIN_HOLD_LEAD_MINUTES):
        return None
    if lead < timedelta(minutes=MIN_AUTO_LEAD_MINUTES):
        return URGENT_HOLD_MINUTES
    return HOLD_MINUTES


def _hold_deadline_text(expires_at: datetime, minutes: int) -> str:
    """Show the real expiry, not a rounded-up minute that might mislead."""
    word = "минуты" if minutes == 2 else "минут"
    return (
        f"Слот удерживается за вами {minutes} {word}, "
        f"до {expires_at.astimezone(TZ):%H:%M}."
    )


def _current_or_future_wash_bookings(
    user_id: int,
    now: datetime | None = None,
    *,
    exclude_booking_id: int | None = None,
) -> list[tuple[int, str, int, datetime]]:
    """Return wash bookings that have not finished yet, ordered by finish time."""
    now = now or datetime.now(TZ)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.id,b.date,b.hour
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE b.user_id=? AND m.type='wash' AND b.date>=?
            ORDER BY b.date,b.hour
            """,
            (int(user_id), now.date().isoformat()),
        ).fetchall()

    result = []
    for booking_id, date_value, hour in rows:
        if exclude_booking_id is not None and int(booking_id) == int(exclude_booking_id):
            continue
        date_iso = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
        end_at = _booking_end(date_iso, int(hour))
        if end_at > now:
            result.append((int(booking_id), date_iso, int(hour), end_at))
    return sorted(result, key=lambda row: row[3])


def _subscription_resume_job_id(request_id: int) -> str:
    return f"waitlist_resume_{int(request_id)}"


def _cancel_subscription_resume(request_id: int) -> None:
    _remove_job(_subscription_resume_job_id(int(request_id)))


def _schedule_subscription_resume(
    request_id: int,
    booking_id: int,
    date_iso: str,
    hour: int,
) -> None:
    """Wake exactly when the controlling wash ends; no polling is required."""
    from apscheduler.triggers.date import DateTrigger
    from scheduler import scheduler

    run_at = _booking_end(str(date_iso), int(hour))
    now = datetime.now(TZ)
    if run_at <= now:
        run_at = now + timedelta(seconds=1)

    scheduler.add_job(
        resume_subscription_after_wash,
        trigger=DateTrigger(run_date=run_at),
        id=_subscription_resume_job_id(int(request_id)),
        args=[int(request_id), int(booking_id)],
        replace_existing=True,
        misfire_grace_time=3600,
    )


def sync_subscription_pause_for_user(user_id: int) -> None:
    """
    Keep one persistent subscription paused while the user already has a wash.

    'paused' means the subscription was created while a booking already existed:
    its priority has not started yet.
    'matched' means an already-waiting subscription received a booking:
    old priority is kept in case that booking is cancelled.
    """
    now = datetime.now(TZ)
    bookings = _current_or_future_wash_bookings(int(user_id), now)
    with get_conn() as conn:
        req = conn.execute(
            """
            SELECT id,status,priority_since
            FROM waitlist_requests
            WHERE user_id=? AND persistent=1 AND status IN ('active','matched','paused')
            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                     updated_at DESC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if not req:
            return

        request_id, status, priority_since = int(req[0]), str(req[1]), req[2]

        if not bookings:
            if status in {'matched', 'paused'}:
                new_priority = (
                    now.isoformat(timespec="seconds")
                    if status == 'paused'
                    else str(priority_since)
                )
                conn.execute(
                    """
                    UPDATE waitlist_requests
                    SET status='active',matched_booking_id=NULL,priority_since=?,updated_at=?
                    WHERE id=?
                    """,
                    (new_priority, now.isoformat(timespec="seconds"), request_id),
                )
            _cancel_subscription_resume(request_id)
            return

        booking_id, date_iso, hour, end_at = bookings[-1]
        new_status = 'matched' if status in {'active', 'matched'} else 'paused'
        new_priority = (
            end_at.isoformat(timespec="seconds")
            if new_status == 'paused'
            else str(priority_since)
        )
        conn.execute(
            """
            UPDATE waitlist_requests
            SET status=?,matched_booking_id=?,priority_since=?,updated_at=?
            WHERE id=?
            """,
            (
                new_status,
                int(booking_id),
                new_priority,
                now.isoformat(timespec="seconds"),
                request_id,
            ),
        )

    _schedule_subscription_resume(request_id, booking_id, date_iso, hour)


async def resume_subscription_after_wash(
    request_id: int,
    booking_id: int,
    *,
    now: datetime | None = None,
    redistribute: bool = True,
) -> bool:
    """Reactivate a persistent subscription after its controlling wash finishes."""
    now = now or datetime.now(TZ)

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT wr.user_id,wr.status,wr.matched_booking_id,b.date,b.hour
            FROM waitlist_requests wr
            LEFT JOIN bookings b ON b.id=wr.matched_booking_id
            WHERE wr.id=? AND wr.persistent=1 AND wr.status IN ('matched','paused')
            """,
            (int(request_id),),
        ).fetchone()
        if not row:
            _cancel_subscription_resume(int(request_id))
            return False

        user_id, status, matched_booking_id, date_value, hour = row
        if matched_booking_id is None or int(matched_booking_id) != int(booking_id):
            return False

        # Legacy rows can outlive their booking after booking-history cleanup.
        if date_value is None or hour is None:
            finished_at = now
        else:
            date_iso = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
            finished_at = _booking_end(date_iso, int(hour))
            if finished_at > now:
                _schedule_subscription_resume(
                    int(request_id), int(booking_id), date_iso, int(hour)
                )
                return False

    # A later wash may have been booked while this subscription was paused.
    remaining = _current_or_future_wash_bookings(int(user_id), now)
    if remaining:
        next_booking_id, next_date, next_hour, next_end = remaining[-1]
        with get_conn() as conn:
            current = conn.execute(
                "SELECT status,priority_since FROM waitlist_requests WHERE id=?",
                (int(request_id),),
            ).fetchone()
            if not current:
                return False
            current_status = str(current[0])
            priority = (
                next_end.isoformat(timespec="seconds")
                if current_status == 'paused'
                else str(current[1])
            )
            conn.execute(
                """
                UPDATE waitlist_requests
                SET matched_booking_id=?,priority_since=?,updated_at=?
                WHERE id=? AND status IN ('matched','paused')
                """,
                (
                    int(next_booking_id),
                    priority,
                    now.isoformat(timespec="seconds"),
                    int(request_id),
                ),
            )
        _schedule_subscription_resume(
            int(request_id), int(next_booking_id), next_date, int(next_hour)
        )
        return False

    with get_conn() as conn:
        other_active = conn.execute(
            """
            SELECT 1 FROM waitlist_requests
            WHERE user_id=? AND status='active' AND id<>?
            LIMIT 1
            """,
            (int(user_id), int(request_id)),
        ).fetchone()
        if other_active:
            _cancel_subscription_resume(int(request_id))
            return False

        conn.execute(
            """
            UPDATE waitlist_requests
            SET status='active',matched_booking_id=NULL,priority_since=?,updated_at=?
            WHERE id=? AND status IN ('matched','paused')
            """,
            (
                finished_at.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                int(request_id),
            ),
        )

    _cancel_subscription_resume(int(request_id))
    if redistribute:
        await check_active_waitlist()
    return True


def _remove_job(job_id: str) -> None:
    try:
        from scheduler import scheduler
        scheduler.remove_job(job_id)
    except Exception:
        pass


def _schedule_hold_expiry(hold_id: int, expires_at) -> None:
    """One in-memory job per HOLD. No database polling is required."""
    from apscheduler.triggers.date import DateTrigger
    from scheduler import scheduler

    run_at = _dt(expires_at)
    now = datetime.now(TZ)
    if run_at <= now:
        run_at = now + timedelta(seconds=1)

    scheduler.add_job(
        expire_hold,
        trigger=DateTrigger(run_date=run_at),
        id=f"waitlist_hold_{int(hold_id)}",
        args=[int(hold_id)],
        replace_existing=True,
        misfire_grace_time=300,
    )


def _cancel_hold_expiry(hold_id: int) -> None:
    _remove_job(f"waitlist_hold_{int(hold_id)}")


def _night_hold_job_id(date_iso: str) -> str:
    return f"waitlist_night_holds_{str(date_iso)}"


def _schedule_night_hold_phase(date_iso: str, run_at: datetime) -> None:
    from apscheduler.triggers.date import DateTrigger
    from scheduler import scheduler

    now = datetime.now(TZ)
    if run_at <= now:
        run_at = now + timedelta(seconds=1)

    scheduler.add_job(
        process_night_hold_phase,
        trigger=DateTrigger(run_date=run_at),
        id=_night_hold_job_id(str(date_iso)),
        args=[str(date_iso)],
        replace_existing=True,
        misfire_grace_time=3600,
    )


def schedule_next_pending_notification() -> None:
    """Schedule one exact wake-up for the earliest unsent midnight notification."""
    from apscheduler.triggers.date import DateTrigger
    from scheduler import scheduler

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT MIN(send_at)
            FROM pending_waitlist_notifications
            WHERE sent=0
            """
        ).fetchone()

    if not row or not row[0]:
        _remove_job("waitlist_pending_notifications_once")
        return

    run_at = _dt(row[0])
    now = datetime.now(TZ)
    if run_at <= now:
        run_at = now + timedelta(seconds=1)

    scheduler.add_job(
        send_pending_notifications,
        trigger=DateTrigger(run_date=run_at),
        id="waitlist_pending_notifications_once",
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def rebuild_waitlist_jobs() -> None:
    """
    One-time recovery after a Render restart.

    Rebuild exact HOLD expiration jobs and the next deferred notification,
    then run one waitlist consistency check. Nothing here polls Neon.
    """
    now = datetime.now(TZ)
    with get_conn() as conn:
        holds = conn.execute(
            """
            SELECT id,expires_at,context,created_at
            FROM slot_holds
            WHERE status='active'
            ORDER BY expires_at
            """
        ).fetchall()

    for hold_id, expires_at, context, created_at in holds:
        # night_pending is a durable fair-round reservation. It must not expire
        # before the second phase has actually shown the user the offer.
        if str(context) == "night_pending":
            continue

        # A Render restart can consume most or all of an active night HOLD.
        # Give the user a fresh response window after startup instead
        # of treating infrastructure downtime as a refusal.
        if str(context) == "night":
            created = _dt(created_at)
            if now - created <= timedelta(minutes=10):
                restored_expiry = now + timedelta(minutes=HOLD_MINUTES)
                with get_conn() as conn:
                    conn.execute(
                        """
                        UPDATE slot_holds
                        SET expires_at=?
                        WHERE id=? AND status='active'
                        """,
                        (
                            restored_expiry.isoformat(timespec="seconds"),
                            int(hold_id),
                        ),
                    )
                _schedule_hold_expiry(int(hold_id), restored_expiry)
                continue

        if _dt(expires_at) <= now:
            await expire_hold(int(hold_id))
        else:
            _schedule_hold_expiry(int(hold_id), expires_at)

    with get_conn() as conn:
        subscriptions = conn.execute(
            """
            SELECT wr.id,wr.matched_booking_id,b.date,b.hour
            FROM waitlist_requests wr
            LEFT JOIN bookings b ON b.id=wr.matched_booking_id
            WHERE wr.persistent=1 AND wr.status IN ('matched','paused')
            """
        ).fetchall()

    for request_id, booking_id, date_value, hour in subscriptions:
        if booking_id is None or date_value is None or hour is None:
            # Old matched rows whose booking has already been cleaned up should
            # no longer stay stuck forever.
            await resume_subscription_after_wash(
                int(request_id),
                int(booking_id or 0),
                now=now,
                redistribute=False,
            )
            continue
        date_iso = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
        if _booking_end(date_iso, int(hour)) <= now:
            await resume_subscription_after_wash(
                int(request_id),
                int(booking_id),
                now=now,
                redistribute=False,
            )
        else:
            _schedule_subscription_resume(
                int(request_id), int(booking_id), date_iso, int(hour)
            )

    # Normalize active persistent subscriptions against existing future
    # washes. If the user already has a wash, the subscription must not compete
    # for another slot.
    with get_conn() as conn:
        active_user_rows = conn.execute(
            """
            SELECT DISTINCT user_id
            FROM waitlist_requests
            WHERE persistent=1 AND status='active'
            """
        ).fetchall()
    for (user_id,) in active_user_rows:
        sync_subscription_pause_for_user(int(user_id))

    # A persistent subscription that has already completed a wash must never
    # keep waiting time from before that wash. This also repairs legacy rows
    # created before persistent lifecycle handling existed.
    with get_conn() as conn:
        active_rows = conn.execute(
            """
            SELECT wr.id,wr.priority_since,l.occurred_at
            FROM waitlist_requests wr
            JOIN (
                SELECT user_id,MAX(occurred_at) AS occurred_at
                FROM laundry_usage_history
                GROUP BY user_id
            ) l ON l.user_id=wr.user_id
            WHERE wr.persistent=1 AND wr.status='active'
            """
        ).fetchall()

        for request_id, priority_since, occurred_at in active_rows:
            try:
                priority_dt = _dt(priority_since)
                usage_dt = _dt(occurred_at)
            except Exception:
                continue
            resume_at = usage_dt + timedelta(hours=1)
            if priority_dt < resume_at:
                conn.execute(
                    """
                    UPDATE waitlist_requests
                    SET priority_since=?,updated_at=?
                    WHERE id=? AND persistent=1 AND status='active'
                    """,
                    (
                        resume_at.isoformat(timespec="seconds"),
                        datetime.now(TZ).isoformat(timespec="seconds"),
                        int(request_id),
                    ),
                )

    # Rebuild the durable second phase of a night round after a restart.
    with get_conn() as conn:
        pending_rounds = conn.execute(
            """
            SELECT target_date,cutoff_at,status
            FROM waitlist_rounds
            WHERE status IN ('holds_pending','holds_running')
            ORDER BY target_date
            """
        ).fetchall()
    for target_date, cutoff_at, status in pending_rounds:
        try:
            planned = _dt(cutoff_at) + timedelta(seconds=90)
        except Exception:
            planned = now + timedelta(seconds=1)
        _schedule_night_hold_phase(
            str(target_date),
            max(planned, now + timedelta(seconds=1)),
        )

    schedule_next_pending_notification()
    await check_active_waitlist()


def get_active_request_for_tg(tg_id: int):
    """Return the user's current persistent subscription, even while it is paused."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT wr.id,wr.mode,wr.any_machine,wr.created_at,wr.priority_since
            FROM waitlist_requests wr
            JOIN users u ON u.id=wr.user_id
            WHERE u.tg_id=? AND wr.persistent=1
              AND wr.status IN ('active','matched','paused')
            ORDER BY CASE wr.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                     wr.updated_at DESC
            LIMIT 1
            """,
            (int(tg_id),),
        ).fetchone()


async def finalize_ban_cleanup(cleanup: dict | None) -> None:
    """Release scheduler jobs and redistribute slots freed by a ban."""
    if not cleanup:
        return

    dates_to_day = set()
    dates_to_night = set()
    now = datetime.now(TZ)

    for hold_id, date_iso, context in cleanup.get("holds", []):
        _cancel_hold_expiry(int(hold_id))
        if str(context) == "night" and now.hour == 23:
            dates_to_night.add(str(date_iso))
        else:
            dates_to_day.add(str(date_iso))

    for date_iso in sorted(dates_to_night):
        await distribute_date(date_iso, context="night")
    for date_iso in sorted(dates_to_day):
        await distribute_date(date_iso, context="day")


def cancel_request_for_tg(tg_id: int) -> bool:
    now = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT wr.id FROM waitlist_requests wr
            JOIN users u ON u.id=wr.user_id
            WHERE u.tg_id=? AND wr.persistent=1
              AND wr.status IN ('active','matched','paused')
            """,
            (int(tg_id),),
        ).fetchall()
        if not rows:
            return False

        request_ids = [int(r[0]) for r in rows]
        hold_rows = []
        for request_id in request_ids:
            hold_rows.extend(
                conn.execute(
                    "SELECT id FROM slot_holds WHERE request_id=? AND status='active'",
                    (request_id,),
                ).fetchall()
            )
            conn.execute(
                "UPDATE waitlist_requests SET status='cancelled',updated_at=? WHERE id=?",
                (now, request_id),
            )
            conn.execute(
                "UPDATE slot_holds SET status='cancelled' WHERE request_id=? AND status='active'",
                (request_id,),
            )

    for request_id in request_ids:
        _cancel_subscription_resume(request_id)
    for (hold_id,) in hold_rows:
        _cancel_hold_expiry(int(hold_id))
    from forecast_service import invalidate_forecasts
    invalidate_forecasts()
    return True


def _normalize_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    normalized: list[tuple[int, int]] = []
    for start, end in sorted((int(a), int(b)) for a, b in intervals):
        if start < min(WORKING_HOURS) or end > max(WORKING_HOURS) + 1 or start >= end:
            raise ValueError("Некорректный интервал")
        if normalized and start <= normalized[-1][1]:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], end))
        else:
            normalized.append((start, end))
    if not normalized or len(normalized) > 3:
        raise ValueError("Для каждого дня нужно выбрать от 1 до 3 интервалов")
    return normalized


def _normalize_schedule(
    intervals: list[tuple[int, int]],
    weekdays: list[int] | None,
    schedule: dict[int, list[tuple[int, int]]] | None,
) -> dict[int, list[tuple[int, int]]]:
    if schedule is not None:
        normalized_schedule: dict[int, list[tuple[int, int]]] = {}
        for raw_day, raw_intervals in schedule.items():
            day = int(raw_day)
            if day < 0 or day > 6:
                raise ValueError("Некорректный день недели")
            if raw_intervals:
                normalized_schedule[day] = _normalize_intervals(list(raw_intervals))
        if not normalized_schedule:
            raise ValueError("Настройте хотя бы один день")
        return normalized_schedule

    normalized_weekdays = sorted({int(x) for x in (weekdays or [])})
    if any(day < 0 or day > 6 for day in normalized_weekdays):
        raise ValueError("Некорректный день недели")
    common = _normalize_intervals(intervals)
    days = normalized_weekdays or list(range(7))
    return {day: list(common) for day in days}


def _legacy_intervals_from_schedule(
    schedule: dict[int, list[tuple[int, int]]],
) -> list[tuple[int, int]]:
    # Kept only for backwards compatibility with admin/export code from older
    # deployments. Matching uses waitlist_schedule directly.
    unique = {
        (int(start), int(end))
        for intervals in schedule.values()
        for start, end in intervals
    }
    return sorted(unique)


def save_request(
    tg_id: int,
    intervals: list[tuple[int, int]],
    machine_ids: list[int],
    any_machine: bool,
    mode: str,
    weekdays: list[int] | None = None,
    schedule: dict[int, list[tuple[int, int]]] | None = None,
) -> int:
    now = datetime.now(TZ)
    if mode not in {"auto", "notify"}:
        raise ValueError("Некорректный режим")

    normalized_schedule = _normalize_schedule(intervals, weekdays, schedule)
    legacy_intervals = _legacy_intervals_from_schedule(normalized_schedule)
    schedule_days = sorted(normalized_schedule)
    legacy_weekdays = [] if schedule_days == list(range(7)) else schedule_days

    with get_conn() as conn:
        user = conn.execute(
            "SELECT id,surname,room FROM users WHERE tg_id=?",
            (int(tg_id),),
        ).fetchone()
        if not user or not user[1] or not user[2]:
            raise ValueError("Сначала завершите регистрацию")
        if is_banned(int(tg_id)):
            raise ValueError("Вы заблокированы и не можете использовать лист ожидания")
        user_id = int(user[0])

        open_bookings = _current_or_future_wash_bookings(user_id, now)
        controlling_booking = open_bookings[-1] if open_bookings else None

        current_rows = conn.execute(
            """
            SELECT id,mode,any_machine,priority_since,status,matched_booking_id
            FROM waitlist_requests
            WHERE user_id=? AND persistent=1
              AND status IN ('active','matched','paused')
            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                     updated_at DESC
            """,
            (user_id,),
        ).fetchall()
        old = current_rows[0] if current_rows else None
        now_s = now.isoformat(timespec="seconds")
        hold_rows = []

        # Legacy versions could leave an old matched row next to a newer active
        # request. Keep one subscription when the user edits it.
        for duplicate in current_rows[1:]:
            duplicate_id = int(duplicate[0])
            conn.execute(
                "UPDATE waitlist_requests SET status='cancelled',updated_at=? WHERE id=?",
                (now_s, duplicate_id),
            )
            conn.execute(
                "UPDATE slot_holds SET status='cancelled' WHERE request_id=? AND status='active'",
                (duplicate_id,),
            )
            _cancel_subscription_resume(duplicate_id)

        if old:
            request_id = int(old[0])
            old_status = str(old[4])
            old_machines = {
                int(r[0])
                for r in conn.execute(
                    "SELECT machine_id FROM waitlist_machines WHERE request_id=?",
                    (request_id,),
                ).fetchall()
            }
            new_machines = set() if any_machine else {int(x) for x in machine_ids}

            # Editing days/hours or switching AUTO/notify must not erase waiting
            # time. Only changing machine eligibility resets an actively waiting
            # subscription. Paused subscriptions do not start priority early.
            # Machine preferences are editable without losing accrued waiting
            # time, just like days/hours and AUTO/notify settings.

            if controlling_booking:
                booking_id, booking_date, booking_hour, booking_end = controlling_booking
                if old_status == 'paused':
                    status = 'paused'
                    priority_since = booking_end.isoformat(timespec="seconds")
                else:
                    status = 'matched'
                    priority_since = (
                        str(old[3])
                    )
                matched_booking_id = int(booking_id)
            else:
                status = 'active'
                matched_booking_id = None
                if old_status == 'paused':
                    priority_since = now_s
                 else:
                    priority_since = str(old[3])

            conn.execute(
                """
                UPDATE waitlist_requests
                SET mode=?,status=?,any_machine=?,priority_since=?,matched_booking_id=?,
                    persistent=1,updated_at=?
                WHERE id=?
                """,
                (
                    mode,
                    status,
                    int(bool(any_machine)),
                    priority_since,
                    matched_booking_id,
                    now_s,
                    request_id,
                ),
            )
            hold_rows = conn.execute(
                "SELECT id FROM slot_holds WHERE request_id=? AND status='active'",
                (request_id,),
            ).fetchall()
            conn.execute(
                "UPDATE slot_holds SET status='cancelled' WHERE request_id=? AND status='active'",
                (request_id,),
            )
            conn.execute("DELETE FROM waitlist_intervals WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM waitlist_machines WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM waitlist_weekdays WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM waitlist_schedule WHERE request_id=?", (request_id,))
        else:
            if controlling_booking:
                booking_id, booking_date, booking_hour, booking_end = controlling_booking
                status = 'paused'
                matched_booking_id = int(booking_id)
                priority_since = booking_end.isoformat(timespec="seconds")
            else:
                status = 'active'
                matched_booking_id = None
                priority_since = now_s

            cur = conn.execute(
                """
                INSERT INTO waitlist_requests
                (user_id,mode,status,any_machine,created_at,priority_since,matched_booking_id,persistent,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    mode,
                    status,
                    int(bool(any_machine)),
                    now_s,
                    priority_since,
                    matched_booking_id,
                    1,
                    now_s,
                ),
            )
            request_id = getattr(cur, "lastrowid", None)
            if not request_id:
                request_id = int(conn.execute(
                    """
                    SELECT id FROM waitlist_requests
                    WHERE user_id=? AND persistent=1
                      AND status IN ('active','matched','paused')
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()[0])

        for weekday, day_intervals in sorted(normalized_schedule.items()):
            for start, end in day_intervals:
                conn.execute(
                    """
                    INSERT INTO waitlist_schedule(request_id,weekday,start_hour,end_hour)
                    VALUES (?,?,?,?)
                    ON CONFLICT DO NOTHING
                    """,
                    (request_id, int(weekday), int(start), int(end)),
                )

        # Legacy mirror: harmless for new code and keeps older admin/export
        # readers usable during rolling deployments.
        for start, end in legacy_intervals:
            conn.execute(
                "INSERT INTO waitlist_intervals(request_id,start_hour,end_hour) VALUES (?,?,?)",
                (request_id, int(start), int(end)),
            )
        for weekday in legacy_weekdays:
            conn.execute(
                "INSERT INTO waitlist_weekdays(request_id,weekday) VALUES (?,?) ON CONFLICT DO NOTHING",
                (request_id, int(weekday)),
            )

        if not any_machine:
            for mid in sorted({int(x) for x in machine_ids}):
                conn.execute(
                    "INSERT INTO waitlist_machines(request_id,machine_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                    (request_id, mid),
                )

    if old:
        for (hold_id,) in hold_rows:
            _cancel_hold_expiry(int(hold_id))

    if controlling_booking:
        booking_id, booking_date, booking_hour, _booking_end_at = controlling_booking
        _schedule_subscription_resume(
            int(request_id), int(booking_id), str(booking_date), int(booking_hour)
        )
    else:
        _cancel_subscription_resume(int(request_id))

    # Import lazily: forecast_service reads requests from this module.
    from forecast_service import invalidate_forecasts
    invalidate_forecasts()
    return int(request_id)


def _active_requests(cutoff_at: str | None = None) -> list[Request]:
    with get_conn() as conn:
        if cutoff_at:
            rows = conn.execute(
                """
                SELECT wr.id,wr.user_id,u.tg_id,wr.mode,wr.any_machine,wr.priority_since
                FROM waitlist_requests wr
                JOIN users u ON u.id=wr.user_id
                WHERE wr.status='active' AND wr.persistent=1 AND wr.priority_since<=?
                ORDER BY wr.priority_since
                """,
                (str(cutoff_at),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT wr.id,wr.user_id,u.tg_id,wr.mode,wr.any_machine,wr.priority_since
                FROM waitlist_requests wr
                JOIN users u ON u.id=wr.user_id
                WHERE wr.status='active' AND wr.persistent=1
                ORDER BY wr.priority_since
                """
            ).fetchall()

        interval_rows = conn.execute(
            "SELECT request_id,start_hour,end_hour FROM waitlist_intervals"
        ).fetchall()
        schedule_rows = conn.execute(
            "SELECT request_id,weekday,start_hour,end_hour FROM waitlist_schedule ORDER BY request_id,weekday,start_hour"
        ).fetchall()
        machine_rows = conn.execute(
            "SELECT request_id,machine_id FROM waitlist_machines"
        ).fetchall()
        weekday_rows = conn.execute(
            "SELECT request_id,weekday FROM waitlist_weekdays"
        ).fetchall()
        now_s = datetime.now(TZ).isoformat(timespec="seconds")
        banned_rows = conn.execute("SELECT tg_id,banned_until FROM banned").fetchall()
        held_user_rows = conn.execute(
            """
            SELECT DISTINCT user_id
            FROM slot_holds
            WHERE status='active' AND expires_at>?
            """,
            (now_s,),
        ).fetchall()
        future_wash_rows = conn.execute(
            """
            SELECT DISTINCT b.user_id,b.date,b.hour
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE m.type='wash' AND b.date>=?
            """,
            (datetime.now(TZ).date().isoformat(),),
        ).fetchall()

    intervals: dict[int, list[tuple[int, int]]] = {}
    for rid, start, end in interval_rows:
        intervals.setdefault(int(rid), []).append((int(start), int(end)))

    schedules: dict[int, dict[int, list[tuple[int, int]]]] = {}
    for rid, weekday, start, end in schedule_rows:
        schedules.setdefault(int(rid), {}).setdefault(int(weekday), []).append(
            (int(start), int(end))
        )

    machines: dict[int, set[int]] = {}
    for rid, mid in machine_rows:
        machines.setdefault(int(rid), set()).add(int(mid))

    weekdays: dict[int, set[int]] = {}
    for rid, weekday in weekday_rows:
        weekdays.setdefault(int(rid), set()).add(int(weekday))

    now = datetime.now(TZ)
    banned_tg_ids = set()
    for tg_id, banned_until in banned_rows:
        if not banned_until:
            continue
        try:
            until = datetime.fromisoformat(str(banned_until))
            if until.tzinfo is None:
                until = until.replace(tzinfo=TZ)
            if until > now:
                banned_tg_ids.add(int(tg_id))
        except Exception:
            banned_tg_ids.add(int(tg_id))

    held_user_ids = {int(r[0]) for r in held_user_rows}
    today_iso = now.date().isoformat()
    users_with_future_wash = set()
    for uid, date_value, booked_hour in future_wash_rows:
        ds = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
        if ds > today_iso or (ds == today_iso and int(booked_hour) > now.hour):
            users_with_future_wash.add(int(uid))

    result = []
    for rid, uid, tg, mode, any_machine, priority_since in rows:
        if int(tg) <= 0 or int(tg) in banned_tg_ids:
            continue
        if int(uid) in held_user_ids:
            continue
        if int(uid) in users_with_future_wash:
            continue

        request_schedule = schedules.get(int(rid), {})
        request_weekdays = weekdays.get(int(rid), set())
        if request_schedule:
            keys = set(request_schedule)
            request_weekdays = set() if keys == set(range(7)) else keys

        result.append(Request(
            int(rid),
            int(uid),
            int(tg),
            str(mode),
            bool(any_machine),
            _dt(priority_since),
            intervals.get(int(rid), []),
            machines.get(int(rid), set()),
            request_weekdays,
            request_schedule,
        ))

    penalties = usage_penalties_for_users([x.user_id for x in result])
    for req in result:
        req.usage_points = int(penalties.get(req.user_id, 0))
    return result


def _free_slots(date_iso: str) -> list[tuple[int, int]]:
    with get_conn() as conn:
        machines = conn.execute(
            "SELECT id FROM machines WHERE type='wash' AND is_active ORDER BY id"
        ).fetchall()

    now = datetime.now(TZ)
    today_iso = now.date().isoformat()
    slots = []
    for (mid,) in machines:
        hours = get_free_hours_effective(int(mid), str(date_iso))
        if str(date_iso) == today_iso:
            hours = [hour for hour in hours if int(hour) > now.hour]
        for hour in hours:
            slots.append((int(mid), int(hour)))
    return slots


def public_waitlist_dates(now: datetime | None = None) -> list[str]:
    """Dates already visible to ordinary booking right now."""
    now = now or datetime.now(TZ)
    if now.hour >= 23:
        offsets = (1, 2)
    else:
        offsets = (0, 1, 2)
    return [(now.date() + timedelta(days=offset)).isoformat() for offset in offsets]


async def check_active_waitlist() -> int:
    """Fallback polling for slots that were already free before a request appeared."""
    if not WAITLIST_ENABLED:
        return 0

    with get_conn() as conn:
        active = conn.execute(
            "SELECT 1 FROM waitlist_requests WHERE status='active' AND persistent=1 LIMIT 1"
        ).fetchone()
    if not active:
        return 0

    matched = 0
    for date_iso in public_waitlist_dates():
        matched += await distribute_date(date_iso, context="day")
    return matched


def _match(
    requests: list[Request],
    slots: list[tuple[int, int]],
    date_iso: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[int, tuple[int, int]]:
    now = now or datetime.now(TZ)
    allowed: dict[int, list[tuple[int, int]]] = {}
    for req in requests:
        opts = [
            slot
            for slot in slots
            if req.accepts_machine(slot[0]) and req.accepts_hour(slot[1], date_iso)
        ]
        if opts:
            allowed[req.id] = opts

    order = sorted(
        [r for r in requests if r.id in allowed],
        # Fairness is the primary criterion: users who have waited longer and
        # washed less recently must not be overtaken only because another
        # request has fewer compatible slots. The augmenting-path matcher
        # below already rearranges assignments to preserve maximum occupancy.
        key=lambda r: (-r.queue_score(now), len(allowed[r.id]), r.priority_since),
    )

    slot_owner: dict[tuple[int, int], int] = {}
    request_slot: dict[int, tuple[int, int]] = {}

    def augment(rid: int, seen: set[tuple[int, int]]) -> bool:
        for slot in sorted(allowed.get(rid, []), key=lambda x: (x[1], x[0])):
            if slot in seen:
                continue
            seen.add(slot)
            current = slot_owner.get(slot)
            if current is None or augment(current, seen):
                slot_owner[slot] = rid
                request_slot[rid] = slot
                return True
        return False

    for req in order:
        augment(req.id, set())
    return request_slot


def _quiet_for(user_id: int) -> bool:
    cfg = get_notification_settings(user_id)
    if not cfg["quiet_enabled"]:
        return False
    h = datetime.now(TZ).hour
    start, end = cfg["quiet_start"], cfg["quiet_end"]
    return h >= start or h < end if start > end else start <= h < end


async def _send_dryer_offer(tg_id: int, result) -> None:
    if BOT is None or result.machine_type != "wash":
        return
    offer = find_next_dryer(result.user_id, result.date, result.hour)
    if not offer:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Да, добавить сушку",
            callback_data=f"auto_dry_{offer.machine_id}_{offer.date}_{offer.hour}",
        ),
        InlineKeyboardButton(
            text="❌ Нет, не добавлять",
            callback_data="auto_dry_cancel",
        ),
    ]])
    try:
        await BOT.send_message(
            int(tg_id),
            "🌬️ <b>Нужна сушка после стирки?</b>\n\n"
            f"Свободна <b>{offer.machine_name}</b>\n"
            f"сразу после вашей стирки, в {offer.hour:02d}:00.",
            parse_mode="HTML",
            reply_markup=kb,
            disable_notification=_quiet_for(result.user_id),
        )
    except Exception:
        pass


async def _schedule_booking_features(result) -> None:
    with get_conn() as conn:
        tg = conn.execute("SELECT tg_id FROM users WHERE id=?", (result.user_id,)).fetchone()
    if not tg or int(tg[0]) <= 0:
        return
    from handlers.laundry_features import schedule_reminder
    await schedule_reminder(int(tg[0]), result.machine_name, result.date, result.hour, 30)


async def _send_auto_confirmation(req: Request, result, *, night: bool) -> None:
    if BOT is None:
        return
    text = (
        "✅ <b>Вы записаны</b>\n\n"
        f"📅 {pretty_date(result.date)}\n"
        f"🕐 {result.hour:02d}:00\n"
        f"🧺 {result.machine_name}\n\n"
        "Бот нашёл подходящее время через лист ожидания."
    )
    if night:
        tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(hour=0, minute=0, second=2, microsecond=0)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO pending_waitlist_notifications
                (user_id,booking_id,text,send_at,sent,created_at)
                VALUES (?,?,?,?,0,?)
                """,
                (
                    req.user_id,
                    result.booking_id,
                    text,
                    tomorrow.isoformat(timespec="seconds"),
                    datetime.now(TZ).isoformat(timespec="seconds"),
                ),
            )
        return
    try:
        await BOT.send_message(
            req.tg_id,
            text,
            parse_mode="HTML",
            disable_notification=_quiet_for(req.user_id),
            reply_markup=build_main_menu(False),
        )
        await _send_dryer_offer(req.tg_id, result)
    except Exception:
        pass


async def _reserve_night_hold(
    req: Request,
    machine_id: int,
    date_iso: str,
    hour: int,
    hold_run_at: datetime,
) -> int | None:
    """
    Persist a fair night-round HOLD winner without notifying yet.

    The row is status=active immediately, so the slot is protected from normal
    booking and redistribution across Render restarts. The user gets the actual
    response window only when the second phase activates it.
    """
    expires = hold_run_at + timedelta(minutes=HOLD_MINUTES)
    now = datetime.now(TZ)
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT id FROM slot_holds
            WHERE user_id=? AND status='active'
            LIMIT 1
            """,
            (req.user_id,),
        ).fetchone()
        if existing:
            return int(existing[0])

        cur = conn.execute(
            """
            INSERT INTO slot_holds
            (request_id,user_id,machine_id,date,hour,expires_at,context,status,created_at)
            VALUES (?,?,?,?,?,?,'night_pending','active',?)
            ON CONFLICT DO NOTHING
            """,
            (
                req.id,
                req.user_id,
                int(machine_id),
                str(date_iso),
                int(hour),
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        hold_id = getattr(cur, "lastrowid", None)
        if hold_id:
            return int(hold_id)

        row = conn.execute(
            """
            SELECT id FROM slot_holds
            WHERE request_id=? AND machine_id=? AND date=? AND hour=?
              AND status='active'
            ORDER BY id DESC LIMIT 1
            """,
            (req.id, int(machine_id), str(date_iso), int(hour)),
        ).fetchone()
        return int(row[0]) if row else None


async def _activate_pending_night_hold(hold_id: int) -> bool:
    if BOT is None:
        return False

    now = datetime.now(TZ)
    expires = now + timedelta(minutes=HOLD_MINUTES)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT sh.request_id,sh.user_id,sh.machine_id,sh.date,sh.hour,
                   u.tg_id,m.name
            FROM slot_holds sh
            JOIN users u ON u.id=sh.user_id
            JOIN machines m ON m.id=sh.machine_id
            WHERE sh.id=? AND sh.status='active' AND sh.context='night_pending'
            """,
            (int(hold_id),),
        ).fetchone()
        if not row:
            return False

        request_id,user_id,machine_id,date_iso,hour,tg_id,machine_name = row
        conn.execute(
            """
            UPDATE slot_holds
            SET context='night',expires_at=?
            WHERE id=? AND status='active' AND context='night_pending'
            """,
            (expires.isoformat(timespec="seconds"), int(hold_id)),
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записаться", callback_data=f"wl_accept_{int(hold_id)}"),
        InlineKeyboardButton(text="❌ Пропустить", callback_data=f"wl_decline_{int(hold_id)}"),
    ]])
    text = (
        "🔔 <b>Для вас найдено место</b>\n\n"
        f"📅 {pretty_date(str(date_iso))}\n"
        f"🕐 {int(hour):02d}:00\n"
        f"🧺 {machine_name}\n\n"
        + _hold_deadline_text(expires, HOLD_MINUTES)
    )
    try:
        await BOT.send_message(
            int(tg_id),
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_notification=_quiet_for(int(user_id)),
        )
        _schedule_hold_expiry(int(hold_id), expires)
        await asyncio.sleep(0.04)
        return True
    except Exception:
        with get_conn() as conn:
            conn.execute(
                "UPDATE slot_holds SET status='expired' WHERE id=?",
                (int(hold_id),),
            )
        return False


async def _create_hold(
    req: Request,
    machine_id: int,
    date_iso: str,
    hour: int,
    context: str,
    *,
    urgent_auto: bool = False,
) -> None:
    if BOT is None:
        return

    now = datetime.now(TZ)
    minutes = _hold_duration_minutes(date_iso, hour, now)
    if minutes is None:
        return False
    expires = now + timedelta(minutes=minutes)

    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT 1 FROM slot_holds
            WHERE user_id=? AND status='active' AND expires_at>?
            LIMIT 1
            """,
            (req.user_id, now.isoformat(timespec="seconds")),
        ).fetchone()
        if existing:
            return
        machine = conn.execute("SELECT name FROM machines WHERE id=?", (machine_id,)).fetchone()
        if not machine:
            return
        cur = conn.execute(
            """
            INSERT INTO slot_holds
            (request_id,user_id,machine_id,date,hour,expires_at,context,status,created_at)
            VALUES (?,?,?,?,?,?,?,'active',?)
            ON CONFLICT DO NOTHING
            """,
            (
                req.id, req.user_id, machine_id, date_iso, hour,
                expires.isoformat(timespec="seconds"), context,
                now.isoformat(timespec="seconds"),
            ),
        )
        hold_id = getattr(cur, "lastrowid", None)
        if not hold_id:
            row = conn.execute(
                """
                SELECT id FROM slot_holds
                WHERE request_id=? AND machine_id=? AND date=? AND hour=? AND status='active'
                ORDER BY id DESC LIMIT 1
                """,
                (req.id, machine_id, date_iso, hour),
            ).fetchone()
            if not row:
                # Another worker/process won the same slot or the same user
                # already received another HOLD. The database constraint is
                # the final authority, so simply skip this stale match.
                return
            hold_id = int(row[0])

    hold_text = _hold_deadline_text(expires, minutes)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записаться", callback_data=f"wl_accept_{hold_id}"),
        InlineKeyboardButton(text="❌ Пропустить", callback_data=f"wl_decline_{hold_id}"),
    ]])
    if urgent_auto:
        text = (
            "⚡ <b>Освободилось место прямо сейчас</b>\n\n"
            f"🕐 {hour:02d}:00, {machine[0]}\n\n"
            "До начала осталось мало времени, поэтому бот не стал записывать вас автоматически.\n"
            f"{hold_text}"
        )
    else:
        text = (
            "🔔 <b>Для вас найдено место</b>\n\n"
            f"📅 {pretty_date(date_iso)}\n"
            f"🕐 {hour:02d}:00\n"
            f"🧺 {machine[0]}\n\n"
            f"{hold_text}"
        )
    try:
        await BOT.send_message(
            req.tg_id, text, parse_mode="HTML", reply_markup=kb,
            disable_notification=_quiet_for(req.user_id),
        )
        _schedule_hold_expiry(int(hold_id), expires)
        await asyncio.sleep(0.04)
    except Exception:
        with get_conn() as conn:
            conn.execute("UPDATE slot_holds SET status='expired' WHERE id=?", (hold_id,))


async def distribute_date(
    date_iso: str,
    *,
    context: str = "day",
    phase: str = "all",
) -> int:
    if not WAITLIST_ENABLED:
        return 0
    async with _DISTRIBUTION_LOCK:
        return await _distribute_date_locked(date_iso, context=context, phase=phase)


async def _distribute_date_locked(
    date_iso: str,
    *,
    context: str = "day",
    phase: str = "all",
) -> int:
    # Keep 30-day fairness data current only when matching is actually needed.
    record_usage_history()
    cutoff_at = None
    if context == "night":
        with get_conn() as conn:
            round_row = conn.execute(
                "SELECT cutoff_at FROM waitlist_rounds WHERE target_date=?",
                (str(date_iso),),
            ).fetchone()
        cutoff_at = str(round_row[0]) if round_row else None
    requests = _active_requests(cutoff_at)
    recent_cutoff = (datetime.now(TZ) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with get_conn() as conn:
        recent_rows = conn.execute(
            """
            SELECT DISTINCT request_id
            FROM waitlist_offer_history
            WHERE date=? AND result IN ('declined','expired','cancelled_booking') AND created_at>=?
            """,
            (str(date_iso), recent_cutoff),
        ).fetchall()
    recent_requests = {int(r[0]) for r in recent_rows if r[0] is not None}
    requests = [
        r for r in requests
        if r.id not in recent_requests and r.accepts_date(date_iso)
    ]
    # Under five minutes a fresh offer cannot be acted on safely.
    now_for_slots = datetime.now(TZ)
    slots = [
        (mid, hour) for mid, hour in _free_slots(date_iso)
        if _hold_duration_minutes(date_iso, hour, now_for_slots) is not None
    ]
    if not slots:
        return 0
    matches = _match(requests, slots, date_iso=date_iso) if requests else {}
    by_id = {r.id: r for r in requests}
    count = 0
    for rid, (mid, hour) in matches.items():
        req = by_id[rid]

        wants_auto = req.mode == "auto" and _auto_booking_allowed(date_iso, hour)
        if phase == "auto" and not wants_auto:
            continue
        if phase == "holds" and wants_auto:
            continue

        if wants_auto:
            try:
                result = await create_booking_safe(req.user_id, mid, date_iso, hour)
            except BookingError:
                continue
            await _schedule_booking_features(result)
            await _send_auto_confirmation(req, result, night=context == "night")
            count += 1
        else:
            await _create_hold(
                req,
                mid,
                date_iso,
                hour,
                context,
                urgent_auto=req.mode == "auto",
            )
            count += 1

    if context == "day":
        await offer_earlier_for_date(date_iso)
    return count


async def process_night_round() -> None:
    """
    Fair night round in two execution phases.

    1) One global matching decides winners across AUTO and notify together.
       AUTO winners are booked immediately. Notify winners get durable pending
       reservations in Neon, but no message/timer yet.
    2) About 90 seconds later pending reservations are activated as real HOLDs.

    This preserves fairness across modes while remaining restart-safe.
    """
    if not WAITLIST_ENABLED:
        return

    now = datetime.now(TZ)
    target = (now.date() + timedelta(days=3)).isoformat()
    cutoff = now.replace(hour=23, minute=0, second=0, microsecond=0)
    hold_run_at = cutoff + timedelta(seconds=90)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT status,cutoff_at FROM waitlist_rounds WHERE target_date=?",
            (target,),
        ).fetchone()

    if row:
        status = str(row[0])
        stored_cutoff = _dt(row[1]) if row[1] else cutoff
        if status == "finished":
            return
        if status in {"holds_pending", "holds_running"}:
            _schedule_night_hold_phase(
                target,
                max(stored_cutoff + timedelta(seconds=90), now + timedelta(seconds=1)),
            )
            return
        cutoff = stored_cutoff
        hold_run_at = cutoff + timedelta(seconds=90)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO waitlist_rounds(target_date,cutoff_at,started_at,status)
            VALUES (?,?,?,'matching')
            ON CONFLICT(target_date) DO UPDATE SET
                cutoff_at=excluded.cutoff_at,
                started_at=COALESCE(waitlist_rounds.started_at, excluded.started_at),
                status='matching'
            """,
            (
                target,
                cutoff.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )

    record_usage_history()
    requests = [r for r in _active_requests(cutoff.isoformat(timespec="seconds")) if r.accepts_date(target)]
    slots = _free_slots(target)
    matches = _match(requests, slots, date_iso=target) if requests else {}
    by_id = {r.id: r for r in requests}

    for rid, (mid, hour) in matches.items():
        req = by_id[rid]
        if req.mode == "auto" and _auto_booking_allowed(target, hour):
            try:
                result = await create_booking_safe(req.user_id, mid, target, hour)
            except BookingError:
                continue
            await _schedule_booking_features(result)
            await _send_auto_confirmation(req, result, night=True)
        else:
            await _reserve_night_hold(req, mid, target, hour, hold_run_at)

    with get_conn() as conn:
        conn.execute(
            "UPDATE waitlist_rounds SET status='holds_pending' WHERE target_date=?",
            (target,),
        )

    _schedule_night_hold_phase(target, hold_run_at)


async def process_night_hold_phase(date_iso: str) -> None:
    if not WAITLIST_ENABLED:
        return

    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM waitlist_rounds WHERE target_date=?",
            (str(date_iso),),
        ).fetchone()
        if row and str(row[0]) == "finished":
            return
        conn.execute(
            "UPDATE waitlist_rounds SET status='holds_running' WHERE target_date=?",
            (str(date_iso),),
        )
        pending = conn.execute(
            """
            SELECT id
            FROM slot_holds
            WHERE date=? AND status='active' AND context='night_pending'
            ORDER BY id
            """,
            (str(date_iso),),
        ).fetchall()

    for (hold_id,) in pending:
        await _activate_pending_night_hold(int(hold_id))

    # Fill any slots left genuinely free because a winner became invalid or a
    # pending offer could not be delivered. At this point the fair winners are
    # already protected/booked, so normal matching is safe for leftovers.
    await distribute_date(str(date_iso), context="night", phase="all")

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE waitlist_rounds
            SET status='finished',finished_at=?
            WHERE target_date=?
            """,
            (
                datetime.now(TZ).isoformat(timespec="seconds"),
                str(date_iso),
            ),
        )

    _remove_job(_night_hold_job_id(str(date_iso)))
    schedule_next_pending_notification()


async def expire_hold(hold_id: int) -> bool:
    """Expire one HOLD at its exact deadline and redistribute its slot."""
    row = get_hold(int(hold_id))
    if not row or str(row[11]) != "active":
        _cancel_hold_expiry(int(hold_id))
        return False

    now = datetime.now(TZ)
    expires_at = _dt(row[8])
    if expires_at > now:
        _schedule_hold_expiry(int(hold_id), expires_at)
        return False

    request_id = row[1]
    machine_id = int(row[4])
    date_iso = str(row[6])
    hour = int(row[7])
    context = str(row[9])

    with get_conn() as conn:
        conn.execute(
            "UPDATE slot_holds SET status='expired' WHERE id=? AND status='active'",
            (int(hold_id),),
        )
        if request_id:
            conn.execute(
                """
                INSERT INTO waitlist_offer_history
                (request_id,machine_id,date,hour,result,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    int(request_id), machine_id, date_iso, hour, "expired",
                    now.isoformat(timespec="seconds"),
                ),
            )

    _cancel_hold_expiry(int(hold_id))

    # Night priority is not restarted after midnight. Day/move slots can be
    # redistributed immediately because their HOLD has just disappeared.
    if context == "night":
        if now.hour == 23:
            await distribute_date(date_iso, context="night")
    elif context in {"day", "move"}:
        await distribute_date(date_iso, context="day")

    return True


async def expire_holds() -> int:
    """Recovery-only batch helper; no longer scheduled periodically."""
    now_s = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id FROM slot_holds
            WHERE status='active' AND expires_at<=?
            ORDER BY expires_at
            """,
            (now_s,),
        ).fetchall()

    expired = 0
    for (hold_id,) in rows:
        if await expire_hold(int(hold_id)):
            expired += 1
    return expired


async def send_pending_notifications() -> int:
    if BOT is None:
        return 0
    now_s = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id,p.user_id,u.tg_id,p.text
            FROM pending_waitlist_notifications p
            JOIN users u ON u.id=p.user_id
            WHERE p.sent=0 AND p.send_at<=?
            ORDER BY p.id
            """,
            (now_s,),
        ).fetchall()
    sent = 0
    for pid, uid, tg, text in rows:
        with get_conn() as conn:
            booking_row = conn.execute(
                "SELECT booking_id FROM pending_waitlist_notifications WHERE id=?",
                (int(pid),),
            ).fetchone()
            booking_id = int(booking_row[0]) if booking_row and booking_row[0] else None
            exists = (
                conn.execute("SELECT 1 FROM bookings WHERE id=?", (booking_id,)).fetchone()
                if booking_id else None
            )
        if booking_id and not exists:
            with get_conn() as conn:
                conn.execute("UPDATE pending_waitlist_notifications SET sent=1 WHERE id=?", (int(pid),))
            continue
        try:
            await BOT.send_message(
                int(tg), str(text), parse_mode="HTML",
                disable_notification=_quiet_for(int(uid)),
                reply_markup=build_main_menu(False),
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.2)
            try:
                await BOT.send_message(
                    int(tg), str(text), parse_mode="HTML",
                    disable_notification=_quiet_for(int(uid)),
                    reply_markup=build_main_menu(False),
                )
            except Exception:
                continue
        except Exception:
            continue
        with get_conn() as conn:
            conn.execute("UPDATE pending_waitlist_notifications SET sent=1 WHERE id=?", (int(pid),))
            booking = (
                conn.execute(
                    """
                    SELECT b.id,b.user_id,b.machine_id,m.type,m.name,b.date,b.hour
                    FROM bookings b
                    JOIN machines m ON m.id=b.machine_id
                    WHERE b.id=?
                    """,
                    (booking_id,),
                ).fetchone()
                if booking_id else None
            )
        if booking:
            class _BookingResult:
                pass
            result = _BookingResult()
            result.booking_id = int(booking[0])
            result.user_id = int(booking[1])
            result.machine_id = int(booking[2])
            result.machine_type = str(booking[3])
            result.machine_name = str(booking[4])
            result.date = booking[5].isoformat() if hasattr(booking[5], "isoformat") else str(booking[5])
            result.hour = int(booking[6])
            await _send_dryer_offer(int(tg), result)
        sent += 1
        await asyncio.sleep(0.04)
    schedule_next_pending_notification()
    return sent


def get_hold(hold_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT h.id,h.request_id,h.user_id,u.tg_id,h.machine_id,m.name,
                   h.date,h.hour,h.expires_at,h.context,h.current_booking_id,h.status
            FROM slot_holds h
            JOIN users u ON u.id=h.user_id
            JOIN machines m ON m.id=h.machine_id
            WHERE h.id=?
            """,
            (int(hold_id),),
        ).fetchone()


async def decline_hold(hold_id: int, tg_id: int) -> bool:
    row = get_hold(hold_id)
    if not row or int(row[3]) != int(tg_id) or str(row[11]) != "active":
        return False
    with get_conn() as conn:
        conn.execute("UPDATE slot_holds SET status='declined' WHERE id=?", (int(hold_id),))
        if row[1]:
            conn.execute(
                """
                INSERT INTO waitlist_offer_history(request_id,machine_id,date,hour,result,created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    int(row[1]), int(row[4]), str(row[6]), int(row[7]),
                    "declined", datetime.now(TZ).isoformat(timespec="seconds"),
                ),
            )
    _cancel_hold_expiry(int(hold_id))
    if str(row[9]) == "night" and datetime.now(TZ).hour == 23:
        await distribute_date(str(row[6]), context="night")
    elif str(row[9]) in {"day", "move"}:
        await distribute_date(str(row[6]), context="day")
    return True


async def accept_hold(hold_id: int, tg_id: int):
    row = get_hold(hold_id)
    if not row or int(row[3]) != int(tg_id):
        return None

    status = str(row[11])
    if status == "accepted":
        with get_conn() as conn:
            booking_row = conn.execute(
                """
                SELECT id FROM bookings
                WHERE user_id=? AND machine_id=? AND date=? AND hour=?
                ORDER BY id DESC LIMIT 1
                """,
                (int(row[2]), int(row[4]), str(row[6]), int(row[7])),
            ).fetchone()
        return get_booking(int(booking_row[0])) if booking_row else None

    if status != "active":
        return None

    if is_banned(int(tg_id)):
        with get_conn() as conn:
            conn.execute("UPDATE slot_holds SET status='cancelled' WHERE id=?", (int(hold_id),))
        return None
    if _dt(row[8]) <= datetime.now(TZ):
        await expire_hold(int(hold_id))
        return None

    from booking_service import move_booking_safe
    if str(row[9]) == "move" and row[10]:
        old, new = await move_booking_safe(
            int(row[10]), int(row[4]), str(row[6]), int(row[7]),
            allowed_hold_id=int(hold_id),
        )
        await _schedule_booking_features(new)
        with get_conn() as conn:
            conn.execute(
                "UPDATE waitlist_requests SET matched_booking_id=?,updated_at=? WHERE id=?",
                (
                    int(new.booking_id),
                    datetime.now(TZ).isoformat(timespec="seconds"),
                    int(row[1]),
                ),
            )
        _schedule_subscription_resume(
            int(row[1]), int(new.booking_id), str(new.date), int(new.hour)
        )
        _cancel_hold_expiry(int(hold_id))
        await distribute_date(old.date, context="day")
        return new

    try:
        result = await create_booking_safe(
            int(row[2]), int(row[4]), str(row[6]), int(row[7]),
            allowed_hold_id=int(hold_id),
        )
    except BookingError:
        return None
    _cancel_hold_expiry(int(hold_id))
    await _schedule_booking_features(result)
    return result


async def offer_earlier_for_date(date_iso: str) -> int:
    if BOT is None or not WAITLIST_ENABLED:
        return 0
    now_for_slots = datetime.now(TZ)
    slots = [
        (mid, hour) for mid, hour in _free_slots(date_iso)
        if _hold_duration_minutes(date_iso, hour, now_for_slots) is not None
    ]
    if not slots:
        return 0

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT wr.id,wr.user_id,u.tg_id,wr.any_machine,wr.priority_since,
                   wr.matched_booking_id,b.date
            FROM waitlist_requests wr
            JOIN users u ON u.id=wr.user_id
            JOIN bookings b ON b.id=wr.matched_booking_id
            LEFT JOIN notification_settings ns ON ns.user_id=wr.user_id
            WHERE wr.status='matched'
              AND b.date>?
              AND COALESCE(ns.earlier_offer_enabled,1)=1
            ORDER BY wr.priority_since
            """,
            (str(date_iso),),
        ).fetchall()
        interval_rows = conn.execute(
            "SELECT request_id,start_hour,end_hour FROM waitlist_intervals"
        ).fetchall()
        schedule_rows = conn.execute(
            "SELECT request_id,weekday,start_hour,end_hour FROM waitlist_schedule ORDER BY request_id,weekday,start_hour"
        ).fetchall()
        machine_rows = conn.execute(
            "SELECT request_id,machine_id FROM waitlist_machines"
        ).fetchall()
        weekday_rows = conn.execute(
            "SELECT request_id,weekday FROM waitlist_weekdays"
        ).fetchall()

    intervals = {}
    for rid, a, b in interval_rows:
        intervals.setdefault(int(rid), []).append((int(a), int(b)))
    schedules = {}
    for rid, weekday, a, b in schedule_rows:
        schedules.setdefault(int(rid), {}).setdefault(int(weekday), []).append(
            (int(a), int(b))
        )
    machines = {}
    for rid, mid in machine_rows:
        machines.setdefault(int(rid), set()).add(int(mid))
    weekdays = {}
    for rid, weekday in weekday_rows:
        weekdays.setdefault(int(rid), set()).add(int(weekday))

    recent_cutoff = (datetime.now(TZ) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with get_conn() as conn:
        recent_rows = conn.execute(
            """
            SELECT DISTINCT request_id FROM waitlist_offer_history
            WHERE date=? AND result IN ('declined','expired') AND created_at>=?
            """,
            (str(date_iso), recent_cutoff),
        ).fetchall()
    recent_requests = {int(r[0]) for r in recent_rows if r[0] is not None}

    candidates = []
    for rid, uid, tg, any_machine, priority_since, booking_id, current_date in rows:
        if int(tg) <= 0 or is_banned(int(tg)):
            continue
        if int(rid) in recent_requests:
            continue
        if active_hold_for_user(int(uid)):
            continue
        request_schedule = schedules.get(int(rid), {})
        request_weekdays = weekdays.get(int(rid), set())
        if request_schedule:
            keys = set(request_schedule)
            request_weekdays = set() if keys == set(range(7)) else keys
        candidate = Request(
            int(rid), int(uid), int(tg), "notify", bool(any_machine),
            _dt(priority_since), intervals.get(int(rid), []),
            machines.get(int(rid), set()),
            request_weekdays,
            request_schedule,
        )
        if not candidate.accepts_date(date_iso):
            continue
        candidates.append(candidate)
        candidates[-1].current_booking_id = int(booking_id)

    penalties = usage_penalties_for_users([c.user_id for c in candidates])
    for c in candidates:
        c.usage_points = int(penalties.get(c.user_id, 0))

    now = datetime.now(TZ)
    used_users = set()
    offered = 0
    for mid, hour in sorted(slots, key=lambda x: (x[1], x[0])):
        compatible = [
            c for c in candidates
            if c.id not in used_users
            and c.accepts_machine(mid)
            and c.accepts_hour(hour, date_iso)
        ]
        if not compatible:
            continue
        compatible.sort(key=lambda c: (-c.queue_score(now), c.priority_since))
        req = compatible[0]
        with get_conn() as conn:
            rejected = conn.execute(
                """
                SELECT 1 FROM waitlist_offer_history
                WHERE request_id=? AND machine_id=? AND date=? AND hour=?
                  AND result IN ('declined','expired')
                LIMIT 1
                """,
                (req.id, mid, str(date_iso), int(hour)),
            ).fetchone()
        if rejected:
            continue
        if await _create_move_hold(req, mid, date_iso, hour, req.current_booking_id):
            used_users.add(req.id)
            offered += 1
    return offered


async def _create_move_hold(
    req: Request,
    machine_id: int,
    date_iso: str,
    hour: int,
    current_booking_id: int,
) -> bool:
    if BOT is None:
        return False
    now = datetime.now(TZ)
    minutes = _hold_duration_minutes(date_iso, hour, now)
    if minutes is None:
        return False
    expires = now + timedelta(minutes=minutes)
    with get_conn() as conn:
        machine = conn.execute("SELECT name FROM machines WHERE id=?", (int(machine_id),)).fetchone()
        current = conn.execute(
            "SELECT m.name,b.date,b.hour FROM bookings b JOIN machines m ON m.id=b.machine_id WHERE b.id=?",
            (int(current_booking_id),),
        ).fetchone()
        if not machine or not current:
            return
        cur = conn.execute(
            """
            INSERT INTO slot_holds
            (request_id,user_id,machine_id,date,hour,expires_at,context,status,current_booking_id,created_at)
            VALUES (?,?,?,?,?,?,'move','active',?,?)
            """,
            (
                req.id, req.user_id, int(machine_id), str(date_iso), int(hour),
                expires.isoformat(timespec="seconds"), int(current_booking_id),
                datetime.now(TZ).isoformat(timespec="seconds"),
            ),
        )
        hold_id = getattr(cur, "lastrowid", None)
        if not hold_id:
            hold_id = int(conn.execute(
                "SELECT id FROM slot_holds WHERE request_id=? AND context='move' AND status='active' ORDER BY id DESC LIMIT 1",
                (req.id,),
            ).fetchone()[0])

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Перенести", callback_data=f"wl_accept_{hold_id}"),
        InlineKeyboardButton(text="Оставить как есть", callback_data=f"wl_decline_{hold_id}"),
    ]])
    text = (
        "🔄 <b>Можно перенести стирку раньше</b>\n\n"
        f"Сейчас: {pretty_date(str(current[1]))}, {int(current[2]):02d}:00, {current[0]}\n"
        f"Освободилось: {pretty_date(date_iso)}, {int(hour):02d}:00, {machine[0]}\n\n"
        + _hold_deadline_text(expires, minutes)
    )
    try:
        await BOT.send_message(
            req.tg_id, text, parse_mode="HTML", reply_markup=kb,
            disable_notification=_quiet_for(req.user_id),
        )
        _schedule_hold_expiry(int(hold_id), expires)
        return True
    except Exception:
        with get_conn() as conn:
            conn.execute("UPDATE slot_holds SET status='expired' WHERE id=?", (int(hold_id),))
        return False
