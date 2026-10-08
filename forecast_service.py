"""Read-only, shared, on-demand estimate for persistent laundry subscriptions.

The actual night/day allocator is the only authority for bookings. This module
never inserts bookings or HOLDs, and never runs a recurring database job.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, time
from time import monotonic
from zoneinfo import ZoneInfo

from config import TIMEZONE, WORKING_HOURS
from database import get_availability_bulk, get_conn
import waitlist_service as waitlist

TZ = ZoneInfo(TIMEZONE)
CACHE_SECONDS = 600
_lock = asyncio.Lock()
_cache: dict | None = None


def invalidate_forecasts() -> None:
    """Clear forecasts when a local subscription change affects matching."""
    global _cache
    _cache = None


def _weight(occurred_at: datetime, at: datetime) -> int:
    age = (at - occurred_at).days
    if age < 0 or age > 30:
        return 0
    if age <= 7:
        return 4
    if age <= 14:
        return 3
    if age <= 21:
        return 2
    return 1


def _date_name(d) -> str:
    months = ("", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
    return f"{d.day} {months[d.month]}"


def _build_snapshot(now: datetime) -> dict:
    """Batch availability and history once for the entire queue."""
    requests = waitlist._active_requests()
    if not requests:
        return {"by_request": {}, "waiting": 0}
    original = {req.id: req for req in requests}
    score_by_id = {rid: req.queue_score(now) for rid, req in original.items()}
    scores = list(score_by_id.values())
    ids = {req.user_id for req in requests}
    horizon = [now.date() + timedelta(days=i) for i in range(8)]
    machines, available = get_availability_bulk([d.isoformat() for d in horizon])
    wash_ids = {int(mid) for mid, typ, _name in machines if str(typ) == "wash"}

    with get_conn() as conn:
        # Users with an upcoming wash will rejoin automatically afterwards.
        # They must count as future competitors, not disappear from the
        # forecast and give current waiters a falsely optimistic date.
        booked = conn.execute(
            """
            SELECT wr.id,wr.user_id,u.tg_id,wr.mode,wr.any_machine,
                   b.date,b.hour
            FROM waitlist_requests wr
            JOIN users u ON u.id=wr.user_id
            JOIN bookings b ON b.id=wr.matched_booking_id
            JOIN machines m ON m.id=b.machine_id AND m.type='wash'
            WHERE wr.persistent=1 AND wr.status IN ('matched','paused')
              AND b.date>=?
            """,
            (now.date().isoformat(),),
        ).fetchall()
        schedule_rows = conn.execute(
            "SELECT request_id,weekday,start_hour,end_hour FROM waitlist_schedule"
        ).fetchall()
        machine_rows = conn.execute(
            "SELECT request_id,machine_id FROM waitlist_machines"
        ).fetchall()
        history = conn.execute(
            "SELECT user_id,occurred_at FROM laundry_usage_history WHERE occurred_at>=?",
            ((now - timedelta(days=30)).isoformat(timespec="seconds"),),
        ).fetchall()
    future_schedule = {}
    for rid, day, a, b in schedule_rows:
        future_schedule.setdefault(int(rid), {}).setdefault(int(day), []).append(
            (int(a), int(b))
        )
    future_machines = {}
    for rid, mid in machine_rows:
        future_machines.setdefault(int(rid), set()).add(int(mid))
    # Mutable clones: simulations never mutate the real live Request objects.
    simulated = {rid: replace(req) for rid, req in original.items()}
    for rid, uid, tg, mode, any_machine, wash_date, wash_hour in booked:
        rid, uid = int(rid), int(uid)
        if rid in simulated:
            continue
        date_iso = wash_date.isoformat() if hasattr(wash_date, "isoformat") else str(wash_date)
        wash_at = datetime.combine(datetime.fromisoformat(date_iso).date(), time(int(wash_hour)), tzinfo=TZ)
        end_at = wash_at + timedelta(hours=1)
        if end_at < now or not future_schedule.get(rid):
            continue
        schedule = future_schedule[rid]
        simulated[rid] = waitlist.Request(
            rid, uid, int(tg), str(mode), bool(any_machine),
            end_at, [], future_machines.get(rid, set()),
            set(schedule), schedule,
        )
        ids.add(uid)
    occurred: dict[int, list[datetime]] = {uid: [] for uid in ids}
    for uid, at in history:
        if int(uid) not in ids:
            continue
        try:
            parsed = waitlist._dt(at)
        except (ValueError, TypeError):
            continue
        occurred[int(uid)].append(parsed)

    for rid, uid, tg, mode, any_machine, wash_date, wash_hour in booked:
        rid, uid = int(rid), int(uid)
        if rid not in simulated:
            continue
        date_iso = wash_date.isoformat() if hasattr(wash_date, "isoformat") else str(wash_date)
        wash_at = datetime.combine(datetime.fromisoformat(date_iso).date(), time(int(wash_hour)), tzinfo=TZ)
        if wash_at > now:
            occurred[uid].append(wash_at)
    first_match = {}
    for d in horizon:
        date_iso = d.isoformat()
        if not wash_ids:
            break
        # An unopened date is first allocated at 23:00 three days earlier.
        cutoff = datetime.combine(d - timedelta(days=3), time(23), tzinfo=TZ)
        at = max(now, cutoff)
        slots = []
        for mid in sorted(wash_ids):
            for hour in available.get(date_iso, {}).get(mid, []):
                if d == now.date() and hour <= now.hour:
                    continue
                slots.append((mid, int(hour)))
        if not slots:
            continue
        eligible = []
        for req in simulated.values():
            if not req.accepts_date(date_iso) or req.priority_since > at:
                continue
            req.usage_points = sum(_weight(x, at) for x in occurred[req.user_id])
            eligible.append(req)
        matches = waitlist._match(eligible, slots, date_iso=date_iso, now=at)
        for rid, (_mid, hour) in matches.items():
            if rid not in first_match:
                first_match[rid] = d
            req = simulated[rid]
            # Forecast winners are assumed to wash, then rejoin the queue at
            # the end of their wash, with the same 30-day usage penalty.
            req.priority_since = datetime.combine(d, time(int(hour)), tzinfo=TZ) + timedelta(hours=1)
            occurred[req.user_id].append(datetime.combine(d, time(int(hour)), tzinfo=TZ))

    result = {}
    for rid, req in original.items():
        score = score_by_id[rid]
        below = sum(value < score for value in scores)
        percent = round(100 * below / len(scores))
        first = first_match.get(rid)
        hours = 0
        for d in horizon:
            if req.accepts_date(d.isoformat()):
                hours = len({
                    hour for a, b in req.intervals_for_date(d.isoformat())
                    for hour in range(a, b) if hour in WORKING_HOURS
                })
                break
        if first:
            days = (first - now.date()).days
            if days <= 2:
                chance = "🟢↗️ Шансы выше среднего"
            elif days <= 4:
                chance = "➖ Средние шансы"
            else:
                chance = "↘️ Шансы ниже среднего"
            # Beyond currently open dates the estimate is uncertain. Always
            # show a range and never promise a guaranteed wash.
            last = first + timedelta(days=2)
            date_label = (
                _date_name(first) if days <= 2
                else f"{_date_name(first)} – {_date_name(last)}"
            )
        else:
            chance = "↘️ Пока шансы невысокие"
            date_label = "Пока не удаётся определить"
        result[rid] = {
            "score": score,
            "percent": percent,
            "waiting": len(requests),
            "hours": hours,
            "chance": chance,
            "date": date_label,
        }
    return {"by_request": result, "waiting": len(requests)}


async def get_forecast(request_id: int) -> dict | None:
    """One shared refresh for concurrent viewers; no background DB polling."""
    global _cache
    now_mono = monotonic()
    if _cache is not None and _cache["valid_until"] > now_mono:
        return _cache["snapshot"]["by_request"].get(int(request_id))
    async with _lock:
        if _cache is None or _cache["valid_until"] <= monotonic():
            snapshot = _build_snapshot(datetime.now(TZ))
            _cache = {"snapshot": snapshot, "valid_until": monotonic() + CACHE_SECONDS}
        return _cache["snapshot"]["by_request"].get(int(request_id))
