from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.triggers.date import DateTrigger

from config import TIMEZONE
from database import get_conn, get_notification_settings, get_user
from scheduler import scheduler

TZ = ZoneInfo(TIMEZONE)
BOT: Bot | None = None

EXCHANGE_MIN_LEAD_MINUTES = 60
EXCHANGE_PROPOSAL_MINUTES = 10
EXCHANGE_PAGE_SIZE = 5

_EXCHANGE_LOCK = asyncio.Lock()


class ExchangeError(Exception):
    pass


class ExchangeUnavailable(ExchangeError):
    pass


class ExchangeConflict(ExchangeError):
    pass


@dataclass(frozen=True)
class ExchangeSlot:
    booking_id: int
    user_id: int
    tg_id: int
    machine_id: int
    machine_name: str
    date: str
    hour: int


@dataclass(frozen=True)
class ListingView:
    listing_id: int
    booking_id: int
    user_id: int
    machine_id: int
    machine_name: str
    date: str
    hour: int
    status: str
    suitable: bool = False


@dataclass(frozen=True)
class ProposalResult:
    proposal_id: int
    from_slot: ExchangeSlot
    to_slot: ExchangeSlot
    expires_at: datetime


def attach_bot(bot: Bot) -> None:
    global BOT
    BOT = bot


def _ds(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _slot_start(date_iso: str, hour: int) -> datetime:
    d = datetime.fromisoformat(str(date_iso)).date()
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ).replace(hour=int(hour))


def _lead_ok(date_iso: str, hour: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    return _slot_start(date_iso, int(hour)) - now >= timedelta(minutes=EXCHANGE_MIN_LEAD_MINUTES)


def _quiet_for(user_id: int) -> bool:
    cfg = get_notification_settings(int(user_id))
    if not cfg["quiet_enabled"]:
        return False
    hour = datetime.now(TZ).hour
    start, end = int(cfg["quiet_start"]), int(cfg["quiet_end"])
    return (hour >= start or hour < end) if start > end else (start <= hour < end)


def _listing_job_id(listing_id: int) -> str:
    return f"exchange_listing_{int(listing_id)}"


def _proposal_job_id(proposal_id: int) -> str:
    return f"exchange_proposal_{int(proposal_id)}"


def _remove_job(job_id: str) -> None:
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


def _booking_row(conn, booking_id: int):
    return conn.execute(
        """
        SELECT b.id,b.user_id,u.tg_id,b.machine_id,m.name,m.type,b.date,b.hour
        FROM bookings b
        JOIN users u ON u.id=b.user_id
        JOIN machines m ON m.id=b.machine_id
        WHERE b.id=?
        """,
        (int(booking_id),),
    ).fetchone()


def _slot_from_row(row) -> ExchangeSlot:
    return ExchangeSlot(
        booking_id=int(row[0]),
        user_id=int(row[1]),
        tg_id=int(row[2]),
        machine_id=int(row[3]),
        machine_name=str(row[4]),
        date=_ds(row[6]),
        hour=int(row[7]),
    )


def future_wash_bookings_for_tg(tg_id: int, now: datetime | None = None) -> list[ExchangeSlot]:
    now = now or datetime.now(TZ)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.id,b.user_id,u.tg_id,b.machine_id,m.name,m.type,b.date,b.hour
            FROM bookings b
            JOIN users u ON u.id=b.user_id
            JOIN machines m ON m.id=b.machine_id
            WHERE u.tg_id=? AND m.type='wash' AND b.date>=?
            ORDER BY b.date,b.hour,b.id
            """,
            (int(tg_id), now.date().isoformat()),
        ).fetchall()

    result = []
    for row in rows:
        slot = _slot_from_row(row)
        if _slot_start(slot.date, slot.hour) > now:
            result.append(slot)
    return result


def has_future_wash_for_tg(tg_id: int, now: datetime | None = None) -> bool:
    return bool(future_wash_bookings_for_tg(int(tg_id), now=now))


def _has_linked_dryer(conn, slot: ExchangeSlot) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM bookings b
        JOIN machines m ON m.id=b.machine_id
        WHERE b.user_id=? AND m.type='dry' AND b.date=? AND b.hour=?
        LIMIT 1
        """,
        (slot.user_id, slot.date, slot.hour + 1),
    ).fetchone()
    return bool(row)


def _listing_snapshot_matches(conn, listing_row, *, now: datetime | None = None) -> bool:
    booking_id = int(listing_row[1])
    row = _booking_row(conn, booking_id)
    if not row or str(row[5]) != "wash":
        return False
    return (
        int(row[1]) == int(listing_row[2])
        and int(row[3]) == int(listing_row[3])
        and _ds(row[6]) == str(listing_row[4])
        and int(row[7]) == int(listing_row[5])
        and _lead_ok(_ds(row[6]), int(row[7]), now=now)
    )


def _active_listing_row(conn, listing_id: int):
    return conn.execute(
        """
        SELECT id,booking_id,user_id,machine_id_snapshot,date_snapshot,hour_snapshot,status,
               created_at,updated_at,closed_at,closed_reason
        FROM exchange_listings
        WHERE id=? AND status IN ('active','locked')
        """,
        (int(listing_id),),
    ).fetchone()


def listing_for_booking(booking_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id,status,created_at
            FROM exchange_listings
            WHERE booking_id=? AND status IN ('active','locked')
            ORDER BY id DESC LIMIT 1
            """,
            (int(booking_id),),
        ).fetchone()


def create_listing(tg_id: int, booking_id: int) -> int:
    now = datetime.now(TZ)
    with get_conn() as conn:
        row = _booking_row(conn, int(booking_id))
        if not row or int(row[2]) != int(tg_id) or str(row[5]) != "wash":
            raise ExchangeUnavailable("Запись на стирку не найдена.")
        slot = _slot_from_row(row)
        if not _lead_ok(slot.date, slot.hour, now):
            raise ExchangeUnavailable("Эту стирку уже поздно выставлять на обмен.")
        if _has_linked_dryer(conn, slot):
            raise ExchangeUnavailable(
                "После этой стирки у вас уже есть запись на сушку. Сначала отмените сушку."
            )

        existing = conn.execute(
            """
            SELECT id FROM exchange_listings
            WHERE booking_id=? AND status IN ('active','locked')
            ORDER BY id DESC LIMIT 1
            """,
            (slot.booking_id,),
        ).fetchone()
        if existing:
            return int(existing[0])

        now_s = now.isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO exchange_listings
            (booking_id,user_id,machine_id_snapshot,date_snapshot,hour_snapshot,status,
             created_at,updated_at)
            VALUES (?,?,?,?,?,'active',?,?)
            """,
            (
                slot.booking_id,
                slot.user_id,
                slot.machine_id,
                slot.date,
                slot.hour,
                now_s,
                now_s,
            ),
        )
        listing_id = int(cur.lastrowid)

    _schedule_listing_expiry(listing_id, slot.date, slot.hour)
    return listing_id


def _subscription_matches(user_id: int, machine_id: int, date_iso: str, hour: int) -> bool:
    weekday = datetime.fromisoformat(str(date_iso)).date().weekday()
    with get_conn() as conn:
        req = conn.execute(
            """
            SELECT id,any_machine
            FROM waitlist_requests
            WHERE user_id=? AND persistent=1 AND status IN ('active','matched','paused')
            ORDER BY updated_at DESC,id DESC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if not req:
            return False
        request_id, any_machine = int(req[0]), bool(req[1])
        schedule = conn.execute(
            """
            SELECT 1 FROM waitlist_schedule
            WHERE request_id=? AND weekday=? AND start_hour<=? AND end_hour>?
            LIMIT 1
            """,
            (request_id, weekday, int(hour), int(hour)),
        ).fetchone()
        if not schedule:
            return False
        if any_machine:
            return True
        machine = conn.execute(
            "SELECT 1 FROM waitlist_machines WHERE request_id=? AND machine_id=? LIMIT 1",
            (request_id, int(machine_id)),
        ).fetchone()
        return bool(machine)


def available_listings(
    tg_id: int,
    from_listing_id: int,
    *,
    page: int = 0,
    page_size: int = EXCHANGE_PAGE_SIZE,
) -> tuple[list[ListingView], int]:
    now = datetime.now(TZ)
    user = get_user(int(tg_id))
    if not user:
        raise ExchangeUnavailable("Пользователь не найден.")
    user_id = int(user[0])

    with get_conn() as conn:
        source = _active_listing_row(conn, int(from_listing_id))
        if not source or int(source[2]) != user_id or str(source[6]) != "active":
            raise ExchangeUnavailable("Ваша запись сейчас недоступна для обмена.")
        if not _listing_snapshot_matches(conn, source, now=now):
            conn.execute(
                "UPDATE exchange_listings SET status='stale',closed_at=?,closed_reason='booking_changed',updated_at=? WHERE id=?",
                (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), int(from_listing_id)),
            )
            raise ExchangeUnavailable("Ваша запись изменилась. Объявление снято.")

        rows = conn.execute(
            """
            SELECT el.id,el.booking_id,el.user_id,el.machine_id_snapshot,m.name,
                   el.date_snapshot,el.hour_snapshot,el.status
            FROM exchange_listings el
            JOIN machines m ON m.id=el.machine_id_snapshot
            WHERE el.status='active' AND el.user_id<>?
            ORDER BY el.date_snapshot,el.hour_snapshot,el.id
            """,
            (user_id,),
        ).fetchall()

    views = []
    for row in rows:
        listing_id = int(row[0])
        with get_conn() as conn:
            fresh = _active_listing_row(conn, listing_id)
            if not fresh or not _listing_snapshot_matches(conn, fresh, now=now):
                if fresh:
                    conn.execute(
                        "UPDATE exchange_listings SET status='stale',closed_at=?,closed_reason='booking_changed',updated_at=? WHERE id=?",
                        (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), listing_id),
                    )
                continue
        views.append(
            ListingView(
                listing_id=listing_id,
                booking_id=int(row[1]),
                user_id=int(row[2]),
                machine_id=int(row[3]),
                machine_name=str(row[4]),
                date=_ds(row[5]),
                hour=int(row[6]),
                status=str(row[7]),
                suitable=_subscription_matches(user_id, int(row[3]), _ds(row[5]), int(row[6])),
            )
        )

    views.sort(key=lambda x: (not x.suitable, x.date, x.hour, x.listing_id))
    total_pages = max(1, (len(views) + page_size - 1) // page_size)
    page = max(0, min(int(page), total_pages - 1))
    start = page * page_size
    return views[start : start + page_size], total_pages


def get_listing_view(listing_id: int) -> ListingView | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT el.id,el.booking_id,el.user_id,el.machine_id_snapshot,m.name,
                   el.date_snapshot,el.hour_snapshot,el.status
            FROM exchange_listings el
            JOIN machines m ON m.id=el.machine_id_snapshot
            WHERE el.id=?
            """,
            (int(listing_id),),
        ).fetchone()
    if not row:
        return None
    return ListingView(
        listing_id=int(row[0]),
        booking_id=int(row[1]),
        user_id=int(row[2]),
        machine_id=int(row[3]),
        machine_name=str(row[4]),
        date=_ds(row[5]),
        hour=int(row[6]),
        status=str(row[7]),
    )


def _proposal_deadline(from_row, to_row, now: datetime) -> datetime:
    normal = now + timedelta(minutes=EXCHANGE_PROPOSAL_MINUTES)
    earliest = min(
        _slot_start(str(from_row[4]), int(from_row[5])),
        _slot_start(str(to_row[4]), int(to_row[5])),
    ) - timedelta(minutes=EXCHANGE_MIN_LEAD_MINUTES)
    return min(normal, earliest)


def create_proposal(tg_id: int, from_listing_id: int, to_listing_id: int) -> ProposalResult:
    if int(from_listing_id) == int(to_listing_id):
        raise ExchangeConflict("Нельзя обменять запись саму на себя.")

    user = get_user(int(tg_id))
    if not user:
        raise ExchangeUnavailable("Пользователь не найден.")
    user_id = int(user[0])
    now = datetime.now(TZ)

    with get_conn() as conn:
        source = _active_listing_row(conn, int(from_listing_id))
        target = _active_listing_row(conn, int(to_listing_id))
        if not source or not target:
            raise ExchangeUnavailable("Один из обменов уже недоступен.")
        if int(source[2]) != user_id:
            raise ExchangeUnavailable("Это не ваша запись.")
        if int(target[2]) == user_id:
            raise ExchangeConflict("Нельзя обмениваться своими же записями.")
        if str(source[6]) != "active" or str(target[6]) != "active":
            raise ExchangeConflict("Одна из записей уже участвует в другом предложении.")
        if not _listing_snapshot_matches(conn, source, now=now) or not _listing_snapshot_matches(conn, target, now=now):
            raise ExchangeUnavailable("Одна из записей уже изменилась.")

        source_slot = _slot_from_row(_booking_row(conn, int(source[1])))
        target_slot = _slot_from_row(_booking_row(conn, int(target[1])))
        if _has_linked_dryer(conn, source_slot) or _has_linked_dryer(conn, target_slot):
            raise ExchangeUnavailable("Обмен недоступен: у одной из стирок уже есть связанная сушка.")

        expires_at = _proposal_deadline(source, target, now)
        if expires_at <= now:
            raise ExchangeUnavailable("До одной из стирок осталось слишком мало времени.")

        now_s = now.isoformat(timespec="seconds")
        try:
            cur = conn.execute(
                """
                INSERT INTO exchange_proposals
                (from_listing_id,to_listing_id,status,created_at,expires_at)
                VALUES (?,?,'pending',?,?)
                """,
                (
                    int(from_listing_id),
                    int(to_listing_id),
                    now_s,
                    expires_at.isoformat(timespec="seconds"),
                ),
            )
            proposal_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE exchange_listings SET status='locked',updated_at=? WHERE id IN (?,?)",
                (now_s, int(from_listing_id), int(to_listing_id)),
            )
        except Exception as exc:
            raise ExchangeConflict("Одна из записей только что получила другое предложение.") from exc

    _schedule_proposal_expiry(proposal_id, expires_at)
    return ProposalResult(proposal_id, source_slot, target_slot, expires_at)


def proposal_details(proposal_id: int) -> ProposalResult | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT ep.id,ep.expires_at,fl.booking_id,tl.booking_id
            FROM exchange_proposals ep
            JOIN exchange_listings fl ON fl.id=ep.from_listing_id
            JOIN exchange_listings tl ON tl.id=ep.to_listing_id
            WHERE ep.id=? AND ep.status='pending'
            """,
            (int(proposal_id),),
        ).fetchone()
        if not row:
            return None
        fr = _booking_row(conn, int(row[2]))
        tr = _booking_row(conn, int(row[3]))
        if not fr or not tr:
            return None
        return ProposalResult(
            int(row[0]),
            _slot_from_row(fr),
            _slot_from_row(tr),
            datetime.fromisoformat(str(row[1])),
        )


def _daily_limit_conflict(conn, user_id: int, target_date: str, exclude_booking_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM bookings b
        JOIN machines m ON m.id=b.machine_id
        WHERE b.user_id=? AND m.type='wash' AND b.date=? AND b.id<>?
        LIMIT 1
        """,
        (int(user_id), str(target_date), int(exclude_booking_id)),
    ).fetchone()
    return bool(row)


async def accept_proposal(tg_id: int, proposal_id: int) -> ProposalResult:
    from booking_service import _begin, _commit, _rollback, _close_transaction

    async with _EXCHANGE_LOCK:
        now = datetime.now(TZ)
        conn = get_conn()
        raw = _begin(conn)
        try:
            row = conn.execute(
                """
                SELECT ep.id,ep.from_listing_id,ep.to_listing_id,ep.expires_at,
                       fl.booking_id,fl.user_id,fl.status,
                       tl.booking_id,tl.user_id,tl.status
                FROM exchange_proposals ep
                JOIN exchange_listings fl ON fl.id=ep.from_listing_id
                JOIN exchange_listings tl ON tl.id=ep.to_listing_id
                WHERE ep.id=? AND ep.status='pending'
                """,
                (int(proposal_id),),
            ).fetchone()
            if not row:
                raise ExchangeUnavailable("Предложение уже недоступно.")
            if int(row[8]) != int(get_user(int(tg_id))[0]):
                raise ExchangeUnavailable("Это предложение адресовано другому пользователю.")
            expires_at = datetime.fromisoformat(str(row[3]))
            if expires_at <= now:
                raise ExchangeUnavailable("Время ответа на предложение уже истекло.")
            if str(row[6]) != "locked" or str(row[9]) != "locked":
                raise ExchangeUnavailable("Одна из записей уже изменилась.")

            from_listing = _active_listing_row(conn, int(row[1]))
            to_listing = _active_listing_row(conn, int(row[2]))
            if not from_listing or not to_listing:
                raise ExchangeUnavailable("Один из обменов уже недоступен.")
            if not _listing_snapshot_matches(conn, from_listing, now=now) or not _listing_snapshot_matches(conn, to_listing, now=now):
                raise ExchangeUnavailable("Одна из записей уже изменилась.")

            a = _slot_from_row(_booking_row(conn, int(row[4])))
            b = _slot_from_row(_booking_row(conn, int(row[7])))
            if not _lead_ok(a.date, a.hour, now) or not _lead_ok(b.date, b.hour, now):
                raise ExchangeUnavailable("До одной из стирок осталось меньше часа.")
            if _has_linked_dryer(conn, a) or _has_linked_dryer(conn, b):
                raise ExchangeUnavailable("Обмен недоступен: у одной из стирок уже есть связанная сушка.")
            if _daily_limit_conflict(conn, a.user_id, b.date, a.booking_id):
                raise ExchangeConflict("После обмена у первого пользователя будет две стирки в один день.")
            if _daily_limit_conflict(conn, b.user_id, a.date, b.booking_id):
                raise ExchangeConflict("После обмена у второго пользователя будет две стирки в один день.")

            temp_hour = -1000000 - int(a.booking_id)
            conn.execute(
                "UPDATE bookings SET hour=? WHERE id=?",
                (temp_hour, a.booking_id),
            )
            conn.execute(
                "UPDATE bookings SET machine_id=?,date=?,hour=? WHERE id=?",
                (a.machine_id, a.date, a.hour, b.booking_id),
            )
            conn.execute(
                "UPDATE bookings SET machine_id=?,date=?,hour=? WHERE id=?",
                (b.machine_id, b.date, b.hour, a.booking_id),
            )

            now_s = now.isoformat(timespec="seconds")
            conn.execute(
                "UPDATE exchange_proposals SET status='accepted',responded_at=? WHERE id=?",
                (now_s, int(proposal_id)),
            )
            conn.execute(
                """
                UPDATE exchange_listings
                SET status='completed',updated_at=?,closed_at=?,closed_reason='swapped'
                WHERE id IN (?,?)
                """,
                (now_s, now_s, int(row[1]), int(row[2])),
            )
            _commit(raw)
        except ExchangeError:
            _rollback(raw)
            raise
        except Exception as exc:
            _rollback(raw)
            raise ExchangeConflict("Не удалось безопасно выполнить обмен.") from exc
        finally:
            _close_transaction(conn, raw)

    _remove_job(_proposal_job_id(int(proposal_id)))
    _remove_job(_listing_job_id(int(row[1])))
    _remove_job(_listing_job_id(int(row[2])))

    from waitlist_service import sync_subscription_pause_for_user
    sync_subscription_pause_for_user(a.user_id)
    sync_subscription_pause_for_user(b.user_id)

    from handlers.laundry_features import schedule_reminder
    await schedule_reminder(a.tg_id, b.machine_name, b.date, b.hour, 30)
    await schedule_reminder(b.tg_id, a.machine_name, a.date, a.hour, 30)

    return ProposalResult(
        int(proposal_id),
        ExchangeSlot(a.booking_id, a.user_id, a.tg_id, b.machine_id, b.machine_name, b.date, b.hour),
        ExchangeSlot(b.booking_id, b.user_id, b.tg_id, a.machine_id, a.machine_name, a.date, a.hour),
        expires_at,
    )


def _unlock_listing_if_valid(conn, listing_id: int, now: datetime) -> None:
    listing = _active_listing_row(conn, int(listing_id))
    if not listing:
        return
    if _listing_snapshot_matches(conn, listing, now=now):
        conn.execute(
            "UPDATE exchange_listings SET status='active',updated_at=? WHERE id=?",
            (now.isoformat(timespec="seconds"), int(listing_id)),
        )
    else:
        conn.execute(
            """
            UPDATE exchange_listings
            SET status='stale',updated_at=?,closed_at=?,closed_reason='booking_changed'
            WHERE id=?
            """,
            (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), int(listing_id)),
        )


async def decline_proposal(tg_id: int, proposal_id: int) -> bool:
    user = get_user(int(tg_id))
    if not user:
        return False
    user_id = int(user[0])
    now = datetime.now(TZ)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT ep.from_listing_id,ep.to_listing_id,tl.user_id
            FROM exchange_proposals ep
            JOIN exchange_listings tl ON tl.id=ep.to_listing_id
            WHERE ep.id=? AND ep.status='pending'
            """,
            (int(proposal_id),),
        ).fetchone()
        if not row or int(row[2]) != user_id:
            return False
        conn.execute(
            "UPDATE exchange_proposals SET status='declined',responded_at=? WHERE id=?",
            (now.isoformat(timespec="seconds"), int(proposal_id)),
        )
        _unlock_listing_if_valid(conn, int(row[0]), now)
        _unlock_listing_if_valid(conn, int(row[1]), now)
    _remove_job(_proposal_job_id(int(proposal_id)))
    return True


async def cancel_proposal(tg_id: int, proposal_id: int) -> bool:
    user = get_user(int(tg_id))
    if not user:
        return False
    user_id = int(user[0])
    now = datetime.now(TZ)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT ep.from_listing_id,ep.to_listing_id,fl.user_id
            FROM exchange_proposals ep
            JOIN exchange_listings fl ON fl.id=ep.from_listing_id
            WHERE ep.id=? AND ep.status='pending'
            """,
            (int(proposal_id),),
        ).fetchone()
        if not row or int(row[2]) != user_id:
            return False
        conn.execute(
            "UPDATE exchange_proposals SET status='cancelled',responded_at=? WHERE id=?",
            (now.isoformat(timespec="seconds"), int(proposal_id)),
        )
        _unlock_listing_if_valid(conn, int(row[0]), now)
        _unlock_listing_if_valid(conn, int(row[1]), now)
    _remove_job(_proposal_job_id(int(proposal_id)))
    return True


async def expire_proposal(proposal_id: int) -> bool:
    now = datetime.now(TZ)
    notify = None
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT ep.from_listing_id,ep.to_listing_id,ep.expires_at,
                   fu.tg_id,tu.tg_id
            FROM exchange_proposals ep
            JOIN exchange_listings fl ON fl.id=ep.from_listing_id
            JOIN exchange_listings tl ON tl.id=ep.to_listing_id
            JOIN users fu ON fu.id=fl.user_id
            JOIN users tu ON tu.id=tl.user_id
            WHERE ep.id=? AND ep.status='pending'
            """,
            (int(proposal_id),),
        ).fetchone()
        if not row:
            return False
        if datetime.fromisoformat(str(row[2])) > now:
            _schedule_proposal_expiry(int(proposal_id), datetime.fromisoformat(str(row[2])))
            return False
        conn.execute(
            "UPDATE exchange_proposals SET status='expired',responded_at=? WHERE id=?",
            (now.isoformat(timespec="seconds"), int(proposal_id)),
        )
        _unlock_listing_if_valid(conn, int(row[0]), now)
        _unlock_listing_if_valid(conn, int(row[1]), now)
        notify = (int(row[3]), int(row[4]))

    if BOT and notify:
        for tg_id in notify:
            try:
                await BOT.send_message(tg_id, "⌛ Предложение обмена истекло. Ваш исходный слот остался без изменений.")
            except Exception:
                pass
    return True


async def cancel_listing(tg_id: int, listing_id: int) -> bool:
    user = get_user(int(tg_id))
    if not user:
        return False
    user_id = int(user[0])
    return await _close_listing(int(listing_id), user_id=user_id, reason="user_cancelled")


async def invalidate_listing_for_booking(booking_id: int, reason: str = "booking_changed") -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id FROM exchange_listings
            WHERE booking_id=? AND status IN ('active','locked')
            ORDER BY id DESC LIMIT 1
            """,
            (int(booking_id),),
        ).fetchone()
    if not row:
        return False
    return await _close_listing(int(row[0]), reason=reason)


async def _close_listing(listing_id: int, *, user_id: int | None = None, reason: str) -> bool:
    now = datetime.now(TZ)
    counterpart_tg = None
    with get_conn() as conn:
        listing = _active_listing_row(conn, int(listing_id))
        if not listing:
            return False
        if user_id is not None and int(listing[2]) != int(user_id):
            return False

        pending = conn.execute(
            """
            SELECT ep.id,ep.from_listing_id,ep.to_listing_id,
                   CASE WHEN ep.from_listing_id=? THEN tu.tg_id ELSE fu.tg_id END
            FROM exchange_proposals ep
            JOIN exchange_listings fl ON fl.id=ep.from_listing_id
            JOIN exchange_listings tl ON tl.id=ep.to_listing_id
            JOIN users fu ON fu.id=fl.user_id
            JOIN users tu ON tu.id=tl.user_id
            WHERE ep.status='pending' AND (ep.from_listing_id=? OR ep.to_listing_id=?)
            LIMIT 1
            """,
            (int(listing_id), int(listing_id), int(listing_id)),
        ).fetchone()
        now_s = now.isoformat(timespec="seconds")
        if pending:
            conn.execute(
                "UPDATE exchange_proposals SET status='stale',responded_at=? WHERE id=?",
                (now_s, int(pending[0])),
            )
            other = int(pending[2]) if int(pending[1]) == int(listing_id) else int(pending[1])
            _unlock_listing_if_valid(conn, other, now)
            counterpart_tg = int(pending[3])
            _remove_job(_proposal_job_id(int(pending[0])))

        conn.execute(
            """
            UPDATE exchange_listings
            SET status=?,updated_at=?,closed_at=?,closed_reason=?
            WHERE id=?
            """,
            ("expired" if reason == "expired" else "cancelled" if reason == "user_cancelled" else "stale",
             now_s, now_s, str(reason), int(listing_id)),
        )

    _remove_job(_listing_job_id(int(listing_id)))
    if BOT and counterpart_tg:
        try:
            await BOT.send_message(
                counterpart_tg,
                "❌ Обмен больше недоступен: вторая запись была изменена или снята с обмена. Ваша запись не изменилась.",
            )
        except Exception:
            pass
    return True


async def expire_listing(listing_id: int) -> bool:
    return await _close_listing(int(listing_id), reason="expired")


def _schedule_listing_expiry(listing_id: int, date_iso: str, hour: int) -> None:
    run_at = _slot_start(date_iso, int(hour)) - timedelta(minutes=EXCHANGE_MIN_LEAD_MINUTES)
    now = datetime.now(TZ)
    if run_at <= now:
        return
    scheduler.add_job(
        expire_listing,
        trigger=DateTrigger(run_date=run_at),
        id=_listing_job_id(int(listing_id)),
        args=[int(listing_id)],
        replace_existing=True,
        misfire_grace_time=3600,
    )


def _schedule_proposal_expiry(proposal_id: int, expires_at: datetime) -> None:
    now = datetime.now(TZ)
    run_at = expires_at if expires_at > now else now + timedelta(seconds=1)
    scheduler.add_job(
        expire_proposal,
        trigger=DateTrigger(run_date=run_at),
        id=_proposal_job_id(int(proposal_id)),
        args=[int(proposal_id)],
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def rebuild_exchange_jobs() -> None:
    now = datetime.now(TZ)
    with get_conn() as conn:
        listings = conn.execute(
            """
            SELECT id,booking_id,user_id,machine_id_snapshot,date_snapshot,hour_snapshot,status,
                   created_at,updated_at,closed_at,closed_reason
            FROM exchange_listings
            WHERE status IN ('active','locked')
            ORDER BY id
            """
        ).fetchall()

    for listing in listings:
        with get_conn() as conn:
            fresh = _active_listing_row(conn, int(listing[0]))
            if not fresh:
                continue
            if not _listing_snapshot_matches(conn, fresh, now=now):
                await _close_listing(int(listing[0]), reason="booking_changed")
                continue
        _schedule_listing_expiry(int(listing[0]), str(listing[4]), int(listing[5]))

    with get_conn() as conn:
        proposals = conn.execute(
            "SELECT id,expires_at FROM exchange_proposals WHERE status='pending' ORDER BY id"
        ).fetchall()

    for proposal_id, expires_at in proposals:
        expiry = datetime.fromisoformat(str(expires_at))
        if expiry <= now:
            await expire_proposal(int(proposal_id))
        else:
            _schedule_proposal_expiry(int(proposal_id), expiry)
