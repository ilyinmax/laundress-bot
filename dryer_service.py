from __future__ import annotations

from dataclasses import dataclass

from config import WORKING_HOURS
from database import get_conn, get_free_hours_effective


@dataclass(frozen=True)
class DryerOffer:
    machine_id: int
    machine_name: str
    date: str
    hour: int


def has_wash_booking(user_id: int, date_iso: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE b.user_id=? AND b.date=? AND m.type='wash'
            LIMIT 1
            """,
            (int(user_id), str(date_iso)),
        ).fetchone()
    return bool(row)


def has_dry_booking(user_id: int, date_iso: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE b.user_id=? AND b.date=? AND m.type='dry'
            LIMIT 1
            """,
            (int(user_id), str(date_iso)),
        ).fetchone()
    return bool(row)


def find_next_dryer(user_id: int, date_iso: str, wash_hour: int) -> DryerOffer | None:
    """Return the first free dryer exactly one hour after a wash."""
    next_hour = int(wash_hour) + 1
    if next_hour not in WORKING_HOURS:
        return None
    if has_dry_booking(int(user_id), str(date_iso)):
        return None

    with get_conn() as conn:
        dryers = conn.execute(
            """
            SELECT id,name
            FROM machines
            WHERE type='dry' AND is_active
            ORDER BY id
            """
        ).fetchall()

    for machine_id, machine_name in dryers:
        if next_hour in get_free_hours_effective(int(machine_id), str(date_iso)):
            return DryerOffer(
                machine_id=int(machine_id),
                machine_name=str(machine_name),
                date=str(date_iso),
                hour=next_hour,
            )
    return None
