from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import TIMEZONE, WORKING_HOURS
from database import (
    get_conn,
    get_user,
    is_banned,
    get_free_hours_effective,
    get_notification_settings,
    set_notification_setting,
)
from keyboards import build_main_menu, reply_menu
from booking_service import create_booking_safe, DailyLimit, SlotBusy, InvalidBooking
from waitlist_service import (
    get_active_request_for_tg,
    cancel_request_for_tg,
    save_request,
    accept_hold,
    decline_hold,
    get_hold,
)

TZ = ZoneInfo(TIMEZONE)
router = Router()

MONTHS = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


class BookFlow(StatesGroup):
    date = State()
    machine = State()
    hour = State()


class CancelFlow(StatesGroup):
    choose = State()
    confirm = State()


class WaitFlow(StatesGroup):
    menu = State()
    interval_start = State()
    interval_end = State()
    intervals = State()
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


def booking_dates() -> list[tuple[str, str]]:
    now = datetime.now(TZ)
    today = now.date()
    start = 1 if now.hour >= 23 else 0
    count = 2 if now.hour >= 23 else 3
    out = []
    for offset in range(start, start + count):
        d = today + timedelta(days=offset)
        if offset == 0:
            prefix = "Сегодня"
        elif offset == 1:
            prefix = "Завтра"
        elif offset == 2:
            prefix = "Послезавтра"
        else:
            prefix = date_text(d.isoformat())
        out.append((f"📅 {prefix}, {d.day} {MONTHS[d.month]}", d.isoformat()))
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
    with get_conn() as conn:
        machines = conn.execute(
            "SELECT id,type,name FROM machines WHERE is_active ORDER BY type,name"
        ).fetchall()

    lines = [f"📅 <b>{date_text(date_iso)}</b>", ""]
    machine_map = {}
    for mid, mtype, name in machines:
        hours = get_free_hours_effective(int(mid), date_iso)
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
    await msg.answer("✅ Запись отменена.", reply_markup=main_kb(msg.from_user.id))
    from waitlist_service import distribute_date
    await distribute_date(old.date, context="day")


def _waitlist_summary(tg_id: int) -> str:
    req = get_active_request_for_tg(tg_id)
    if not req:
        return ""
    rid, mode, any_machine, created_at, priority_since = req
    with get_conn() as conn:
        intervals = conn.execute(
            "SELECT start_hour,end_hour FROM waitlist_intervals WHERE request_id=? ORDER BY start_hour",
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
    times = ", ".join(f"{int(a):02d}:00-{int(b):02d}:00" for a, b in intervals)
    machine_text = "Любая стиральная машина" if any_machine else ", ".join(str(x[0]) for x in machines)
    mode_text = "Автозапись" if mode == "auto" else "Сначала спросить"
    return (
        "🔔 <b>Активная заявка</b>\n\n"
        f"🕐 {times}\n"
        f"🧺 {machine_text}\n"
        f"⚡ Режим: {mode_text}"
    )


@router.message(F.text.startswith("🔔 Лист ожидания"))
async def waitlist_home(msg: types.Message, state: FSMContext):
    await state.clear()
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
    await state.update_data(intervals=[])
    await state.set_state(WaitFlow.intervals)
    await msg.answer(
        "🕐 Добавьте до 3 удобных интервалов времени.",
        reply_markup=reply_menu([["➕ Добавить интервал"], ["✅ Продолжить"], ["🏠 Главное меню"]]),
    )


@router.message(WaitFlow.intervals)
async def interval_menu(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "➕ Добавить интервал":
        data = await state.get_data()
        if len(data.get("intervals", [])) >= 3:
            return await msg.answer("Можно добавить максимум 3 интервала.")
        starts = [f"{h:02d}:00" for h in WORKING_HOURS]
        await state.set_state(WaitFlow.interval_start)
        await state.update_data(start_map={f"{h:02d}:00": h for h in WORKING_HOURS})
        rows = [starts[i:i + 4] for i in range(0, len(starts), 4)] + [["⬅️ Назад"]]
        return await msg.answer("Выберите начало интервала:", reply_markup=reply_menu(rows))
    if msg.text == "✅ Продолжить":
        data = await state.get_data()
        intervals = data.get("intervals", [])
        if not intervals:
            return await msg.answer("Добавьте хотя бы один интервал.")
        return await show_waitlist_machines(msg, state)
    await msg.answer("Выберите действие кнопкой ниже.")


@router.message(WaitFlow.interval_start)
async def interval_start(msg: types.Message, state: FSMContext):
    if msg.text == "⬅️ Назад":
        await state.set_state(WaitFlow.intervals)
        return await msg.answer(
            "🕐 Интервалы",
            reply_markup=reply_menu([["➕ Добавить интервал"], ["✅ Продолжить"], ["🏠 Главное меню"]]),
        )
    data = await state.get_data()
    start = (data.get("start_map") or {}).get(msg.text)
    if start is None:
        return await msg.answer("Выберите время кнопкой ниже.")
    ends = list(range(int(start) + 1, max(WORKING_HOURS) + 2))
    await state.set_state(WaitFlow.interval_end)
    await state.update_data(current_start=int(start), end_map={f"{h:02d}:00": h for h in ends})
    labels = [f"{h:02d}:00" for h in ends]
    rows = [labels[i:i + 4] for i in range(0, len(labels), 4)] + [["⬅️ Назад"]]
    await msg.answer("Выберите конец интервала:", reply_markup=reply_menu(rows))


@router.message(WaitFlow.interval_end)
async def interval_end(msg: types.Message, state: FSMContext):
    if msg.text == "⬅️ Назад":
        await state.set_state(WaitFlow.interval_start)
        return await msg.answer("Выберите начало интервала ещё раз.")
    data = await state.get_data()
    end = (data.get("end_map") or {}).get(msg.text)
    if end is None:
        return await msg.answer("Выберите время кнопкой ниже.")
    intervals = list(data.get("intervals", []))
    intervals.append((int(data["current_start"]), int(end)))
    intervals = sorted(intervals)
    merged = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    await state.update_data(intervals=merged)
    await state.set_state(WaitFlow.intervals)
    text = "\n".join(f"{i + 1}. {a:02d}:00-{b:02d}:00" for i, (a, b) in enumerate(merged))
    await msg.answer(
        "🕐 Выбранные интервалы:\n" + text,
        reply_markup=reply_menu([["➕ Добавить интервал"], ["✅ Продолжить"], ["🏠 Главное меню"]]),
    )


async def show_waitlist_machines(msg: types.Message, state: FSMContext):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,name FROM machines WHERE type='wash' AND is_active ORDER BY name"
        ).fetchall()
    machine_map = {str(name): int(mid) for mid, name in rows}
    await state.set_state(WaitFlow.machines)
    await state.update_data(machine_map=machine_map, selected_machines=[], any_machine=True)
    kb_rows = [["✅ Любая"]]
    kb_rows += [[f"⬜ {name}"] for name in machine_map]
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
    if msg.text == "⬅️ Назад":
        await state.set_state(WaitFlow.intervals)
        return await msg.answer(
            "🕐 Интервалы",
            reply_markup=reply_menu([["➕ Добавить интервал"], ["✅ Продолжить"], ["🏠 Главное меню"]]),
        )
    data = await state.get_data()
    if msg.text == "✅ Продолжить":
        if not data.get("any_machine") and not data.get("selected_machines"):
            return await msg.answer("Выберите хотя бы одну машинку.")
        await state.set_state(WaitFlow.mode)
        return await msg.answer(
            "⚡ Что сделать, если найдётся место?\n\n"
            "⚡ Автоматически: бот сам запишет вас на подходящий слот и сообщит об этом.\n\n"
            "🔔 Сначала спросить: бот предложит конкретный слот и удержит его за вами 2 минуты.",
            reply_markup=reply_menu([
                ["⚡ Записать автоматически"],
                ["🔔 Сначала спросить"],
                ["⬅️ Назад", "🏠 Главное меню"],
            ]),
        )

    any_machine = bool(data.get("any_machine"))
    selected = set(data.get("selected_machines", []))
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
        if mid in selected:
            selected.remove(mid)
        else:
            selected.add(mid)

    await state.update_data(any_machine=any_machine, selected_machines=list(selected))
    rows = [["✅ Любая" if any_machine else "⬜ Любая"]]
    for name, mid in machine_map.items():
        rows.append([f"{'✅' if mid in selected else '⬜'} {name}"])
    rows += [["✅ Продолжить"], ["⬅️ Назад", "🏠 Главное меню"]]
    await msg.answer("🧺 Выбор машинок обновлён:", reply_markup=reply_menu(rows))


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
    await state.update_data(mode=mode)
    intervals = data.get("intervals", [])
    times = "\n".join(f"• {a:02d}:00-{b:02d}:00" for a, b in intervals)
    if data.get("any_machine"):
        machines = "Любая стиральная машина"
    else:
        reverse = {v: k for k, v in (data.get("machine_map") or {}).items()}
        machines = ", ".join(reverse.get(x, str(x)) for x in data.get("selected_machines", []))
    mode_text = "Автозапись" if mode == "auto" else "Сначала спросить"
    await state.set_state(WaitFlow.confirm)
    await msg.answer(
        "🔔 <b>Новая заявка</b>\n\n"
        f"🕐 Время:\n{times}\n\n"
        f"🧺 Машинки: {machines}\n"
        f"⚡ Режим: {mode_text}\n\n"
        "Заявка действует, пока вам не найдётся подходящее место или вы её не отмените.",
        parse_mode="HTML",
        reply_markup=reply_menu([["✅ Встать в очередь"], ["⬅️ Назад", "🏠 Главное меню"]]),
    )


@router.message(WaitFlow.confirm)
async def waitlist_confirm(msg: types.Message, state: FSMContext):
    if msg.text == "🏠 Главное меню":
        return await show_home(msg, state)
    if msg.text == "⬅️ Назад":
        await state.set_state(WaitFlow.mode)
        return await msg.answer("Выберите режим.")
    if msg.text != "✅ Встать в очередь":
        return await msg.answer("Подтвердите заявку кнопкой ниже.")
    data = await state.get_data()
    try:
        save_request(
            msg.from_user.id,
            data.get("intervals", []),
            data.get("selected_machines", []),
            bool(data.get("any_machine")),
            data.get("mode"),
        )
    except ValueError as exc:
        await state.clear()
        return await msg.answer(str(exc), reply_markup=main_kb(msg.from_user.id))
    await state.clear()
    await msg.answer(
        "✅ Вы добавлены в лист ожидания.\n\n"
        "Если заявка создана до 23:00, она участвует в ближайшем приоритетном распределении новой даты.",
        reply_markup=main_kb(msg.from_user.id),
    )


@router.message(F.text == "❌ Отменить заявку")
async def waitlist_cancel(msg: types.Message, state: FSMContext):
    await state.clear()
    ok = cancel_request_for_tg(msg.from_user.id)
    await msg.answer(
        "✅ Заявка отменена." if ok else "Активной заявки уже нет.",
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
        "📅 Новая дата для обычной записи открывается каждый день в 00:00.",

    "🤯 Что нового в PRA4KA 2.0":
        "🤯 <b>Что нового в PRA4KA 2.0</b>\n\n"
        "🔔 Лист ожидания\n⚡ Автозапись или предложение слота\n🕐 До 3 удобных интервалов\n🧺 Несколько машинок или любая\n🔄 Предложения более раннего дня\n🌙 Настройки уведомлений\n⏰ Таймер стирки и напоминание забрать вещи\n⚠️ Уведомление предыдущему пользователю, если вещи остались в машинке\n\n"
        "С 23:00 до 00:00 бот распределяет часть мест новой даты между заранее созданными заявками. В 00:00 оставшиеся места открываются для обычной записи.",

    "📘 Про лист ожидания":
        "🔔 <b>Лист ожидания</b>\n\n"
        "Лист ожидания позволяет заранее указать удобное для вас время, а поиск свободной записи бот возьмёт на себя.\n\n"
        "Можно выбрать до 3 интервалов, одну, несколько или любую стиральную машину.\n\n"
        "⚡ Автозапись: бот сам занимает подходящее место.\n\n"
        "🔔 Сначала спросить: бот предлагает конкретный слот и удерживает его 2 минуты.\n\n"
        "Конкретную дату выбирать не нужно. После получения записи заявка закрывается.",

    "⚖️ Как работает очередь":
        "⚖️ <b>Как работает очередь</b>\n\n"
        "Если на одно место претендуют несколько человек, бот учитывает, как давно человек ждёт, сколько подходящих вариантов у него есть и как часто он стирал за последние 30 дней.\n\n"
        "Если одному подходит только 20:00, а другому подходит почти весь вечер, система постарается оставить редкий слот первому и подобрать второму другой час.\n\n"
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
        "Если место освобождается днём, бот тоже проверяет лист ожидания.",
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
    result = await accept_hold(hold_id, callback.from_user.id)
    if not result:
        return await callback.answer("Этот слот уже недоступен.", show_alert=True)
    await callback.answer("Готово ✅")
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
