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

HOLD_MINUTES = 2
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
            SELECT id,expires_at
            FROM slot_holds
            WHERE status='active'
            ORDER BY expires_at
            """
        ).fetchall()

    for hold_id, expires_at in holds:
        if _dt(expires_at) <= now:
            await expire_hold(int(hold_id))
        else:
            _schedule_hold_expiry(int(hold_id), expires_at)

    schedule_next_pending_notification()
    await check_active_waitlist()


def get_active_request_for_tg(tg_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT wr.id,wr.mode,wr.any_machine,wr.created_at,wr.priority_since
            FROM waitlist_requests wr
            JOIN users u ON u.id=wr.user_id
            WHERE u.tg_id=? AND wr.status='active'
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
        row = conn.execute(
            """
            SELECT wr.id FROM waitlist_requests wr
            JOIN users u ON u.id=wr.user_id
            WHERE u.tg_id=? AND wr.status='active'
            LIMIT 1
            """,
            (int(tg_id),),
        ).fetchone()
        if not row:
            return False
        hold_rows = conn.execute(
            "SELECT id FROM slot_holds WHERE request_id=? AND status='active'",
            (int(row[0]),),
        ).fetchall()
        conn.execute(
            "UPDATE waitlist_requests SET status='cancelled',updated_at=? WHERE id=?",
            (now, int(row[0])),
        )
        conn.execute(
            "UPDATE slot_holds SET status='cancelled' WHERE request_id=? AND status='active'",
            (int(row[0]),),
        )
    for (hold_id,) in hold_rows:
        _cancel_hold_expiry(int(hold_id))
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

        now_date = now.date().isoformat()
        future_rows = conn.execute(
            """
            SELECT b.date,b.hour
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE b.user_id=? AND m.type='wash'
              AND b.date>=?
            """,
            (user_id, now_date),
        ).fetchall()
        for date_value, booked_hour in future_rows:
            ds = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
            if ds > now_date or (ds == now_date and int(booked_hour) > now.hour):
                raise ValueError(
                    "У вас уже есть будущая запись на стирку. Новую заявку можно создать после неё или после отмены."
                )

        old = conn.execute(
            "SELECT id,mode,any_machine,priority_since FROM waitlist_requests WHERE user_id=? AND status='active'",
            (user_id,),
        ).fetchone()
        now_s = now.isoformat(timespec="seconds")
        hold_rows = []

        if old:
            request_id = int(old[0])
            old_machines = {
                int(r[0])
                for r in conn.execute(
                    "SELECT machine_id FROM waitlist_machines WHERE request_id=?",
                    (request_id,),
                ).fetchall()
            }
            new_machines = set() if any_machine else {int(x) for x in machine_ids}

            # Editing days/hours or switching AUTO/notify must not erase waiting
            # time. Only changing the machine eligibility changes queue priority.
            machine_conditions_changed = (
                bool(old[2]) != bool(any_machine)
                or old_machines != new_machines
            )
            priority_since = now_s if machine_conditions_changed else str(old[3])

            conn.execute(
                """
                UPDATE waitlist_requests
                SET mode=?,any_machine=?,priority_since=?,updated_at=?
                WHERE id=?
                """,
                (mode, int(bool(any_machine)), priority_since, now_s, request_id),
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
            cur = conn.execute(
                """
                INSERT INTO waitlist_requests
                (user_id,mode,status,any_machine,created_at,priority_since,updated_at)
                VALUES (?,?,'active',?,?,?,?)
                """,
                (user_id, mode, int(bool(any_machine)), now_s, now_s, now_s),
            )
            request_id = getattr(cur, "lastrowid", None)
            if not request_id:
                request_id = int(conn.execute(
                    "SELECT id FROM waitlist_requests WHERE user_id=? AND status='active'",
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
    return int(request_id)


def _active_requests(cutoff_at: str | None = None) -> list[Request]:
    with get_conn() as conn:
        if cutoff_at:
            rows = conn.execute(
                """
                SELECT wr.id,wr.user_id,u.tg_id,wr.mode,wr.any_machine,wr.priority_since
                FROM waitlist_requests wr
                JOIN users u ON u.id=wr.user_id
                WHERE wr.status='active' AND wr.priority_since<=?
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
                WHERE wr.status='active'
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
            "SELECT 1 FROM waitlist_requests WHERE status='active' LIMIT 1"
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
) -> dict[int, tuple[int, int]]:
    now = datetime.now(TZ)
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
    slot_start = _slot_start(date_iso, hour)
    expires = min(now + timedelta(minutes=HOLD_MINUTES), slot_start)
    if expires <= now:
        return

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
            """,
            (
                req.id, req.user_id, machine_id, date_iso, hour,
                expires.isoformat(timespec="seconds"), context,
                now.isoformat(timespec="seconds"),
            ),
        )
        hold_id = getattr(cur, "lastrowid", None)
        if not hold_id:
            hold_id = int(conn.execute(
                """
                SELECT id FROM slot_holds
                WHERE request_id=? AND machine_id=? AND date=? AND hour=? AND status='active'
                ORDER BY id DESC LIMIT 1
                """,
                (req.id, machine_id, date_iso, hour),
            ).fetchone()[0])

    full_hold = expires >= now + timedelta(minutes=HOLD_MINUTES)
    hold_text = (
        f"Слот зарезервирован за вами на {HOLD_MINUTES} минуты."
        if full_hold
        else "Слот зарезервирован за вами до начала стирки."
    )

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


async def distribute_date(date_iso: str, *, context: str = "day") -> int:
    if not WAITLIST_ENABLED:
        return 0
    async with _DISTRIBUTION_LOCK:
        return await _distribute_date_locked(date_iso, context=context)


async def _distribute_date_locked(date_iso: str, *, context: str = "day") -> int:
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
    slots = _free_slots(date_iso)
    if not slots:
        return 0
    matches = _match(requests, slots, date_iso=date_iso) if requests else {}
    by_id = {r.id: r for r in requests}
    count = 0
    for rid, (mid, hour) in matches.items():
        req = by_id[rid]
        if req.mode == "auto" and _auto_booking_allowed(date_iso, hour):
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
    if not WAITLIST_ENABLED:
        return
    now = datetime.now(TZ)
    target = (now.date() + timedelta(days=3)).isoformat()
    cutoff = now.replace(hour=23, minute=0, second=0, microsecond=0)
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM waitlist_rounds WHERE target_date=?", (target,)).fetchone()
        if row and str(row[0]) == "finished":
            return
        conn.execute(
            """
            INSERT INTO waitlist_rounds(target_date,cutoff_at,started_at,status)
            VALUES (?,?,?,'running')
            ON CONFLICT(target_date) DO UPDATE SET started_at=excluded.started_at,status='running'
            """,
            (target, cutoff.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        )
    await distribute_date(target, context="night")
    with get_conn() as conn:
        conn.execute(
            "UPDATE waitlist_rounds SET status='finished',finished_at=? WHERE target_date=?",
            (datetime.now(TZ).isoformat(timespec="seconds"), target),
        )
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
    slots = _free_slots(date_iso)
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
        machine_rows = conn.execute(
            "SELECT request_id,machine_id FROM waitlist_machines"
        ).fetchall()
        weekday_rows = conn.execute(
            "SELECT request_id,weekday FROM waitlist_weekdays"
        ).fetchall()

    intervals = {}
    for rid, a, b in interval_rows:
        intervals.setdefault(int(rid), []).append((int(a), int(b)))
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
        candidate = Request(
            int(rid), int(uid), int(tg), "notify", bool(any_machine),
            _dt(priority_since), intervals.get(int(rid), []),
            machines.get(int(rid), set()),
            weekdays.get(int(rid), set()),
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
            if c.id not in used_users and c.accepts_machine(mid) and c.accepts_hour(hour)
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
        await _create_move_hold(req, mid, date_iso, hour, req.current_booking_id)
        used_users.add(req.id)
        offered += 1
    return offered


async def _create_move_hold(
    req: Request,
    machine_id: int,
    date_iso: str,
    hour: int,
    current_booking_id: int,
) -> None:
    if BOT is None:
        return
    with get_conn() as conn:
        machine = conn.execute("SELECT name FROM machines WHERE id=?", (int(machine_id),)).fetchone()
        current = conn.execute(
            "SELECT m.name,b.date,b.hour FROM bookings b JOIN machines m ON m.id=b.machine_id WHERE b.id=?",
            (int(current_booking_id),),
        ).fetchone()
        if not machine or not current:
            return
        expires = datetime.now(TZ) + timedelta(minutes=HOLD_MINUTES)
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
        f"Новый слот удерживается {HOLD_MINUTES} минуты."
    )
    try:
        await BOT.send_message(
            req.tg_id, text, parse_mode="HTML", reply_markup=kb,
            disable_notification=_quiet_for(req.user_id),
        )
        _schedule_hold_expiry(int(hold_id), expires)
    except Exception:
        with get_conn() as conn:
            conn.execute("UPDATE slot_holds SET status='expired' WHERE id=?", (int(hold_id),))
