from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import TIMEZONE, WORKING_HOURS
from database import (
    get_conn,
    get_user,
    is_banned,
    get_free_hours_effective,
    get_availability_bulk,
    get_notification_settings,
    set_notification_setting,
)
from keyboards import build_main_menu, reply_menu
from booking_service import create_booking_safe, DailyLimit, SlotBusy, InvalidBooking
from dryer_service import has_wash_booking, find_next_dryer
from waitlist_service import (
    get_active_request_for_tg,
    cancel_request_for_tg,
    save_request,
    check_active_waitlist,
    accept_hold,
    decline_hold,
    get_hold,
    WAITLIST_ENABLED,
)

TZ = ZoneInfo(TIMEZONE)
router = Router()

MONTHS = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


class BookFlow(StatesGroup):
    date = State()
    machine = State()
    hour = State()


class CancelFlow(StatesGroup):
    choose = State()
    confirm = State()


class WaitFlow(StatesGroup):
    menu = State()
    schedule_mode = State()
    interval_start = State()
    interval_end = State()
    intervals = State()
    weekdays = State()
    schedule_days = State()
    day_intervals = State()
    copy_days = State()
    machines = State()
    mode = State()
    confirm = State()


class HelpFlow(StatesGroup):
    menu = State()


class NotificationFlow(StatesGroup):
    quiet_start = State()
    quiet_end = State()


def active_waitlist(tg_id: int) -> bool:
    return bool(get_active_request_for_tg(int(tg_id)))


def main_kb(tg_id: int):
    return build_main_menu(active_waitlist(tg_id))


def date_text(date_iso: str) -> str:
    d = datetime.fromisoformat(str(date_iso)).date()
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month]}"


def free_slots_per_type(date_iso: str) -> tuple[int, int]:
    """Count free hourly slots for active washers and dryers."""
    now = datetime.now(TZ)
    today_iso = now.date().isoformat()

    with get_conn() as conn:
        machines = conn.execute(
            "SELECT id,type FROM machines WHERE is_active ORDER BY type,name"
        ).fetchall()

    wash_slots = 0
    dry_slots = 0
    for machine_id, machine_type in machines:
        hours = get_free_hours_effective(int(machine_id), str(date_iso))
        if str(date_iso) == today_iso:
            hours = [hour for hour in hours if int(hour) > now.hour]
        if str(machine_type) == "wash":
            wash_slots += len(hours)
        elif str(machine_type) == "dry":
            dry_slots += len(hours)

    return wash_slots, dry_slots


def booking_dates() -> list[tuple[str, str]]:
    now = datetime.now(TZ)
    today = now.date()
    start = 1 if now.hour >= 23 else 0
    count = 2 if now.hour >= 23 else 3
    raw_dates = [(offset, today + timedelta(days=offset)) for offset in range(start, start + count)]
    date_isos = [d.isoformat() for _, d in raw_dates]
    machines, availability = get_availability_bulk(date_isos)

    out = []
    today_iso = today.isoformat()
    for offset, d in raw_dates:
        date_iso = d.isoformat()
        if offset == 0:
            prefix = "Сегодня"
        elif offset == 1:
            prefix = "Завтра"
        elif offset == 2:
            prefix = "Послезавтра"
        else:
            prefix = date_text(date_iso)

        free_wash = 0
        free_dry = 0
        for machine_id, machine_type, _name in machines:
            hours = list(availability.get(date_iso, {}).get(int(machine_id), []))
            if date_iso == today_iso:
                hours = [h for h in hours if h > now.hour]
            if str(machine_type) == "wash":
                free_wash += len(hours)
            elif str(machine_type) == "dry":
                free_dry += len(hours)

        label = (
            f"📅 {prefix}, {d.day} {MONTHS[d.month]}"
            f" • 🧺 {free_wash} / 🌬️ {free_dry}"
        )
        out.append((label, date_iso))
    return out


def nav_rows(back: bool = True) -> list[list[str]]:
    return [["⬅️ Назад", "🏠 Главное меню"]] if back else [["🏠 Главное меню"]]


async def show_home(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("🏠 Главное меню", reply_markup=main_kb(msg.from_user.id))


@router.message(Command("menu"))
@router.message(F.text == "🏠 Главное меню")
async def home(msg: types.Message, state: FSMContext):
    await show_home(msg, state)


@router.message(Command("book"))
@router.message(F.text == "🧺 Записаться")
async def start_booking(msg: types.Message, state: FSMContext):
    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете записываться.", reply_markup=main_kb(msg.from_user.id))
    user = get_user(msg.from_user.id)
    if not user or not user[2] or not user[3]:
        return await msg.answer("Сначала завершите регистрацию через /start.")
    dates = booking_dates()
    await state.clear()
    await state.set_state(BookFlow.date)
    await state.update_data(date_map={label: iso for label, iso in dates})
    await msg.answer(
        "📅 Выберите дату:",
        reply_markup=reply_menu([[label] for label, _ in dates] + nav_rows(False)),
    )


@router.message(BookFlow.date)
async def choose_date(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    data = await state.get_data()
    date_iso = (data.get("date_map") or {}).get(msg.text)
    if not date_iso:
        return await msg.answer("Выберите дату кнопкой ниже.")

    now = datetime.now(TZ)
    machines, availability = get_availability_bulk([date_iso])

    lines = [f"📅 <b>{date_text(date_iso)}</b>", ""]
    machine_map = {}
    user = get_user(msg.from_user.id)
    wash_exists = bool(user and has_wash_booking(int(user[0]), date_iso))
    for mid, mtype, name in machines:
        if str(mtype) == "dry" and not wash_exists:
            continue
        hours = list(availability.get(date_iso, {}).get(int(mid), []))
        if date_iso == now.date().isoformat():
            hours = [h for h in hours if h > now.hour]
        if not hours:
            continue
        emoji = "🧺" if str(mtype) == "wash" else "🌬️"
        lines.append(f"{emoji} <b>{name}</b>")
        lines.append("  " + "  ".join(f"{h:02d}:00" for h in hours))
        lines.append("")
        machine_map[f"{emoji} {name}"] = int(mid)

    if not machine_map:
        await state.clear()
        return await msg.answer(
            "На эту дату свободных записей нет.",
            reply_markup=main_kb(msg.from_user.id),
        )

    await state.set_state(BookFlow.machine)
    await state.update_data(date=date_iso, machine_map=machine_map)
    rows = [[label] for label in machine_map] + nav_rows()
    await msg.answer(
        "\n".join(lines).rstrip() + "\n\nВыберите машинку 👇",
        parse_mode="HTML",
        reply_markup=reply_menu(rows),
    )


@router.message(BookFlow.machine)
async def choose_machine(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        return await start_booking(msg, state)

    data = await state.get_data()
    mid = (data.get("machine_map") or {}).get(msg.text)
    if not mid:
        return await msg.answer("Выберите машинку кнопкой ниже.")
    date_iso = data["date"]
    hours = get_free_hours_effective(int(mid), date_iso)
    now = datetime.now(TZ)
    if date_iso == now.date().isoformat():
        hours = [h for h in hours if h > now.hour]
    if not hours:
        return await msg.answer("Эти свободные часы уже заняли. Выберите другую машинку.")

    with get_conn() as conn:
        row = conn.execute("SELECT type,name FROM machines WHERE id=?", (int(mid),)).fetchone()
    emoji = "🧺" if row and row[0] == "wash" else "🌬️"
    hour_map = {f"{h:02d}:00": int(h) for h in hours}
    await state.set_state(BookFlow.hour)
    await state.update_data(machine_id=int(mid), hour_map=hour_map)
    buttons = list(hour_map)
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)] + nav_rows()
    await msg.answer(
        f"{emoji} <b>{row[1]}</b>\n📅 {date_text(date_iso)}\n\nВыберите свободное время:",
        parse_mode="HTML",
        reply_markup=reply_menu(rows),
    )


@router.message(BookFlow.hour)
async def choose_hour(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        return await start_booking(msg, state)

    data = await state.get_data()
    hour = (data.get("hour_map") or {}).get(msg.text)
    if hour is None:
        return await msg.answer("Выберите время кнопкой ниже.")

    user = get_user(msg.from_user.id)
    try:
        result = await create_booking_safe(
            int(user[0]), int(data["machine_id"]), str(data["date"]), int(hour)
        )
    except DailyLimit:
        await state.clear()
        return await msg.answer(
            "⚠️ У вас уже есть запись на этот тип машины в этот день.",
            reply_markup=main_kb(msg.from_user.id),
        )
    except (SlotBusy, InvalidBooking):
        await state.clear()
        return await msg.answer(
            "Этот слот уже недоступен. Откройте запись ещё раз.",
            reply_markup=main_kb(msg.from_user.id),
        )

    from handlers.laundry_features import schedule_reminder
    await schedule_reminder(msg.from_user.id, result.machine_name, result.date, result.hour, 30)
    await state.clear()
    await msg.answer(
        "✅ <b>Запись создана</b>\n\n"
        f"📅 {date_text(result.date)}\n"
        f"🕐 {result.hour:02d}:00\n"
        f"{'🧺' if result.machine_type == 'wash' else '🌬️'} {result.machine_name}",
        parse_mode="HTML",
        reply_markup=main_kb(msg.from_user.id),
    )

    if result.machine_type == "wash":
        offer = find_next_dryer(result.user_id, result.date, result.hour)
        if offer:
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
            await msg.answer(
                "🌬️ <b>Нужна сушка после стирки?</b>\n\n"
                f"Свободна <b>{offer.machine_name}</b>\n"
                f"сразу после вашей стирки, в {offer.hour:02d}:00.",
                parse_mode="HTML",
                reply_markup=kb,
            )


def future_bookings(tg_id: int):
    now = datetime.now(TZ)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.id,m.type,m.name,b.date,b.hour
            FROM bookings b
            JOIN users u ON u.id=b.user_id
            JOIN machines m ON m.id=b.machine_id
            WHERE u.tg_id=?
            ORDER BY b.date,b.hour
            """,
            (int(tg_id),),
        ).fetchall()
    out = []
    for bid, mtype, name, d, h in rows:
        ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
        slot = datetime.fromisoformat(ds).replace(tzinfo=TZ, hour=int(h))
        if slot > now:
            out.append((int(bid), str(mtype), str(name), ds, int(h)))
    return out


@router.message(Command("mybookings"))
@router.message(F.text == "📋 Мои записи")
async def my_bookings(msg: types.Message, state: FSMContext):
    await state.clear()
    rows = future_bookings(msg.from_user.id)
    if not rows:
        return await msg.answer("У вас нет предстоящих записей.", reply_markup=main_kb(msg.from_user.id))
    lines = ["📋 <b>Мои записи</b>", ""]
    for _, typ, name, date_iso, hour in rows:
        lines += [
            f"{'🧺' if typ == 'wash' else '🌬️'} <b>{name}</b>",
            f"📅 {date_text(date_iso)}",
            f"🕐 {hour:02d}:00",
            "",
        ]
    await msg.answer("\n".join(lines).rstrip(), parse_mode="HTML", reply_markup=main_kb(msg.from_user.id))


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отменить запись")
async def cancel_start(msg: types.Message, state: FSMContext):
    rows = future_bookings(msg.from_user.id)
    if not rows:
        return await msg.answer("У вас нет предстоящих записей.", reply_markup=main_kb(msg.from_user.id))
    labels = {}
    buttons = []
    for bid, typ, name, date_iso, hour in rows:
        label = f"{'🧺' if typ == 'wash' else '🌬️'} {date_text(date_iso)}, {hour:02d}:00, {name}"
        labels[label] = bid
        buttons.append([label])
    await state.set_state(CancelFlow.choose)
    await state.update_data(cancel_map=labels)
    await msg.answer("Какую запись отменить?", reply_markup=reply_menu(buttons + nav_rows(False)))


@router.message(CancelFlow.choose)
async def cancel_choose(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    data = await state.get_data()
    bid = (data.get("cancel_map") or {}).get(msg.text)
    if not bid:
        return await msg.answer("Выберите запись кнопкой ниже.")
    await state.set_state(CancelFlow.confirm)
    await state.update_data(cancel_id=int(bid))
    await msg.answer(
        "Точно отменить эту запись?",
        reply_markup=reply_menu([["✅ Да, отменить", "❌ Нет"]]),
    )


@router.message(CancelFlow.confirm)
async def cancel_confirm(msg: types.Message, state: FSMContext):
    if msg.text == "❌ Нет":
        return await show_home(msg, state)
    if msg.text != "✅ Да, отменить":
        return await msg.answer("Выберите действие кнопкой ниже.")
    from booking_service import cancel_booking_safe, InvalidBooking
    data = await state.get_data()
    try:
        old = await cancel_booking_safe(int(data["cancel_id"]))
    except InvalidBooking as exc:
        await state.clear()
        return await msg.answer(str(exc), reply_markup=main_kb(msg.from_user.id))
    await state.clear()
    if getattr(old, "waitlist_reopened", False):
        await msg.answer(
            "✅ Запись отменена.\n\n"
            "Ваша заявка в листе ожидания снова активна, а накопленный приоритет сохранён.",
            reply_markup=main_kb(msg.from_user.id),
        )
    else:
        await msg.answer("✅ Запись отменена.", reply_markup=main_kb(msg.from_user.id))
    from waitlist_service import distribute_date
    await distribute_date(old.date, context="day")


def _schedule_from_rows(rows) -> dict[int, list[tuple[int, int]]]:
    schedule: dict[int, list[tuple[int, int]]] = {}
    for weekday, start_hour, end_hour in rows:
        schedule.setdefault(int(weekday), []).append((int(start_hour), int(end_hour)))
    return schedule


def _format_interval(start_hour: int, end_hour_exclusive: int) -> str:
    """Show the first and last possible wash start, not the internal exclusive end."""
    start = int(start_hour)
    last_start = int(end_hour_exclusive) - 1
    if start == last_start:
        return f"{start:02d}:00"
    return f"{start:02d}:00-{last_start:02d}:00"


def _format_intervals(intervals: list[tuple[int, int]]) -> str:
    return ", ".join(_format_interval(a, b) for a, b in intervals)


def _interval_end_choices(start_hour: int) -> list[int]:
    """Visible choices are possible start times, including 22:00 but never 23:00."""
    return list(range(int(start_hour), max(WORKING_HOURS) + 1))


def _format_schedule(schedule: dict[int, list[tuple[int, int]]]) -> str:
    if not schedule:
        return "Не настроено"

    groups: dict[tuple[tuple[int, int], ...], list[int]] = {}
    for weekday in sorted(schedule):
        key = tuple((int(a), int(b)) for a, b in schedule[weekday])
        groups.setdefault(key, []).append(int(weekday))

    lines = []
    for intervals, days in groups.items():
        day_text = "Каждый день" if days == list(range(7)) else ", ".join(WEEKDAY_SHORT[d] for d in days)
        lines.append(f"📆 {day_text}\n🕐 {_format_intervals(list(intervals))}")
    return "\n\n".join(lines)


def _subscription_state_text(request_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT wr.status,b.date,b.hour
            FROM waitlist_requests wr
            LEFT JOIN bookings b ON b.id=wr.matched_booking_id
            WHERE wr.id=?
            """,
            (int(request_id),),
        ).fetchone()
    if not row:
        return ""

    status, date_value, hour = str(row[0]), row[1], row[2]
    if status == "active":
        return "🟢 Подписка участвует в очереди."

    if date_value is None or hour is None:
        return "⏸ Подписка временно приостановлена и включится автоматически после текущей стирки."

    date_iso = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
    if status == "paused":
        end_hour = int(hour) + 1
        return (
            "✅ Подписка сохранена.\n"
            f"У вас уже есть запись {date_text(date_iso)} "
            f"с {int(hour):02d}:00 до {end_hour:02d}:00, поэтому до неё "
            "подписка не участвует в распределении.\n"
            f"Приоритет начнёт считаться с {end_hour:02d}:00 после этой стирки."
        )

    return (
        "✅ Подписка сохранена.\n"
        f"Сейчас у вас уже есть запись {date_text(date_iso)} "
        f"в {int(hour):02d}:00. После этой стирки подписка "
        "автоматически снова начнёт участвовать в очереди с новым приоритетом."
    )


def _load_waitlist_form(tg_id: int) -> dict:
    req = get_active_request_for_tg(tg_id)
    if not req:
        return {}

    rid, mode, any_machine, _created_at, _priority_since = req
    with get_conn() as conn:
        schedule_rows = conn.execute(
            """
            SELECT weekday,start_hour,end_hour
            FROM waitlist_schedule
            WHERE request_id=?
            ORDER BY weekday,start_hour
            """,
            (int(rid),),
        ).fetchall()
        if not schedule_rows:
            intervals = [
                (int(a), int(b))
                for a, b in conn.execute(
                    "SELECT start_hour,end_hour FROM waitlist_intervals WHERE request_id=? ORDER BY start_hour",
                    (int(rid),),
                ).fetchall()
            ]
            weekdays = [
                int(r[0])
                for r in conn.execute(
                    "SELECT weekday FROM waitlist_weekdays WHERE request_id=? ORDER BY weekday",
                    (int(rid),),
                ).fetchall()
            ]
            days = weekdays or list(range(7))
            schedule = {day: list(intervals) for day in days}
        else:
            schedule = _schedule_from_rows(schedule_rows)

        machine_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT machine_id FROM waitlist_machines WHERE request_id=? ORDER BY machine_id",
                (int(rid),),
            ).fetchall()
        ]

    return {
        "schedule": schedule,
        "selected_machines": machine_ids,
        "any_machine": bool(any_machine),
        "mode": str(mode),
    }


def _waitlist_summary(tg_id: int) -> str:
    req = get_active_request_for_tg(tg_id)
    if not req:
        return ""
    rid, mode, any_machine, _created_at, _priority_since = req
    with get_conn() as conn:
        schedule_rows = conn.execute(
            """
            SELECT weekday,start_hour,end_hour
            FROM waitlist_schedule
            WHERE request_id=?
            ORDER BY weekday,start_hour
            """,
            (int(rid),),
        ).fetchall()
        machines = conn.execute(
            """
            SELECT m.name FROM waitlist_machines wm
            JOIN machines m ON m.id=wm.machine_id
            WHERE wm.request_id=? ORDER BY m.name
            """,
            (int(rid),),
        ).fetchall()

        if schedule_rows:
            schedule = _schedule_from_rows(schedule_rows)
        else:
            intervals = [
                (int(a), int(b))
                for a, b in conn.execute(
                    "SELECT start_hour,end_hour FROM waitlist_intervals WHERE request_id=? ORDER BY start_hour",
                    (int(rid),),
                ).fetchall()
            ]
            weekdays = [
                int(r[0])
                for r in conn.execute(
                    "SELECT weekday FROM waitlist_weekdays WHERE request_id=? ORDER BY weekday",
                    (int(rid),),
                ).fetchall()
            ]
            schedule = {day: list(intervals) for day in (weekdays or list(range(7)))}

    machine_text = "Любая стиральная машина" if any_machine else ", ".join(str(x[0]) for x in machines)
    mode_text = "Автозапись" if mode == "auto" else "Сначала спросить"
    state_text = _subscription_state_text(int(rid))
    return (
        "🔔 <b>Подписка на стирку</b>\n\n"
        f"{_format_schedule(schedule)}\n\n"
        f"🧺 {machine_text}\n"
        f"⚡ Режим: {mode_text}\n\n"
        f"{state_text}"
    )


@router.message(F.text.startswith("🔔 Лист ожидания"))
async def waitlist_home(msg: types.Message, state: FSMContext):
    await state.clear()
    if not WAITLIST_ENABLED:
        return await msg.answer(
            "🔧 Лист ожидания временно недоступен. Обычная запись работает как обычно.",
            reply_markup=main_kb(msg.from_user.id),
        )
    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете использовать лист ожидания.", reply_markup=main_kb(msg.from_user.id))
    user = get_user(msg.from_user.id)
    if not user or not user[2] or not user[3]:
        return await msg.answer("Сначала завершите регистрацию через /start.")
    summary = _waitlist_summary(msg.from_user.id)
    if summary:
        rows = [
            ["✏️ Изменить заявку"],
            ["❌ Отменить заявку"],
            ["⚙️ Настройки уведомлений"],
            ["ℹ️ Как работает лист ожидания"],
            ["🏠 Главное меню"],
        ]
        return await msg.answer(summary, parse_mode="HTML", reply_markup=reply_menu(rows))
    rows = [
        ["➕ Создать заявку"],
        ["⚙️ Настройки уведомлений"],
        ["ℹ️ Как работает лист ожидания"],
        ["🏠 Главное меню"],
    ]
    await msg.answer(
        "🔔 <b>Лист ожидания</b>\n\n"
        "Укажите удобное время и машинки, а бот будет искать подходящую свободную запись.",
        parse_mode="HTML",
        reply_markup=reply_menu(rows),
    )


@router.message(F.text.in_({"➕ Создать заявку", "✏️ Изменить заявку"}))
async def waitlist_create(msg: types.Message, state: FSMContext):
    await state.clear()
    if not WAITLIST_ENABLED:
        return await msg.answer(
            "🔧 Лист ожидания временно недоступен.",
            reply_markup=main_kb(msg.from_user.id),
        )

    is_edit = msg.text == "✏️ Изменить заявку"
    current = _load_waitlist_form(msg.from_user.id) if is_edit else {}
    await state.update_data(
        is_edit=is_edit,
        schedule=current.get("schedule", {}),
        selected_machines=current.get("selected_machines", []),
        any_machine=current.get("any_machine", True),
        mode=current.get("mode", "auto"),
        intervals=[],
        selected_weekdays=[],
        any_day=True,
    )
    await show_schedule_mode(msg, state)


async def show_schedule_mode(msg: types.Message, state: FSMContext):
    await state.set_state(WaitFlow.schedule_mode)
    await msg.answer(
        "📆 <b>Как настроить удобное время?</b>\n\n"
        "🕐 <b>Одинаковое время по дням</b>\n"
        "Например: Пн, Ср и Пт с 18:00 до 22:00.\n\n"
        "📆 <b>Разное время по дням</b>\n"
        "Например: Пн 10:00-22:00, а Вт 18:00-22:00.",
        parse_mode="HTML",
        reply_markup=reply_menu([
            ["🕐 Одинаковое время по дням"],
            ["📆 Разное время по дням"],
            ["🏠 Главное меню"],
        ]),
    )


@router.message(WaitFlow.schedule_mode)
async def waitlist_schedule_mode(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)

    if msg.text == "🕐 Одинаковое время по дням":
        await state.update_data(
            schedule_mode="common",
            intervals=[],
            selected_weekdays=[],
            any_day=True,
        )
        return await show_common_intervals(msg, state)

    if msg.text == "📆 Разное время по дням":
        await state.update_data(schedule_mode="flexible")
        return await show_schedule_days(msg, state)

    await msg.answer("Выберите способ настройки кнопкой ниже.")


def _state_schedule(data: dict) -> dict[int, list[tuple[int, int]]]:
    out: dict[int, list[tuple[int, int]]] = {}
    for raw_day, raw_intervals in (data.get("schedule") or {}).items():
        day = int(raw_day)
        out[day] = [(int(a), int(b)) for a, b in raw_intervals]
    return out


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((int(a), int(b)) for a, b in intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


async def show_common_intervals(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    intervals = [(int(a), int(b)) for a, b in data.get("intervals", [])]
    await state.set_state(WaitFlow.intervals)
    if intervals:
        text = "🕐 <b>Одинаковое время</b>\n\n" + "\n".join(
            f"• {_format_interval(a, b)}" for a, b in intervals
        )
    else:
        text = "🕐 <b>Одинаковое время</b>\n\nДобавьте до 3 удобных интервалов."
    rows = []
    if len(intervals) < 3:
        rows.append(["➕ Добавить интервал"])
    rows += [["✅ Продолжить"], ["⬅️ Назад", "🏠 Главное меню"]]
    await msg.answer(text, parse_mode="HTML", reply_markup=reply_menu(rows))


@router.message(WaitFlow.intervals)
async def interval_menu(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        return await show_schedule_mode(msg, state)
    if msg.text == "➕ Добавить интервал":
        return await start_interval_picker(msg, state, target="common")
    if msg.text == "✅ Продолжить":
        data = await state.get_data()
        if not data.get("intervals"):
            return await msg.answer("Добавьте хотя бы один интервал.")
        return await show_waitlist_weekdays(msg, state)
    await msg.answer("Выберите действие кнопкой ниже.")


async def start_interval_picker(
    msg: types.Message,
    state: FSMContext,
    *,
    target: str,
) -> None:
    data = await state.get_data()
    if target == "day":
        weekday = int(data["current_weekday"])
        intervals = _state_schedule(data).get(weekday, [])
    else:
        intervals = [(int(a), int(b)) for a, b in data.get("intervals", [])]

    if len(intervals) >= 3:
        await msg.answer("Можно добавить максимум 3 интервала на один день.")
        return

    starts = [f"{h:02d}:00" for h in WORKING_HOURS]
    await state.set_state(WaitFlow.interval_start)
    await state.update_data(
        interval_target=target,
        start_map={f"{h:02d}:00": h for h in WORKING_HOURS},
    )
    rows = [starts[i:i + 4] for i in range(0, len(starts), 4)] + [["⬅️ Назад"]]
    await msg.answer("Выберите начало интервала:", reply_markup=reply_menu(rows))


@router.message(WaitFlow.interval_start)
async def interval_start(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "⬅️ Назад":
        if data.get("interval_target") == "day":
            return await show_day_intervals(msg, state, int(data["current_weekday"]))
        return await show_common_intervals(msg, state)

    start = (data.get("start_map") or {}).get(msg.text)
    if start is None:
        return await msg.answer("Выберите время кнопкой ниже.")

    last_starts = _interval_end_choices(int(start))
    await state.set_state(WaitFlow.interval_end)
    await state.update_data(
        current_start=int(start),
        # Internally intervals stay half-open: choosing the last allowed start
        # 22:00 is stored as end_hour=23.
        end_map={f"{h:02d}:00": h + 1 for h in last_starts},
    )
    labels = [f"{h:02d}:00" for h in last_starts]
    rows = [labels[i:i + 4] for i in range(0, len(labels), 4)] + [["⬅️ Назад"]]
    await msg.answer(
        "Выберите последнее подходящее время начала стирки:",
        reply_markup=reply_menu(rows),
    )


@router.message(WaitFlow.interval_end)
async def interval_end(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "⬅️ Назад":
        await state.set_state(WaitFlow.interval_start)
        return await msg.answer("Выберите начало интервала ещё раз.")

    end = (data.get("end_map") or {}).get(msg.text)
    if end is None:
        return await msg.answer("Выберите время кнопкой ниже.")

    new_interval = (int(data["current_start"]), int(end))
    if data.get("interval_target") == "day":
        weekday = int(data["current_weekday"])
        schedule = _state_schedule(data)
        merged = _merge_intervals(schedule.get(weekday, []) + [new_interval])
        if len(merged) > 3:
            return await msg.answer("Можно добавить максимум 3 интервала на один день.")
        schedule[weekday] = merged
        await state.update_data(schedule=schedule)
        return await show_day_intervals(msg, state, weekday)

    intervals = [(int(a), int(b)) for a, b in data.get("intervals", [])]
    merged = _merge_intervals(intervals + [new_interval])
    if len(merged) > 3:
        return await msg.answer("Можно добавить максимум 3 интервала.")
    await state.update_data(intervals=merged)
    return await show_common_intervals(msg, state)


async def show_waitlist_weekdays(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    selected = set(int(x) for x in data.get("selected_weekdays", []))
    any_day = bool(data.get("any_day", True))
    await state.set_state(WaitFlow.weekdays)
    await state.update_data(selected_weekdays=list(selected), any_day=any_day)

    rows = [["✅ Любой день" if any_day else "⬜ Любой день"]]
    first = []
    second = []
    for idx, label in enumerate(WEEKDAY_SHORT):
        text = f"{'✅' if idx in selected else '⬜'} {label}"
        (first if idx < 4 else second).append(text)
    rows += [first, second, ["✅ Продолжить"], ["⬅️ Назад", "🏠 Главное меню"]]
    await msg.answer(
        "📆 Какие дни недели вам подходят?\n\n"
        "Выбранные интервалы будут одинаковыми для всех этих дней.",
        reply_markup=reply_menu(rows),
    )


@router.message(WaitFlow.weekdays)
async def waitlist_weekdays(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        return await show_common_intervals(msg, state)

    data = await state.get_data()
    selected = set(int(x) for x in data.get("selected_weekdays", []))
    any_day = bool(data.get("any_day", True))

    if msg.text == "✅ Продолжить":
        if not any_day and not selected:
            return await msg.answer("Выберите хотя бы один день недели или «Любой день».")
        intervals = [(int(a), int(b)) for a, b in data.get("intervals", [])]
        days = list(range(7)) if any_day else sorted(selected)
        schedule = {day: list(intervals) for day in days}
        await state.update_data(schedule=schedule)
        return await show_waitlist_machines(msg, state)

    if msg.text in {"✅ Любой день", "⬜ Любой день"}:
        any_day = True
        selected.clear()
    else:
        raw = (msg.text or "").replace("✅ ", "").replace("⬜ ", "")
        if raw not in WEEKDAY_SHORT:
            return await msg.answer("Выберите день кнопкой ниже.")
        idx = WEEKDAY_SHORT.index(raw)
        any_day = False
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)

    await state.update_data(selected_weekdays=list(selected), any_day=any_day)
    return await show_waitlist_weekdays(msg, state)


async def show_schedule_days(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    schedule = _state_schedule(data)
    await state.set_state(WaitFlow.schedule_days)

    summary = _format_schedule(schedule) if schedule else "Пока ни один день не настроен."
    first = [f"{'✅' if i in schedule else '⬜'} {WEEKDAY_SHORT[i]}" for i in range(4)]
    second = [f"{'✅' if i in schedule else '⬜'} {WEEKDAY_SHORT[i]}" for i in range(4, 7)]
    rows = [
        first,
        second,
        ["✅ Продолжить"],
        ["🕐 Одинаковое время по дням"],
        ["⬅️ Назад", "🏠 Главное меню"],
    ]
    await msg.answer(
        "📆 <b>Расписание по дням</b>\n\n"
        f"{summary}\n\n"
        "Нажмите на день, чтобы задать для него свои интервалы.",
        parse_mode="HTML",
        reply_markup=reply_menu(rows),
    )


@router.message(WaitFlow.schedule_days)
async def waitlist_schedule_days(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        return await show_schedule_mode(msg, state)
    if msg.text == "🕐 Одинаковое время по дням":
        await state.update_data(schedule_mode="common", intervals=[], selected_weekdays=[], any_day=True)
        return await show_common_intervals(msg, state)
    if msg.text == "✅ Продолжить":
        if not _state_schedule(await state.get_data()):
            return await msg.answer("Настройте хотя бы один день.")
        return await show_waitlist_machines(msg, state)

    raw = (msg.text or "").replace("✅ ", "").replace("⬜ ", "")
    if raw not in WEEKDAY_SHORT:
        return await msg.answer("Выберите день кнопкой ниже.")
    weekday = WEEKDAY_SHORT.index(raw)
    await state.update_data(current_weekday=weekday)
    return await show_day_intervals(msg, state, weekday)


async def show_day_intervals(msg: types.Message, state: FSMContext, weekday: int):
    data = await state.get_data()
    schedule = _state_schedule(data)
    intervals = schedule.get(int(weekday), [])
    await state.set_state(WaitFlow.day_intervals)
    await state.update_data(current_weekday=int(weekday))

    time_text = _format_intervals(intervals) if intervals else "Время ещё не задано."
    rows = []
    if len(intervals) < 3:
        rows.append(["➕ Добавить интервал"])
    if intervals:
        rows.append(["📋 Скопировать на другие дни"])
        rows.append(["🗑 Убрать день"])
    rows += [["✅ Готово"], ["🏠 Главное меню"]]

    await msg.answer(
        f"📆 <b>{WEEKDAYS[int(weekday)]}</b>\n\n"
        f"🕐 {time_text}",
        parse_mode="HTML",
        reply_markup=reply_menu(rows),
    )


@router.message(WaitFlow.day_intervals)
async def waitlist_day_intervals(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)

    data = await state.get_data()
    weekday = int(data["current_weekday"])

    if msg.text == "➕ Добавить интервал":
        return await start_interval_picker(msg, state, target="day")
    if msg.text == "📋 Скопировать на другие дни":
        schedule = _state_schedule(data)
        if not schedule.get(weekday):
            return await msg.answer("Сначала добавьте хотя бы один интервал.")
        await state.update_data(copy_days=[])
        return await show_copy_days(msg, state)
    if msg.text == "🗑 Убрать день":
        schedule = _state_schedule(data)
        schedule.pop(weekday, None)
        await state.update_data(schedule=schedule)
        return await show_schedule_days(msg, state)
    if msg.text == "✅ Готово":
        return await show_schedule_days(msg, state)

    await msg.answer("Выберите действие кнопкой ниже.")


async def show_copy_days(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    source = int(data["current_weekday"])
    selected = set(int(x) for x in data.get("copy_days", []))
    await state.set_state(WaitFlow.copy_days)

    labels = []
    for day in range(7):
        if day == source:
            continue
        labels.append(f"{'✅' if day in selected else '⬜'} {WEEKDAY_SHORT[day]}")
    rows = [labels[i:i + 3] for i in range(0, len(labels), 3)]
    rows += [["✅ Применить"], ["⬅️ Назад", "🏠 Главное меню"]]
    await msg.answer(
        f"📋 Скопировать время из <b>{WEEKDAYS[source]}</b>\n\n"
        "Выберите дни. Если в них уже есть интервалы, они будут заменены.",
        parse_mode="HTML",
        reply_markup=reply_menu(rows),
    )


@router.message(WaitFlow.copy_days)
async def waitlist_copy_days(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)

    data = await state.get_data()
    source = int(data["current_weekday"])
    if msg.text == "⬅️ Назад":
        return await show_day_intervals(msg, state, source)

    selected = set(int(x) for x in data.get("copy_days", []))
    if msg.text == "✅ Применить":
        if not selected:
            return await msg.answer("Выберите хотя бы один день.")
        schedule = _state_schedule(data)
        source_intervals = list(schedule.get(source, []))
        for day in selected:
            schedule[int(day)] = list(source_intervals)
        await state.update_data(schedule=schedule, copy_days=[])
        return await show_schedule_days(msg, state)

    raw = (msg.text or "").replace("✅ ", "").replace("⬜ ", "")
    if raw not in WEEKDAY_SHORT:
        return await msg.answer("Выберите день кнопкой ниже.")
    day = WEEKDAY_SHORT.index(raw)
    if day == source:
        return await msg.answer("Это исходный день.")
    if day in selected:
        selected.remove(day)
    else:
        selected.add(day)
    await state.update_data(copy_days=list(selected))
    return await show_copy_days(msg, state)


async def show_waitlist_machines(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,name FROM machines WHERE type='wash' AND is_active ORDER BY name"
        ).fetchall()

    machine_map = {str(name): int(mid) for mid, name in rows}
    active_ids = set(machine_map.values())
    selected = {
        int(x) for x in data.get("selected_machines", [])
        if int(x) in active_ids
    }
    any_machine = bool(data.get("any_machine", not selected))
    if not any_machine and not selected:
        any_machine = True

    await state.set_state(WaitFlow.machines)
    await state.update_data(
        machine_map=machine_map,
        selected_machines=list(selected),
        any_machine=any_machine,
    )

    kb_rows = [["✅ Любая" if any_machine else "⬜ Любая"]]
    for name, mid in machine_map.items():
        kb_rows.append([f"{'✅' if mid in selected else '⬜'} {name}"])
    kb_rows += [["✅ Продолжить"], ["⬅️ Назад", "🏠 Главное меню"]]
    await msg.answer(
        "🧺 Какие стиральные машинки вам подходят?\n\n"
        "Можно выбрать одну, несколько или любую.",
        reply_markup=reply_menu(kb_rows),
    )


@router.message(WaitFlow.machines)
async def waitlist_machines(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)

    data = await state.get_data()
    if msg.text == "⬅️ Назад":
        if data.get("schedule_mode") == "common":
            return await show_waitlist_weekdays(msg, state)
        return await show_schedule_days(msg, state)

    if msg.text == "✅ Продолжить":
        if not data.get("any_machine") and not data.get("selected_machines"):
            return await msg.answer("Выберите хотя бы одну машинку.")
        await state.set_state(WaitFlow.mode)
        return await msg.answer(
            "⚡ Что сделать, если найдётся место?\n\n"
            "⚡ Автоматически: бот сам запишет вас на подходящий слот и сообщит об этом. "
            "Если до начала осталось 5–30 минут, бот сначала попросит подтверждение и удержит слот на 2 минуты. Менее чем за 5 минут новые предложения не отправляются.\n\n"
            "🔔 Сначала спросить: бот предложит конкретный слот и удержит его на 5 минут. Если до стирки осталось 5–30 минут, удержание составит 2 минуты.",
            reply_markup=reply_menu([
                ["⚡ Записать автоматически"],
                ["🔔 Сначала спросить"],
                ["⬅️ Назад", "🏠 Главное меню"],
            ]),
        )

    any_machine = bool(data.get("any_machine"))
    selected = set(int(x) for x in data.get("selected_machines", []))
    machine_map = data.get("machine_map", {})

    if msg.text in {"✅ Любая", "⬜ Любая"}:
        any_machine = True
        selected.clear()
    else:
        raw = (msg.text or "").replace("✅ ", "").replace("⬜ ", "")
        mid = machine_map.get(raw)
        if mid is None:
            return await msg.answer("Выберите машинку кнопкой ниже.")
        any_machine = False
        if int(mid) in selected:
            selected.remove(int(mid))
        else:
            selected.add(int(mid))

    await state.update_data(any_machine=any_machine, selected_machines=list(selected))
    return await show_waitlist_machines(msg, state)


@router.message(WaitFlow.mode)
async def waitlist_mode(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        return await show_waitlist_machines(msg, state)

    if msg.text == "⚡ Записать автоматически":
        mode = "auto"
    elif msg.text == "🔔 Сначала спросить":
        mode = "notify"
    else:
        return await msg.answer("Выберите режим кнопкой ниже.")

    data = await state.get_data()
    schedule = _state_schedule(data)
    if not schedule:
        return await msg.answer("Сначала настройте расписание.")

    await state.update_data(mode=mode)
    if data.get("any_machine"):
        machines = "Любая стиральная машина"
    else:
        reverse = {v: k for k, v in (data.get("machine_map") or {}).items()}
        machines = ", ".join(
            reverse.get(int(x), str(x))
            for x in data.get("selected_machines", [])
        )

    mode_text = "Автозапись" if mode == "auto" else "Сначала спросить"
    is_edit = bool(data.get("is_edit"))
    action = "✅ Сохранить заявку" if is_edit else "✅ Встать в очередь"

    await state.set_state(WaitFlow.confirm)
    await msg.answer(
        ("🔔 <b>Изменение заявки</b>\n\n" if is_edit else "🔔 <b>Новая заявка</b>\n\n")
        + f"{_format_schedule(schedule)}\n\n"
        + f"🧺 Машинки: {machines}\n"
        + f"⚡ Режим: {mode_text}\n\n"
        + "Подписка остаётся включённой, пока вы сами её не отмените. После каждой состоявшейся стирки она снова начинает участвовать в очереди.",
        parse_mode="HTML",
        reply_markup=reply_menu([[action], ["⬅️ Назад", "🏠 Главное меню"]]),
    )


@router.message(WaitFlow.confirm)
async def waitlist_confirm(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        await state.set_state(WaitFlow.mode)
        return await msg.answer("Выберите режим.")

    data = await state.get_data()
    expected = "✅ Сохранить заявку" if data.get("is_edit") else "✅ Встать в очередь"
    if msg.text != expected:
        return await msg.answer("Подтвердите заявку кнопкой ниже.")

    schedule = _state_schedule(data)
    try:
        request_id = save_request(
            msg.from_user.id,
            [],
            data.get("selected_machines", []),
            bool(data.get("any_machine")),
            data.get("mode"),
            schedule=schedule,
        )
    except ValueError as exc:
        await state.clear()
        return await msg.answer(str(exc), reply_markup=main_kb(msg.from_user.id))

    is_edit = bool(data.get("is_edit"))
    state_text = _subscription_state_text(int(request_id))
    await state.clear()
    await msg.answer(
        (
            "✅ Подписка обновлена.\n\n"
            if is_edit
            else "✅ Подписка сохранена.\n\n"
        )
        + state_text
        + (
            "\n\nЕсли у вас уже есть запись на стирку, подписка не копит приоритет параллельно с ней."
            "\nПосле состоявшейся стирки подписка включается снова автоматически."
        ),
        reply_markup=main_kb(msg.from_user.id),
    )
    await check_active_waitlist()


@router.message(F.text == "❌ Отменить заявку")
async def waitlist_cancel(msg: types.Message, state: FSMContext):
    await state.clear()
    ok = cancel_request_for_tg(msg.from_user.id)
    await msg.answer(
        "✅ Подписка отключена." if ok else "Активной подписки уже нет.",
        reply_markup=main_kb(msg.from_user.id),
    )


@router.message(F.text == "⚙️ Настройки уведомлений")
async def notification_settings(msg: types.Message, state: FSMContext):
    user = get_user(msg.from_user.id)
    if not user:
        return
    cfg = get_notification_settings(int(user[0]))
    await state.clear()
    rows = [
        [f"🌙 Тихий режим: {'✅' if cfg['quiet_enabled'] else '❌'}"],
        [f"🕐 Тихие часы: {cfg['quiet_start']:02d}:00-{cfg['quiet_end']:02d}:00"],
        [f"🔄 Более ранняя запись: {'✅' if cfg['earlier_offer_enabled'] else '❌'}"],
        [f"⏰ Напоминание за 30 минут: {'✅' if cfg['reminder_30_enabled'] else '❌'}"],
        ["⬅️ Назад", "🏠 Главное меню"],
    ]
    await msg.answer(
        "⚙️ <b>Настройки уведомлений</b>\n\n"
        f"🌙 Тихий режим\nНочные сообщения продолжают приходить, но без звука. Сейчас: {cfg['quiet_start']:02d}:00-{cfg['quiet_end']:02d}:00.\n\n"
        "🔄 Более ранняя запись\nБот может предложить подходящий слот на более ранний день.\n\n"
        "⏰ Напоминание за 30 минут\nОбычное напоминание перед вашей записью.",
        parse_mode="HTML",
        reply_markup=reply_menu(rows),
    )


@router.message(F.text.startswith("🌙 Тихий режим:"))
@router.message(F.text.startswith("🔄 Более ранняя запись:"))
@router.message(F.text.startswith("⏰ Напоминание за 30 минут:"))
async def toggle_notification_setting(msg: types.Message, state: FSMContext):
    user = get_user(msg.from_user.id)
    if not user:
        return
    uid = int(user[0])
    cfg = get_notification_settings(uid)
    if msg.text.startswith("🌙"):
        set_notification_setting(uid, "quiet_enabled", int(not cfg["quiet_enabled"]))
    elif msg.text.startswith("🔄"):
        set_notification_setting(uid, "earlier_offer_enabled", int(not cfg["earlier_offer_enabled"]))
    else:
        set_notification_setting(uid, "reminder_30_enabled", int(not cfg["reminder_30_enabled"]))
    await notification_settings(msg, state)


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def help_home(msg: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(HelpFlow.menu)
    await msg.answer(
        "ℹ️ <b>Помощь</b>\n\nВыберите раздел 👇",
        parse_mode="HTML",
        reply_markup=reply_menu([
            ["📖 Как пользоваться ботом"],
            ["🤯 Что нового в PRA4KA 2.0"],
            ["📘 Про лист ожидания"],
            ["⚖️ Как работает очередь"],
            ["⏰ Напоминания и таймер"],
            ["⚙️ Настройки уведомлений"],
            ["🏠 Главное меню"],
        ]),
    )


HELP_TEXTS = {
    "📖 Как пользоваться ботом":
        "📖 <b>Как пользоваться ботом</b>\n\n"
        "🧺 Записаться\nВыберите дату, машинку и свободное время. После выбора даты бот сразу покажет свободные часы всех работающих машин.\n\n"
        "📋 Мои записи\nПоказывает ваши предстоящие записи.\n\n"
        "❌ Отменить запись\nВыберите запись, которую хотите отменить.\n\n"
        "🔔 Лист ожидания\nМожно заранее указать удобное время и машинки, а бот сам будет искать подходящее свободное место.\n\n"
        "🌬️ Сушка\nСначала нужно записаться на стиральную машину. После записи бот предложит свободную сушилку на следующий час, если она есть. После этого сушилку также можно выбрать вручную.\n\n"
        "📅 Новая дата для обычной записи открывается каждый день в 00:00.\n\n"
        "Если заметили ошибку или что-то работает странно, напишите <b>@ilyinmax</b>.",

    "🤯 Что нового в PRA4KA 2.0":
        "🤯 <b>Что нового в PRA4KA 2.0</b>\n\n"
        "🔔 Лист ожидания\n⚡ Автозапись или предложение слота\n📆 Отдельное расписание по дням недели\n🕐 До 3 интервалов на каждый день\n🧺 Несколько машинок или любая\n🔄 Предложения более раннего дня\n🌙 Настройки уведомлений\n⏰ Таймер стирки и напоминание забрать вещи\n⚠️ Уведомление предыдущему пользователю, если вещи остались в машинке\n\n"
        "С 23:00 до 00:00 бот распределяет часть мест новой даты между заранее созданными заявками. В 00:00 оставшиеся места открываются для обычной записи.",

    "📘 Про лист ожидания":
        "🔔 <b>Лист ожидания</b>\n\n"
        "Лист ожидания позволяет заранее указать удобное для вас время, а поиск свободной записи бот возьмёт на себя.\n\n"
        "Расписание можно настроить двумя способами: задать одинаковое время для нескольких дней или указать свои интервалы отдельно для каждого дня недели. На каждый день можно выбрать до 3 интервалов.\n\n"
        "Например: Пн 10:00-22:00, Вт 18:00-22:00, а Чт 08:00-12:00 и 18:00-22:00. Настроенное время можно быстро скопировать на другие дни.\n\n"
        "⚡ Автозапись: бот сам занимает подходящее место, если до начала стирки осталось не меньше 30 минут. Если до стирки осталось 5–30 минут, бот сначала пришлёт срочное предложение и удержит его 2 минуты. При запасе 30 минут и больше предложение удерживается 5 минут. Менее чем за 5 минут новые HOLD не выдаются.\n\n"
        "🔔 Сначала спросить: бот предлагает конкретный слот и удерживает его 5 минут, а при запасе 5–30 минут — 2 минуты. В сообщении указано точное время окончания удержания.\n\n"
        "Конкретную дату выбирать не нужно. Подписка остаётся сохранённой и после полученной стирки: после её окончания она автоматически снова начнёт участвовать в очереди с новым отсчётом приоритета.",

    "⚖️ Как работает очередь":
        "⚖️ <b>Как работает очередь</b>\n\n"
        "Если на одно место претендуют несколько человек, главный критерий - как давно человек ждёт с учётом его стирок за последние 30 дней.\n\n"
        "Если приоритет одинаковый, бот дополнительно учитывает количество подходящих вариантов и старается не отбирать редкий слот у человека с более узким расписанием.\n\n"
        "Чем старше предыдущая стирка, тем меньше она влияет. Через 30 дней она перестаёт учитываться.",

    "⏰ Напоминания и таймер":
        "⏰ <b>Напоминания и таймер</b>\n\n"
        "Эта функция нужна, чтобы машинки освобождались вовремя и в общий чат как можно реже приходилось писать «заберите вещи».\n\n"
        "За 30 минут до записи бот может напомнить о стирке.\n\n"
        "После запуска машинки нажмите <b>⏱ Поставить таймер</b> и укажите количество минут с дисплея. Если вы уже пользовались таймером, бот предложит последние значения для быстрого выбора.\n\n"
        "За 2 минуты до окончания бот напомнит, что пора спускаться за вещами. Пожалуйста, ставьте таймер после запуска машинки. Это помогает не задерживать следующего человека.\n\n"
        "Если ваша запись уже началась, а в машинке остались вещи предыдущего пользователя, появится кнопка <b>⚠️ В машине чужие вещи</b>. Бот сам отправит предыдущему человеку уведомление. Вам не нужно искать владельца вещей или писать в общий чат.\n\n"
        "Если предыдущий пользователь поставил таймер и его программа ещё идёт, бот учитывает это и не предлагает уведомлять его раньше времени.",

    "ℹ️ Как работает лист ожидания":
        "🔔 <b>Как работает лист ожидания</b>\n\n"
        "До 23:00 можно создать заявку. С 23:00 до 00:00 бот обрабатывает заявки на новую дату, которая ещё не видна в обычной записи. В 00:00 оставшиеся места становятся доступны всем.\n\n"
        "Если место освобождается днём, бот тоже проверяет лист ожидания. Обычно предложение удерживается 5 минут. Если до стирки осталось от 5 до 30 минут, даже в режиме автозаписи бот сначала просит подтверждение и удерживает место 2 минуты. Менее чем за 5 минут новые предложения не отправляются.\n\n"
        "Если вы пропустили предложенный слот или не ответили вовремя, это не считается отказом от стирки: заявка остаётся активной и накопленный приоритет сохраняется.\n\n"
        "Если AUTO уже записал вас, но вы отменили будущую запись до её начала, подписка снова становится активной. Если она уже копила приоритет до этой записи, накопленный приоритет сохраняется.\n\n"
        "Если вы создаёте подписку при уже существующей записи на стирку, она сохранится сразу, но приоритет начнёт считаться только после окончания этой стирки. После каждой состоявшейся стирки подписка автоматически включается снова с новым отсчётом приоритета.",
}


@router.message(HelpFlow.menu)
async def help_section(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⚙️ Настройки уведомлений":
        return await notification_settings(msg, state)
    text = HELP_TEXTS.get(msg.text)
    if not text:
        return await msg.answer("Выберите раздел кнопкой ниже.")
    await msg.answer(text, parse_mode="HTML")


@router.callback_query(F.data.startswith("wl_accept_"))
async def waitlist_accept_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        hold_id = int(callback.data.removeprefix("wl_accept_"))
    except Exception:
        return await callback.answer("Некорректное предложение.", show_alert=True)

    # Telegram callback queries expire quickly. Acknowledge the click before
    # any Neon work so the user never waits tens of seconds for button feedback.
    try:
        await callback.answer("Обрабатываю запись…")
    except Exception:
        pass

    result = await accept_hold(hold_id, callback.from_user.id)
    if not result:
        return await callback.message.answer(
            "⚠️ Этот слот уже недоступен. Если вы нажимали кнопку повторно, проверьте «Мои записи»."
        )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.clear()
    await callback.message.answer(
        "✅ <b>Запись подтверждена</b>\n\n"
        f"📅 {date_text(result.date)}\n"
        f"🕐 {result.hour:02d}:00\n"
        f"🧺 {result.machine_name}",
        parse_mode="HTML",
        reply_markup=main_kb(callback.from_user.id),
    )
    if result.machine_type == "wash":
        offer = find_next_dryer(result.user_id, result.date, result.hour)
        if offer:
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
            await callback.message.answer(
                "🌬️ <b>Нужна сушка после стирки?</b>\n\n"
                f"Свободна <b>{offer.machine_name}</b>\n"
                f"сразу после вашей стирки, в {offer.hour:02d}:00.",
                parse_mode="HTML",
                reply_markup=kb,
            )


@router.callback_query(F.data.startswith("wl_decline_"))
async def waitlist_decline_callback(callback: types.CallbackQuery):
    try:
        hold_id = int(callback.data.removeprefix("wl_decline_"))
    except Exception:
        return await callback.answer("Некорректное предложение.", show_alert=True)
    hold = get_hold(hold_id)
    context = str(hold[9]) if hold else ""
    ok = await decline_hold(hold_id, callback.from_user.id)
    if not ok:
        return await callback.answer("Предложение уже неактуально.", show_alert=True)
    await callback.answer("Хорошо")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if context == "move":
        await callback.message.answer("Текущая запись сохранена без изменений.")
    else:
        await callback.message.answer("Слот пропущен. Ваша заявка остаётся активной.")


@router.message(F.text.startswith("🕐 Тихие часы:"))
async def quiet_hours_start(msg: types.Message, state: FSMContext):
    labels = [f"{h:02d}:00" for h in range(24)]
    await state.set_state(NotificationFlow.quiet_start)
    await state.update_data(quiet_start_map={label: h for h, label in enumerate(labels)})
    rows = [labels[i:i + 4] for i in range(0, len(labels), 4)] + [["⬅️ Назад"]]
    await msg.answer("🌙 С какого времени включать тихий режим?", reply_markup=reply_menu(rows))


@router.message(NotificationFlow.quiet_start)
async def quiet_start_chosen(msg: types.Message, state: FSMContext):
    if msg.text == "⬅️ Назад":
        return await notification_settings(msg, state)
    data = await state.get_data()
    start = (data.get("quiet_start_map") or {}).get(msg.text)
    if start is None:
        return await msg.answer("Выберите время кнопкой ниже.")
    labels = [f"{h:02d}:00" for h in range(24) if h != int(start)]
    await state.set_state(NotificationFlow.quiet_end)
    await state.update_data(
        quiet_start=int(start),
        quiet_end_map={label: int(label[:2]) for label in labels},
    )
    rows = [labels[i:i + 4] for i in range(0, len(labels), 4)] + [["⬅️ Назад"]]
    await msg.answer("🌙 До какого времени оставить уведомления тихими?", reply_markup=reply_menu(rows))


@router.message(NotificationFlow.quiet_end)
async def quiet_end_chosen(msg: types.Message, state: FSMContext):
    if msg.text == "⬅️ Назад":
        return await quiet_hours_start(msg, state)
    data = await state.get_data()
    end = (data.get("quiet_end_map") or {}).get(msg.text)
    if end is None:
        return await msg.answer("Выберите время кнопкой ниже.")
    user = get_user(msg.from_user.id)
    if not user:
        return await show_home(msg, state)
    set_notification_setting(int(user[0]), "quiet_start", int(data["quiet_start"]))
    set_notification_setting(int(user[0]), "quiet_end", int(end))
    await msg.answer("✅ Тихие часы обновлены.")
    await notification_settings(msg, state)


@router.message(F.text == "⬅️ Назад")
async def generic_back(msg: types.Message, state: FSMContext):
    await show_home(msg, state)


@router.message(F.text == "ℹ️ Как работает лист ожидания")
async def waitlist_info(msg: types.Message):
    await msg.answer(HELP_TEXTS["ℹ️ Как работает лист ожидания"], parse_mode="HTML")
