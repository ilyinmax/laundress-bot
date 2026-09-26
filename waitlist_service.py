from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import TIMEZONE, WORKING_HOURS
from database import (
    get_conn,
    get_notification_settings,
    usage_penalties_for_users,
    get_free_hours_effective,
    is_banned,
)
from booking_service import create_booking_safe, BookingError

TZ = ZoneInfo(TIMEZONE)
BOT: Bot | None = None
HOLD_MINUTES = 2
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
    usage_points: int = 0

    def accepts_hour(self, hour: int) -> bool:
        return any(start <= hour < end for start, end in self.intervals)

    def accepts_machine(self, machine_id: int) -> bool:
        return self.any_machine or machine_id in self.machines

    def queue_score(self, now: datetime) -> int:
        waiting = min(168, max(0, int((now - self.priority_since).total_seconds() // 3600)))
        return waiting - self.usage_points * 12


def attach_bot(bot: Bot) -> None:
    global BOT
    BOT = bot


def _dt(value) -> datetime:
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


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
        conn.execute(
            "UPDATE waitlist_requests SET status='cancelled',updated_at=? WHERE id=?",
            (now, int(row[0])),
        )
    return True


def save_request(
    tg_id: int,
    intervals: list[tuple[int, int]],
    machine_ids: list[int],
    any_machine: bool,
    mode: str,
) -> int:
    now = datetime.now(TZ)
    if mode not in {"auto", "notify"}:
        raise ValueError("Некорректный режим")
    normalized: list[tuple[int, int]] = []
    for start, end in sorted((int(a), int(b)) for a, b in intervals):
        if start < min(WORKING_HOURS) or end > max(WORKING_HOURS) + 1 or start >= end:
            raise ValueError("Некорректный интервал")
        if normalized and start <= normalized[-1][1]:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], end))
        else:
            normalized.append((start, end))
    if not normalized or len(normalized) > 3:
        raise ValueError("Нужно выбрать от 1 до 3 интервалов")

    with get_conn() as conn:
        user = conn.execute("SELECT id FROM users WHERE tg_id=?", (int(tg_id),)).fetchone()
        if not user:
            raise ValueError("Пользователь не зарегистрирован")
        user_id = int(user[0])
        old = conn.execute(
            "SELECT id,mode,any_machine FROM waitlist_requests WHERE user_id=? AND status='active'",
            (user_id,),
        ).fetchone()
        now_s = now.isoformat(timespec="seconds")
        if old:
            request_id = int(old[0])
            conn.execute(
                """
                UPDATE waitlist_requests
                SET mode=?,any_machine=?,priority_since=?,updated_at=?
                WHERE id=?
                """,
                (mode, int(bool(any_machine)), now_s, now_s, request_id),
            )
            conn.execute("DELETE FROM waitlist_intervals WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM waitlist_machines WHERE request_id=?", (request_id,))
        else:
            cur = conn.execute(
                """
                INSERT INTO waitlist_requests
                (user_id,mode,status,any_machine,created_at,priority_since,updated_at)
                VALUES (?,?,'active',?,?,?,?,?)
                """,
                (user_id, mode, int(bool(any_machine)), now_s, now_s, now_s),
            )
            request_id = getattr(cur, "lastrowid", None)
            if not request_id:
                request_id = int(conn.execute(
                    "SELECT id FROM waitlist_requests WHERE user_id=? AND status='active'",
                    (user_id,),
                ).fetchone()[0])

        for start, end in normalized:
            conn.execute(
                "INSERT INTO waitlist_intervals(request_id,start_hour,end_hour) VALUES (?,?,?)",
                (request_id, int(start), int(end)),
            )
        if not any_machine:
            for mid in sorted({int(x) for x in machine_ids}):
                conn.execute(
                    "INSERT INTO waitlist_machines(request_id,machine_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                    (request_id, mid),
                )
    return int(request_id)


def _active_requests() -> list[Request]:
    with get_conn() as conn:
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
        machine_rows = conn.execute(
            "SELECT request_id,machine_id FROM waitlist_machines"
        ).fetchall()

    intervals: dict[int, list[tuple[int, int]]] = {}
    for rid, start, end in interval_rows:
        intervals.setdefault(int(rid), []).append((int(start), int(end)))
    machines: dict[int, set[int]] = {}
    for rid, mid in machine_rows:
        machines.setdefault(int(rid), set()).add(int(mid))

    result = []
    for rid, uid, tg, mode, any_machine, priority_since in rows:
        if int(tg) <= 0 or is_banned(int(tg)):
            continue
        result.append(Request(
            int(rid), int(uid), int(tg), str(mode), bool(any_machine),
            _dt(priority_since), intervals.get(int(rid), []),
            machines.get(int(rid), set()),
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
    slots = []
    for (mid,) in machines:
        for hour in get_free_hours_effective(int(mid), str(date_iso)):
            slots.append((int(mid), int(hour)))
    return slots


def _match(requests: list[Request], slots: list[tuple[int, int]]) -> dict[int, tuple[int, int]]:
    now = datetime.now(TZ)
    allowed: dict[int, list[tuple[int, int]]] = {}
    for req in requests:
        opts = [slot for slot in slots if req.accepts_machine(slot[0]) and req.accepts_hour(slot[1])]
        if opts:
            allowed[req.id] = opts

    order = sorted(
        [r for r in requests if r.id in allowed],
        key=lambda r: (len(allowed[r.id]), -r.queue_score(now), r.priority_since),
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
                if current is not None:
                    request_slot.pop(current, None)
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
        f"📅 {result.date}\n"
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
        )
    except Exception:
        pass


async def _create_hold(req: Request, machine_id: int, date_iso: str, hour: int, context: str) -> None:
    if BOT is None:
        return
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT 1 FROM slot_holds
            WHERE user_id=? AND status='active' AND expires_at>?
            LIMIT 1
            """,
            (req.user_id, datetime.now(TZ).isoformat(timespec="seconds")),
        ).fetchone()
        if existing:
            return
        machine = conn.execute("SELECT name FROM machines WHERE id=?", (machine_id,)).fetchone()
        if not machine:
            return
        expires = datetime.now(TZ) + timedelta(minutes=HOLD_MINUTES)
        cur = conn.execute(
            """
            INSERT INTO slot_holds
            (request_id,user_id,machine_id,date,hour,expires_at,context,status,created_at)
            VALUES (?,?,?,?,?,?,?,'active',?)
            """,
            (
                req.id, req.user_id, machine_id, date_iso, hour,
                expires.isoformat(timespec="seconds"), context,
                datetime.now(TZ).isoformat(timespec="seconds"),
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

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записаться", callback_data=f"wl_accept_{hold_id}"),
        InlineKeyboardButton(text="❌ Пропустить", callback_data=f"wl_decline_{hold_id}"),
    ]])
    text = (
        "🔔 <b>Для вас найдено место</b>\n\n"
        f"📅 {date_iso}\n"
        f"🕐 {hour:02d}:00\n"
        f"🧺 {machine[0]}\n\n"
        f"Слот зарезервирован за вами на {HOLD_MINUTES} минуты."
    )
    try:
        await BOT.send_message(
            req.tg_id, text, parse_mode="HTML", reply_markup=kb,
            disable_notification=_quiet_for(req.user_id),
        )
    except Exception:
        with get_conn() as conn:
            conn.execute("UPDATE slot_holds SET status='expired' WHERE id=?", (hold_id,))


async def distribute_date(date_iso: str, *, context: str = "day") -> int:
    if not WAITLIST_ENABLED:
        return 0
    requests = _active_requests()
    slots = _free_slots(date_iso)
    if not requests or not slots:
        return 0
    matches = _match(requests, slots)
    by_id = {r.id: r for r in requests}
    count = 0
    for rid, (mid, hour) in matches.items():
        req = by_id[rid]
        if req.mode == "auto":
            try:
                result = await create_booking_safe(req.user_id, mid, date_iso, hour)
            except BookingError:
                continue
            await _schedule_booking_features(result)
            await _send_auto_confirmation(req, result, night=context == "night")
            count += 1
        else:
            await _create_hold(req, mid, date_iso, hour, context)
            count += 1
    return count


async def process_night_round() -> None:
    if not WAITLIST_ENABLED:
        return
    now = datetime.now(TZ)
    target = (now.date() + timedelta(days=3)).isoformat()
    cutoff = now.replace(hour=23, minute=0, second=0, microsecond=0)
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM waitlist_rounds WHERE target_date=?", (target,)).fetchone()
        if row and str(row[0]) in {"running", "finished"}:
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


async def expire_holds() -> int:
    now = datetime.now(TZ)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id,date,context FROM slot_holds
            WHERE status='active' AND expires_at<=?
            """,
            (now.isoformat(timespec="seconds"),),
        ).fetchall()
        for hold_id, _, _ in rows:
            conn.execute(
                "UPDATE slot_holds SET status='expired' WHERE id=?",
                (int(hold_id),),
            )
    rerun = set()
    for _, date_iso, context in rows:
        if context == "night" and now.hour == 23:
            rerun.add(str(date_iso))
        elif context == "day":
            rerun.add(str(date_iso))
    for date_iso in rerun:
        await distribute_date(date_iso, context="night" if now.hour == 23 else "day")
    return len(rows)


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
        try:
            await BOT.send_message(
                int(tg), str(text), parse_mode="HTML",
                disable_notification=_quiet_for(int(uid)),
            )
            with get_conn() as conn:
                conn.execute("UPDATE pending_waitlist_notifications SET sent=1 WHERE id=?", (int(pid),))
            sent += 1
        except Exception:
            pass
    return sent
