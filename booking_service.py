from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from config import TIMEZONE, WORKING_HOURS
from database import DATABASE_URL, get_conn, is_admin, is_banned

TZ = ZoneInfo(TIMEZONE)
_BOOKING_LOCK = asyncio.Lock()


class BookingError(Exception):
    pass


class SlotBusy(BookingError):
    pass


class DailyLimit(BookingError):
    pass


class InvalidBooking(BookingError):
    pass


@dataclass
class BookingResult:
    booking_id: int
    user_id: int
    machine_id: int
    machine_type: str
    machine_name: str
    date: str
    hour: int
    waitlist_reopened: bool = False


def slot_datetime(date_iso: str, hour: int) -> datetime:
    d = datetime.fromisoformat(str(date_iso)).date()
    return datetime.combine(d, time(hour=int(hour)), tzinfo=TZ)


def _booking_from_row(row) -> BookingResult:
    bid, uid, mid, mtype, mname, d, h = row
    ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
    return BookingResult(int(bid), int(uid), int(mid), str(mtype), str(mname), ds, int(h))


def get_booking(booking_id: int) -> BookingResult | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT b.id,b.user_id,b.machine_id,m.type,m.name,b.date,b.hour
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE b.id=?
            """,
            (int(booking_id),),
        ).fetchone()
    return _booking_from_row(row) if row else None


def _begin(conn):
    raw = getattr(conn, "_conn", None)
    if raw is None:
        return None
    if DATABASE_URL:
        raw.autocommit = False
    else:
        raw.execute("BEGIN IMMEDIATE")
    return raw


def _commit(raw):
    if raw is not None:
        raw.commit()


def _rollback(raw):
    if raw is not None:
        try:
            raw.rollback()
        except Exception:
            pass


def _close_transaction(conn, raw):
    if DATABASE_URL and raw is not None:
        try:
            raw.autocommit = True
        except Exception:
            pass
    conn.close()


def _insert_booking(
    conn,
    user_id: int,
    machine_id: int,
    date_iso: str,
    hour: int,
    *,
    allowed_hold_id: int | None,
    close_waitlist: bool,
    ignore_booking_id: int | None = None,
) -> BookingResult:
    hour = int(hour)
    if hour not in WORKING_HOURS:
        raise InvalidBooking("Недоступное время")
    if slot_datetime(date_iso, hour) <= datetime.now(TZ):
        raise InvalidBooking("Это время уже прошло")

    user = conn.execute(
        "SELECT tg_id FROM users WHERE id=?",
        (int(user_id),),
    ).fetchone()
    if not user:
        raise InvalidBooking("Пользователь не найден")
    if int(user[0]) > 0 and is_banned(int(user[0])):
        raise InvalidBooking("Вы заблокированы и не можете записываться")

    machine = conn.execute(
        "SELECT type,name,is_active FROM machines WHERE id=?",
        (int(machine_id),),
    ).fetchone()
    if not machine:
        raise InvalidBooking("Машина не найдена")

    mtype, mname, active = machine
    if not active:
        raise InvalidBooking("Машина сейчас недоступна")

    if str(mtype) == "dry" and not is_admin(int(user[0])):
        wash = conn.execute(
            """
            SELECT 1
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE b.user_id=? AND b.date=? AND m.type='wash'
            LIMIT 1
            """,
            (int(user_id), str(date_iso)),
        ).fetchone()
        if not wash:
            raise InvalidBooking(
                "Сначала нужно записаться на стиральную машину в этот день"
            )

    if DATABASE_URL:
        lock_key = f"booking:{int(user_id)}:{str(date_iso)}:{str(mtype)}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (lock_key,))

    if not is_admin(int(user[0])):
        params = [int(user_id), str(date_iso), str(mtype)]
        sql = """
            SELECT 1
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE b.user_id=? AND b.date=? AND m.type=?
        """
        if ignore_booking_id is not None:
            sql += " AND b.id<>?"
            params.append(int(ignore_booking_id))
        sql += " LIMIT 1"
        if conn.execute(sql, tuple(params)).fetchone():
            raise DailyLimit("На этот тип машины уже есть запись в этот день")

    params = [int(machine_id), str(date_iso), hour]
    sql = "SELECT 1 FROM bookings WHERE machine_id=? AND date=? AND hour=?"
    if ignore_booking_id is not None:
        sql += " AND id<>?"
        params.append(int(ignore_booking_id))
    sql += " LIMIT 1"
    if conn.execute(sql, tuple(params)).fetchone():
        raise SlotBusy("Слот уже занят")

    now_s = datetime.now(TZ).isoformat(timespec="seconds")
    holds = conn.execute(
        """
        SELECT id
        FROM slot_holds
        WHERE machine_id=? AND date=? AND hour=?
          AND status='active' AND expires_at>?
        """,
        (int(machine_id), str(date_iso), hour, now_s),
    ).fetchall()
    for (hold_id,) in holds:
        if allowed_hold_id is None or int(hold_id) != int(allowed_hold_id):
            raise SlotBusy("Слот временно зарезервирован")

    cur = conn.execute(
        "INSERT INTO bookings(user_id,machine_id,date,hour) VALUES (?,?,?,?)",
        (int(user_id), int(machine_id), str(date_iso), hour),
    )
    booking_id = getattr(cur, "lastrowid", None)
    if not booking_id:
        row = conn.execute(
            """
            SELECT id FROM bookings
            WHERE user_id=? AND machine_id=? AND date=? AND hour=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id), int(machine_id), str(date_iso), hour),
        ).fetchone()
        booking_id = int(row[0])

    if allowed_hold_id is not None:
        conn.execute(
            "UPDATE slot_holds SET status='accepted' WHERE id=?",
            (int(allowed_hold_id),),
        )

    if close_waitlist and str(mtype) == "wash":
        conn.execute(
            """
            UPDATE waitlist_requests
            SET status='matched',matched_booking_id=?,updated_at=?
            WHERE user_id=? AND status='active'
            """,
            (int(booking_id), now_s, int(user_id)),
        )
        conn.execute(
            """
            UPDATE slot_holds
            SET status='cancelled'
            WHERE request_id IN (
                SELECT id FROM waitlist_requests
                WHERE user_id=? AND status='matched' AND matched_booking_id=?
            )
              AND status='active'
              AND id<>COALESCE(?, -1)
            """,
            (int(user_id), int(booking_id), allowed_hold_id),
        )

    return BookingResult(
        int(booking_id),
        int(user_id),
        int(machine_id),
        str(mtype),
        str(mname),
        str(date_iso),
        hour,
    )


async def create_booking_safe(
    user_id: int,
    machine_id: int,
    date_iso: str,
    hour: int,
    *,
    allowed_hold_id: int | None = None,
    close_waitlist: bool = True,
) -> BookingResult:
    async with _BOOKING_LOCK:
        conn = get_conn()
        raw = _begin(conn)
        try:
            result = _insert_booking(
                conn,
                int(user_id),
                int(machine_id),
                str(date_iso),
                int(hour),
                allowed_hold_id=allowed_hold_id,
                close_waitlist=close_waitlist,
            )
            _commit(raw)

            if result.machine_type == "wash" and close_waitlist:
                # Lazy import avoids the booking_service <-> waitlist_service
                # module cycle. A manual booking must pause the same persistent
                # subscription just like a slot received from the waitlist.
                try:
                    from waitlist_service import sync_subscription_pause_for_user
                    sync_subscription_pause_for_user(int(result.user_id))
                except Exception:
                    # Booking itself must stay valid even if scheduling the
                    # subscription wake-up temporarily fails; startup recovery
                    # rebuilds these jobs.
                    pass

            return result
        except BookingError:
            _rollback(raw)
            raise
        except Exception as exc:
            _rollback(raw)
            raise SlotBusy("Слот только что заняли") from exc
        finally:
            _close_transaction(conn, raw)


async def cancel_booking_safe(
    booking_id: int,
    *,
    require_future: bool = True,
) -> BookingResult:
    async with _BOOKING_LOCK:
        conn = get_conn()
        raw = _begin(conn)
        try:
            row = conn.execute(
                """
                SELECT b.id,b.user_id,b.machine_id,m.type,m.name,b.date,b.hour
                FROM bookings b
                JOIN machines m ON m.id=b.machine_id
                WHERE b.id=?
                """,
                (int(booking_id),),
            ).fetchone()
            if not row:
                raise InvalidBooking("Запись не найдена")
            booking = _booking_from_row(row)
            slot_time = slot_datetime(booking.date, booking.hour)
            now = datetime.now(TZ)
            if require_future and slot_time <= now:
                raise InvalidBooking("Начавшуюся запись отменить нельзя")

            waitlist_reopened = False
            matched_request = None
            if booking.machine_type == "wash" and slot_time > now:
                matched_request = conn.execute(
                    """
                    SELECT id,priority_since,status
                    FROM waitlist_requests
                    WHERE user_id=? AND status IN ('matched','paused') AND matched_booking_id=?
                    LIMIT 1
                    """,
                    (int(booking.user_id), int(booking_id)),
                ).fetchone()

            conn.execute("DELETE FROM bookings WHERE id=?", (int(booking_id),))

            if matched_request:
                request_id = int(matched_request[0])
                request_status = str(matched_request[2])
                now_s = now.isoformat(timespec="seconds")

                remaining_rows = conn.execute(
                    """
                    SELECT b.id,b.date,b.hour
                    FROM bookings b
                    JOIN machines m ON m.id=b.machine_id
                    WHERE b.user_id=? AND m.type='wash' AND b.date>=?
                    ORDER BY b.date,b.hour
                    """,
                    (int(booking.user_id), now.date().isoformat()),
                ).fetchall()
                remaining = []
                for other_id, other_date, other_hour in remaining_rows:
                    ds = other_date.isoformat() if hasattr(other_date, "isoformat") else str(other_date)
                    end_at = slot_datetime(ds, int(other_hour)) + timedelta(hours=1)
                    if end_at > now:
                        remaining.append((int(other_id), ds, int(other_hour), end_at))

                other_active = conn.execute(
                    """
                    SELECT 1
                    FROM waitlist_requests
                    WHERE user_id=? AND status='active' AND id<>?
                    LIMIT 1
                    """,
                    (int(booking.user_id), request_id),
                ).fetchone()

                if not other_active:
                    if remaining:
                        next_booking_id, _next_date, _next_hour, next_end = remaining[-1]
                        priority_since = (
                            next_end.isoformat(timespec="seconds")
                            if request_status == "paused"
                            else str(matched_request[1])
                        )
                        conn.execute(
                            """
                            UPDATE waitlist_requests
                            SET status=?,matched_booking_id=?,priority_since=?,updated_at=?
                            WHERE id=? AND status IN ('matched','paused')
                            """,
                            (
                                request_status,
                                int(next_booking_id),
                                priority_since,
                                now_s,
                                request_id,
                            ),
                        )
                    else:
                        # If this subscription was already waiting before it got
                        # the booking, cancellation preserves the accumulated
                        # priority. If it was created while a booking already
                        # existed, waiting starts only now, at cancellation.
                        priority_since = (
                            now_s
                            if request_status == "paused"
                            else str(matched_request[1])
                        )
                        conn.execute(
                            """
                            UPDATE waitlist_requests
                            SET status='active',matched_booking_id=NULL,
                                priority_since=?,updated_at=?
                            WHERE id=? AND status IN ('matched','paused')
                            """,
                            (priority_since, now_s, request_id),
                        )
                        waitlist_reopened = True

                    conn.execute(
                        """
                        INSERT INTO waitlist_offer_history
                        (request_id,machine_id,date,hour,result,created_at)
                        VALUES (?,?,?,?,?,?)
                        """,
                        (
                            request_id,
                            int(booking.machine_id),
                            str(booking.date),
                            int(booking.hour),
                            "cancelled_booking",
                            now_s,
                        ),
                    )

            booking.waitlist_reopened = waitlist_reopened
            _commit(raw)

            if booking.machine_type == "wash":
                try:
                    from waitlist_service import (
                        _cancel_subscription_resume,
                        sync_subscription_pause_for_user,
                    )
                    if matched_request:
                        _cancel_subscription_resume(int(matched_request[0]))
                    # If another non-finished wash exists, keep the subscription
                    # paused/matched against that one. Otherwise the reopened
                    # request remains active from the cancellation moment.
                    sync_subscription_pause_for_user(int(booking.user_id))
                except Exception:
                    pass

            return booking
        except BookingError:
            _rollback(raw)
            raise
        except Exception:
            _rollback(raw)
            raise
        finally:
            _close_transaction(conn, raw)


async def move_booking_safe(
    current_booking_id: int,
    new_machine_id: int,
    new_date: str,
    new_hour: int,
    *,
    allowed_hold_id: int | None = None,
) -> tuple[BookingResult, BookingResult]:
    async with _BOOKING_LOCK:
        conn = get_conn()
        raw = _begin(conn)
        try:
            row = conn.execute(
                """
                SELECT b.id,b.user_id,b.machine_id,m.type,m.name,b.date,b.hour
                FROM bookings b
                JOIN machines m ON m.id=b.machine_id
                WHERE b.id=?
                """,
                (int(current_booking_id),),
            ).fetchone()
            if not row:
                raise InvalidBooking("Исходная запись не найдена")
            old = _booking_from_row(row)
            if slot_datetime(old.date, old.hour) <= datetime.now(TZ):
                raise InvalidBooking("Начавшуюся запись переносить нельзя")

            new = _insert_booking(
                conn,
                old.user_id,
                int(new_machine_id),
                str(new_date),
                int(new_hour),
                allowed_hold_id=allowed_hold_id,
                close_waitlist=False,
                ignore_booking_id=int(current_booking_id),
            )
            conn.execute("DELETE FROM bookings WHERE id=?", (int(current_booking_id),))
            _commit(raw)
            return old, new
        except BookingError:
            _rollback(raw)
            raise
        except Exception as exc:
            _rollback(raw)
            raise SlotBusy("Не удалось безопасно перенести запись") from exc
        finally:
            _close_transaction(conn, raw)
