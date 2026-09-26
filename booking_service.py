from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from config import TIMEZONE, WORKING_HOURS
from database import get_conn, get_user_bookings_today, get_free_hours_effective

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


def slot_datetime(date_iso: str, hour: int) -> datetime:
    d = datetime.fromisoformat(str(date_iso)).date()
    return datetime.combine(d, time(hour=int(hour)), tzinfo=TZ)


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
    if not row:
        return None
    bid, uid, mid, mtype, mname, d, h = row
    ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
    return BookingResult(int(bid), int(uid), int(mid), str(mtype), str(mname), ds, int(h))


async def create_booking_safe(
    user_id: int,
    machine_id: int,
    date_iso: str,
    hour: int,
    *,
    allowed_hold_id: int | None = None,
    close_waitlist: bool = True,
) -> BookingResult:
    hour = int(hour)
    if hour not in WORKING_HOURS:
        raise InvalidBooking("Недоступное время")
    if slot_datetime(date_iso, hour) <= datetime.now(TZ):
        raise InvalidBooking("Это время уже прошло")

    async with _BOOKING_LOCK:
        with get_conn() as conn:
            machine = conn.execute(
                "SELECT type,name,is_active FROM machines WHERE id=?",
                (int(machine_id),),
            ).fetchone()
        if not machine:
            raise InvalidBooking("Машина не найдена")

        mtype, mname, active = machine
        if not active:
            raise InvalidBooking("Машина сейчас недоступна")

        if get_user_bookings_today(int(user_id), str(date_iso), str(mtype)):
            raise DailyLimit("На этот тип машины уже есть запись в этот день")

        free = get_free_hours_effective(int(machine_id), str(date_iso))
        if hour not in free:
            if allowed_hold_id is None:
                raise SlotBusy("Слот уже занят или временно зарезервирован")
            with get_conn() as conn:
                own = conn.execute(
                    """
                    SELECT 1 FROM slot_holds
                    WHERE id=? AND user_id=? AND machine_id=? AND date=? AND hour=?
                      AND status='active' AND expires_at>?
                    """,
                    (
                        int(allowed_hold_id),
                        int(user_id),
                        int(machine_id),
                        str(date_iso),
                        hour,
                        datetime.now(TZ).isoformat(timespec="seconds"),
                    ),
                ).fetchone()
            if not own:
                raise SlotBusy("Резерв этого слота уже недействителен")

        try:
            with get_conn() as conn:
                cur = conn.execute(
                    "INSERT INTO bookings (user_id,machine_id,date,hour) VALUES (?,?,?,?)",
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
                    now_s = datetime.now(TZ).isoformat(timespec="seconds")
                    conn.execute(
                        """
                        UPDATE waitlist_requests
                        SET status='matched',matched_booking_id=?,updated_at=?
                        WHERE user_id=? AND status='active'
                        """,
                        (int(booking_id), now_s, int(user_id)),
                    )
        except Exception as exc:
            raise SlotBusy("Слот только что заняли") from exc

        return BookingResult(
            int(booking_id),
            int(user_id),
            int(machine_id),
            str(mtype),
            str(mname),
            str(date_iso),
            hour,
        )


async def cancel_booking_safe(
    booking_id: int,
    *,
    require_future: bool = True,
) -> BookingResult:
    async with _BOOKING_LOCK:
        booking = get_booking(int(booking_id))
        if not booking:
            raise InvalidBooking("Запись не найдена")

        if require_future and slot_datetime(booking.date, booking.hour) <= datetime.now(TZ):
            raise InvalidBooking("Начавшуюся запись отменить нельзя")

        with get_conn() as conn:
            conn.execute("DELETE FROM bookings WHERE id=?", (int(booking_id),))
        return booking


async def move_booking_safe(
    current_booking_id: int,
    new_machine_id: int,
    new_date: str,
    new_hour: int,
    *,
    allowed_hold_id: int | None = None,
) -> tuple[BookingResult, BookingResult]:
    old = get_booking(int(current_booking_id))
    if not old:
        raise InvalidBooking("Исходная запись не найдена")
    if slot_datetime(old.date, old.hour) <= datetime.now(TZ):
        raise InvalidBooking("Начавшуюся запись переносить нельзя")

    new = await create_booking_safe(
        old.user_id,
        int(new_machine_id),
        str(new_date),
        int(new_hour),
        allowed_hold_id=allowed_hold_id,
        close_waitlist=False,
    )
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM bookings WHERE id=?", (int(current_booking_id),))
    except Exception:
        try:
            with get_conn() as conn:
                conn.execute("DELETE FROM bookings WHERE id=?", (int(new.booking_id),))
        except Exception:
            pass
        raise
    return old, new
