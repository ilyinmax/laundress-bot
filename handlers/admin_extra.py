from __future__ import annotations

import html
from calendar import monthrange
from datetime import date, datetime, timedelta

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import TIMEZONE, WORKING_HOURS
from database import (
    _b64d_try,
    ensure_user_by_surname_room,
    get_conn,
    get_free_hours,
    get_free_hours_effective,
    get_user_bookings_today,
    daily_limit_reached,
    is_admin,
)
from zoneinfo import ZoneInfo
from booking_service import create_booking_safe, BookingError

TZ = ZoneInfo(TIMEZONE)
router = Router()

EARLY_BOOKING_MAX_DAYS = 365
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTHS = (
    "",
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


class EarlyBooking(StatesGroup):
    choosing_target = State()
    other_person = State()
    choosing_date = State()
    choosing_machine = State()
    choosing_hour = State()
    confirming = State()


def _admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ Ранняя запись", callback_data="early_start"),
            InlineKeyboardButton(text="📚 Все команды", callback_data="admin_extra_commands"),
        ],
        [
            InlineKeyboardButton(text="📅 Расписание", callback_data="admin_menu_schedule"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_menu_stats"),
        ],
        [
            InlineKeyboardButton(text="👥 Пользователи", callback_data="lf_users_0"),
            InlineKeyboardButton(text="🔔 Лист ожидания", callback_data="admin_waitlist_0"),
        ],
        [InlineKeyboardButton(text="📤 Экспорт", callback_data="admin_menu_export")],
    ])


ADMIN_COMMANDS_TEXT = """🧺 <b>Все команды бота</b>

<b>Пользовательские:</b>
/start - запуск и регистрация
/book - записаться
/mybookings - мои активные записи
/cancel - отменить запись
/edit - изменить фамилию и комнату
/help - помощь

<b>Администраторские:</b>
/admin - панель администратора
/early - ⭐ ранняя запись на любую дату
/admin_commands - этот список команд
/export - экспорт записей в Excel
/import - импорт записей из Excel
/machines - включить/выключить машины
/ban - заблокировать пользователя
/unban - разблокировать пользователя
/banned - список заблокированных
/abookfio - ручная запись старым способом
/notify_incomplete - напомнить заполнить профиль
/test_reminder - тест напоминания
/laundry_news - разослать список работающих машин

💡 Для обычной работы достаточно <b>/admin</b>: ранняя запись теперь делается кнопками, без machine_id и длинной команды."""


def _target_text(data: dict) -> str:
    surname = data.get("target_surname") or "-"
    room = data.get("target_room") or "-"
    return f"{surname}, комн. {room}"


def _month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    today = datetime.now(TZ).date()
    max_day = today + timedelta(days=EARLY_BOOKING_MAX_DAYS)
    first = date(year, month, 1)
    last_day = monthrange(year, month)[1]

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=day, callback_data="early_noop") for day in WEEKDAYS]
    ]

    week: list[InlineKeyboardButton] = []
    for _ in range(first.weekday()):
        week.append(InlineKeyboardButton(text=" ", callback_data="early_noop"))

    for day_num in range(1, last_day + 1):
        current = date(year, month, day_num)
        allowed = today <= current <= max_day
        text = str(day_num) if allowed else "·"
        callback_data = f"early_day_{current.isoformat()}" if allowed else "early_noop"
        week.append(InlineKeyboardButton(text=text, callback_data=callback_data))
        if len(week) == 7:
            rows.append(week)
            week = []

    if week:
        while len(week) < 7:
            week.append(InlineKeyboardButton(text=" ", callback_data="early_noop"))
        rows.append(week)

    nav: list[InlineKeyboardButton] = []
    current_month = date(today.year, today.month, 1)
    max_month = date(max_day.year, max_day.month, 1)
    if first > current_month:
        py, pm = _month_shift(year, month, -1)
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"early_month_{py:04d}-{pm:02d}"))
    nav.append(InlineKeyboardButton(text=f"{MONTHS[month]} {year}", callback_data="early_noop"))
    if first < max_month:
        ny, nm = _month_shift(year, month, 1)
        nav.append(InlineKeyboardButton(text="›", callback_data=f"early_month_{ny:04d}-{nm:02d}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="early_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_calendar(message: types.Message, state: FSMContext, *, edit: bool = True) -> None:
    data = await state.get_data()
    today = datetime.now(TZ).date()
    year = int(data.get("calendar_year", today.year))
    month = int(data.get("calendar_month", today.month))
    await state.set_state(EarlyBooking.choosing_date)
    text = (
        "⭐ <b>Ранняя запись</b>\n\n"
        f"👤 {_target_text(data)}\n"
        "📅 Выберите дату:"
    )
    kb = _calendar_keyboard(year, month)
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def _start_early(event: types.Message | types.CallbackQuery, state: FSMContext) -> None:
    user_id = event.from_user.id
    if not is_admin(user_id):
        if isinstance(event, types.CallbackQuery):
            await event.answer("🚫 Нет доступа.", show_alert=True)
        else:
            await event.answer("🚫 Нет доступа.")
        return

    await state.clear()
    await state.set_state(EarlyBooking.choosing_target)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🙋 Себя", callback_data="early_target_self"),
            InlineKeyboardButton(text="👤 Другого", callback_data="early_target_other"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="early_cancel")],
    ])
    text = "⭐ <b>Ранняя запись</b>\n\nКого записываем?"
    if isinstance(event, types.CallbackQuery):
        await event.answer()
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("admin"))
@router.message(F.text == "/admin")
async def admin_panel(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 У вас нет прав администратора.")
    await state.clear()
    await msg.answer(
        "🧺 <b>Панель администратора</b>\n\nВыберите действие:",
        reply_markup=_admin_menu(),
        parse_mode="HTML",
    )


@router.message(Command("admin_commands"))
async def admin_commands(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет доступа.")
    await msg.answer(ADMIN_COMMANDS_TEXT, parse_mode="HTML")


@router.callback_query(F.data == "admin_extra_commands")
async def admin_commands_callback(callback: types.CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_extra_home")]
    ])
    await callback.message.edit_text(ADMIN_COMMANDS_TEXT, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_extra_home")
async def admin_home(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    await state.clear()
    await callback.message.edit_text(
        "🧺 <b>Панель администратора</b>\n\nВыберите действие:",
        reply_markup=_admin_menu(),
        parse_mode="HTML",
    )


WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
ADMIN_WAITLIST_PAGE = 8


def _fmt_dt(value) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).strftime("%d.%m %H:%M")
    except Exception:
        return str(value)


def _admin_waitlist_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    pages = max(1, (total + ADMIN_WAITLIST_PAGE - 1) // ADMIN_WAITLIST_PAGE)
    page = min(max(0, page), pages - 1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"admin_waitlist_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="admin_waitlist_noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"admin_waitlist_{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_extra_home")],
    ])


@router.callback_query(F.data == "admin_waitlist_noop")
async def admin_waitlist_noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("admin_waitlist_"))
async def admin_waitlist(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    try:
        page = max(0, int(callback.data.removeprefix("admin_waitlist_")))
    except Exception:
        page = 0

    with get_conn() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM waitlist_requests WHERE status='active'"
        ).fetchone()[0])
        pages = max(1, (total + ADMIN_WAITLIST_PAGE - 1) // ADMIN_WAITLIST_PAGE)
        page = min(page, pages - 1)
        rows = conn.execute(
            """
            SELECT wr.id,u.surname,u.room,u.username,wr.mode,wr.any_machine,
                   wr.priority_since
            FROM waitlist_requests wr
            JOIN users u ON u.id=wr.user_id
            WHERE wr.status='active'
            ORDER BY wr.priority_since,wr.id
            LIMIT ? OFFSET ?
            """,
            (ADMIN_WAITLIST_PAGE, page * ADMIN_WAITLIST_PAGE),
        ).fetchall()

        ids = [int(r[0]) for r in rows]
        intervals = {}
        schedules = {}
        machines = {}
        weekdays = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            for rid,a,b in conn.execute(
                f"SELECT request_id,start_hour,end_hour FROM waitlist_intervals WHERE request_id IN ({marks}) ORDER BY start_hour",
                tuple(ids),
            ).fetchall():
                intervals.setdefault(int(rid), []).append((int(a),int(b)))
            for rid,weekday,a,b in conn.execute(
                f"""
                SELECT request_id,weekday,start_hour,end_hour
                FROM waitlist_schedule
                WHERE request_id IN ({marks})
                ORDER BY request_id,weekday,start_hour
                """,
                tuple(ids),
            ).fetchall():
                schedules.setdefault(int(rid), {}).setdefault(int(weekday), []).append((int(a),int(b)))
            for rid,name in conn.execute(
                f"""
                SELECT wm.request_id,m.name
                FROM waitlist_machines wm
                JOIN machines m ON m.id=wm.machine_id
                WHERE wm.request_id IN ({marks})
                ORDER BY m.name
                """,
                tuple(ids),
            ).fetchall():
                machines.setdefault(int(rid), []).append(str(name))
            for rid,weekday in conn.execute(
                f"SELECT request_id,weekday FROM waitlist_weekdays WHERE request_id IN ({marks}) ORDER BY weekday",
                tuple(ids),
            ).fetchall():
                weekdays.setdefault(int(rid), []).append(int(weekday))

    lines = [f"🔔 <b>Активные заявки: {total}</b>"]
    if not rows:
        lines.append("")
        lines.append("Активных заявок нет.")
    for idx,(rid,surname,room,username,mode,any_machine,priority_since) in enumerate(
        rows, start=page * ADMIN_WAITLIST_PAGE + 1
    ):
        su = html.escape(str(_b64d_try(surname) or "-"))
        ro = html.escape(str(_b64d_try(room) or "-"))
        un = f"@{html.escape(str(username))}" if username else "без username"
        request_schedule = schedules.get(int(rid), {})
        if request_schedule:
            schedule_parts = []
            for day in sorted(request_schedule):
                times = ",".join(
                    f"{a:02d}:00-{b:02d}:00"
                    for a,b in request_schedule[day]
                )
                schedule_parts.append(f"{WEEKDAY_SHORT[day]} {times}")
            schedule_text = "; ".join(schedule_parts)
        else:
            day_values = weekdays.get(int(rid), [])
            day_text = "любой день" if not day_values else ",".join(WEEKDAY_SHORT[x] for x in day_values)
            time_text = ", ".join(f"{a:02d}:00-{b:02d}:00" for a,b in intervals.get(int(rid), [])) or "-"
            schedule_text = f"{day_text} {time_text}"
        machine_text = "любая" if bool(any_machine) else ", ".join(machines.get(int(rid), [])) or "-"
        mode_text = "AUTO" if str(mode) == "auto" else "спросить"
        lines += [
            "",
            f"<b>{idx}. {su}</b> · комн. {ro} · {un}",
            f"📆 {html.escape(schedule_text)}",
            f"🧺 {html.escape(machine_text)} · {mode_text} · с {_fmt_dt(priority_since)}",
        ]

    await callback.answer()
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_admin_waitlist_keyboard(page,total),
    )


@router.message(Command("early"))
async def early_command(msg: types.Message, state: FSMContext):
    await _start_early(msg, state)


@router.callback_query(F.data == "early_start")
async def early_callback(callback: types.CallbackQuery, state: FSMContext):
    await _start_early(callback, state)


@router.callback_query(F.data == "early_target_self")
async def early_target_self(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, surname, room FROM users WHERE tg_id=?",
            (callback.from_user.id,),
        ).fetchone()
    if not row or not row[1] or not row[2]:
        return await callback.message.edit_text(
            "Сначала заполните свой профиль через /start или /edit."
        )

    user_id, surname, room = row
    await state.update_data(
        target_user_id=int(user_id),
        target_surname=_b64d_try(surname),
        target_room=_b64d_try(room),
    )
    await _show_calendar(callback.message, state)


@router.callback_query(F.data == "early_target_other")
async def early_target_other(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    await state.set_state(EarlyBooking.other_person)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="early_cancel")]
    ])
    await callback.message.edit_text(
        "👤 Введите фамилию и комнату одним сообщением.\n\n"
        "Например: <code>Иванов 412</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(EarlyBooking.other_person)
async def early_other_person_input(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        await state.clear()
        return await msg.answer("🚫 Нет доступа.")

    raw = (msg.text or "").strip()
    try:
        surname, room = raw.rsplit(maxsplit=1)
    except ValueError:
        return await msg.answer("Формат: <code>Фамилия Комната</code>, например <code>Иванов 412</code>.", parse_mode="HTML")

    surname = surname.strip()
    room = room.strip()
    if not surname or not room.isdigit() or not (100 <= int(room) <= 555):
        return await msg.answer("Комната должна быть числом от 100 до 555. Например: <code>Иванов 412</code>.", parse_mode="HTML")

    user_id = ensure_user_by_surname_room(surname, room)
    await state.update_data(
        target_user_id=int(user_id),
        target_surname=surname,
        target_room=room,
    )
    await _show_calendar(msg, state, edit=False)


@router.callback_query(F.data == "early_noop")
async def early_noop(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("early_month_"))
async def early_change_month(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    try:
        ym = callback.data.removeprefix("early_month_")
        year_s, month_s = ym.split("-", 1)
        year, month = int(year_s), int(month_s)
        assert 1 <= month <= 12
    except Exception:
        return await callback.answer("Некорректный месяц.", show_alert=True)
    await state.update_data(calendar_year=year, calendar_month=month)
    await _show_calendar(callback.message, state)


@router.callback_query(F.data.startswith("early_day_"))
async def early_choose_day(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    try:
        date_iso = callback.data.removeprefix("early_day_")
        selected = date.fromisoformat(date_iso)
    except ValueError:
        return await callback.answer("Некорректная дата.", show_alert=True)

    today = datetime.now(TZ).date()
    if not (today <= selected <= today + timedelta(days=EARLY_BOOKING_MAX_DAYS)):
        return await callback.answer("Дата вне доступного диапазона.", show_alert=True)

    data = await state.get_data()
    user_id = data.get("target_user_id")
    if not user_id:
        return await callback.message.edit_text("Сессия устарела. Откройте /early заново.")

    with get_conn() as conn:
        machines = conn.execute(
            "SELECT id, type, name FROM machines WHERE is_active ORDER BY type, name"
        ).fetchall()

    rows: list[list[InlineKeyboardButton]] = []
    for machine_id, machine_type, machine_name in machines:
        if daily_limit_reached(int(user_id), date_iso, machine_type):
            continue
        free = get_free_hours_effective(int(machine_id), date_iso)
        if selected == today:
            now_hour = datetime.now(TZ).hour
            free = [h for h in free if h > now_hour]
        free = [h for h in free if h in WORKING_HOURS]
        if not free:
            continue
        icon = "🧺" if machine_type == "wash" else "🌬️"
        rows.append([
            InlineKeyboardButton(
                text=f"{icon} {machine_name} - {len(free)} свободно",
                callback_data=f"early_machine_{int(machine_id)}_{date_iso}",
            )
        ])

    rows.append([InlineKeyboardButton(text="⬅️ К календарю", callback_data="early_back_calendar")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="early_cancel")])
    await state.update_data(selected_date=date_iso)
    await state.set_state(EarlyBooking.choosing_machine)

    if len(rows) == 2:
        text = f"На <b>{selected.strftime('%d.%m.%Y')}</b> доступных машин нет или у пользователя уже есть запись на каждый тип."
    else:
        text = (
            "⭐ <b>Ранняя запись</b>\n\n"
            f"👤 {_target_text(data)}\n"
            f"📅 {selected.strftime('%d.%m.%Y')}\n\n"
            "Выберите машину:"
        )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@router.callback_query(F.data == "early_back_calendar")
async def early_back_calendar(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await _show_calendar(callback.message, state)


@router.callback_query(F.data.startswith("early_machine_"))
async def early_choose_machine(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    try:
        rest = callback.data.removeprefix("early_machine_")
        machine_id_s, date_iso = rest.split("_", 1)
        machine_id = int(machine_id_s)
        selected = date.fromisoformat(date_iso)
    except Exception:
        return await callback.answer("Некорректные данные машины.", show_alert=True)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT type, name FROM machines WHERE id=? AND is_active",
            (machine_id,),
        ).fetchone()
    if not row:
        return await callback.answer("Эта машина сейчас недоступна.", show_alert=True)
    machine_type, machine_name = row

    data = await state.get_data()
    user_id = data.get("target_user_id")
    if not user_id:
        return await callback.message.edit_text("Сессия устарела. Откройте /early заново.")
    if daily_limit_reached(int(user_id), date_iso, machine_type):
        return await callback.answer("У пользователя уже есть запись на этот тип машины в этот день.", show_alert=True)

    free = get_free_hours_effective(machine_id, date_iso)
    if selected == datetime.now(TZ).date():
        free = [h for h in free if h > datetime.now(TZ).hour]
    free = sorted(h for h in free if h in WORKING_HOURS)
    if not free:
        return await callback.answer("Свободных часов уже не осталось.", show_alert=True)

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(free), 3):
        rows.append([
            InlineKeyboardButton(
                text=f"{h:02d}:00",
                callback_data=f"early_hour_{machine_id}_{date_iso}_{h}",
            )
            for h in free[i:i + 3]
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К машинам", callback_data=f"early_day_{date_iso}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="early_cancel")])
    await state.set_state(EarlyBooking.choosing_hour)
    await callback.message.edit_text(
        "⭐ <b>Ранняя запись</b>\n\n"
        f"👤 {_target_text(data)}\n"
        f"📅 {selected.strftime('%d.%m.%Y')}\n"
        f"{'🧺' if machine_type == 'wash' else '🌬️'} {machine_name}\n\n"
        "Выберите время:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("early_hour_"))
async def early_choose_hour(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)
    try:
        rest = callback.data.removeprefix("early_hour_")
        machine_id_s, date_iso, hour_s = rest.split("_", 2)
        machine_id, hour = int(machine_id_s), int(hour_s)
        selected = date.fromisoformat(date_iso)
    except Exception:
        return await callback.answer("Некорректный слот.", show_alert=True)

    with get_conn() as conn:
        row = conn.execute("SELECT type, name FROM machines WHERE id=?", (machine_id,)).fetchone()
    if not row:
        return await callback.answer("Машина не найдена.", show_alert=True)
    machine_type, machine_name = row

    data = await state.get_data()
    await state.update_data(
        selected_machine_id=machine_id,
        selected_machine_type=machine_type,
        selected_machine_name=machine_name,
        selected_date=date_iso,
        selected_hour=hour,
    )
    await state.set_state(EarlyBooking.confirming)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Записать", callback_data="early_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="early_cancel"),
        ],
        [InlineKeyboardButton(text="⬅️ К времени", callback_data=f"early_machine_{machine_id}_{date_iso}")],
    ])
    await callback.message.edit_text(
        "⭐ <b>Подтвердите раннюю запись</b>\n\n"
        f"👤 {_target_text(data)}\n"
        f"📅 {selected.strftime('%d.%m.%Y')}\n"
        f"⏰ {hour:02d}:00\n"
        f"{'🧺' if machine_type == 'wash' else '🌬️'} {machine_name}",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "early_confirm")
async def early_confirm(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    data = await state.get_data()
    try:
        user_id = int(data["target_user_id"])
        machine_id = int(data["selected_machine_id"])
        machine_type = str(data["selected_machine_type"])
        machine_name = str(data["selected_machine_name"])
        date_iso = str(data["selected_date"])
        hour = int(data["selected_hour"])
    except Exception:
        await state.clear()
        return await callback.message.edit_text("Сессия устарела. Откройте /early заново.")

    if daily_limit_reached(user_id, date_iso, machine_type):
        return await callback.answer("У пользователя уже есть запись на этот тип машины в этот день.", show_alert=True)
    if hour not in get_free_hours_effective(machine_id, date_iso):
        return await callback.answer("Этот слот только что заняли. Выберите другое время.", show_alert=True)

    try:
        result = await create_booking_safe(user_id, machine_id, date_iso, hour)
    except BookingError:
        return await callback.answer("Не удалось создать запись. Возможно, слот уже занят.", show_alert=True)

    with get_conn() as conn:
        tg_row = conn.execute("SELECT tg_id FROM users WHERE id=?", (user_id,)).fetchone()
    if tg_row and int(tg_row[0]) > 0:
        from handlers.laundry_features import schedule_reminder
        await schedule_reminder(int(tg_row[0]), result.machine_name, result.date, result.hour, 30)

    selected = date.fromisoformat(date_iso)
    target = _target_text(data)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Ещё ранняя запись", callback_data="early_start")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_extra_home")],
    ])
    await callback.message.edit_text(
        "✅ <b>Ранняя запись создана</b>\n\n"
        f"👤 {target}\n"
        f"📅 {selected.strftime('%d.%m.%Y')}\n"
        f"⏰ {hour:02d}:00\n"
        f"{'🧺' if machine_type == 'wash' else '🌬️'} {machine_name}",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "early_cancel")
async def early_cancel(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    await state.clear()
    if is_admin(callback.from_user.id):
        await callback.message.edit_text(
            "🧺 <b>Панель администратора</b>\n\nВыберите действие:",
            reply_markup=_admin_menu(),
            parse_mode="HTML",
        )
