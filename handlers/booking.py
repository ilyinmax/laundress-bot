from aiogram import Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from database import *
from datetime import datetime, timedelta
from config import WASHING_MACHINES, DRYERS
from keyboards import main_menu
from scheduler import schedule_reminder
from aiogram import Bot
import sqlite3
from aiogram.exceptions import TelegramBadRequest

router = Router()

def _norm_kb(kb: InlineKeyboardMarkup | None):
    if not kb:
        return None
    rows = []
    for row in kb.inline_keyboard:
        rows.append(tuple(
            (btn.text, getattr(btn, "callback_data", None), getattr(btn, "url", None))
            for btn in row
        ))
    return tuple(rows)

async def safe_edit(msg: Message, *, text: str | None = None,
                    reply_markup: InlineKeyboardMarkup | None = None,
                    parse_mode: str | None = "HTML"):
    if msg is None:
        return None

    cur_text = (msg.text or msg.caption or "")
    cur_kb = _norm_kb(getattr(msg, "reply_markup", None))
    new_kb = _norm_kb(reply_markup)

    try:
        # меняем текст (и при необходимости клавиатуру)
        if text is not None and text != cur_text:
            return await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

        # текст тот же — меняем только клавиатуру, если она реально другая
        if new_kb is not None and new_kb != cur_kb:
            return await msg.edit_reply_markup(reply_markup=reply_markup)

        # ничего не изменилось — ничего не делаем
        return None

    except TelegramBadRequest as e:
        s = str(e).lower()
        if "message is not modified" in s or "message to edit not found" in s:
            return None
        raise



# === Команда бронирования ===
@router.message(F.text == "/book")
async def choose_type(msg: types.Message):
    user = get_user(msg.from_user.id)
    if not user:
        return await msg.answer("Сначала пройдите регистрацию с помощью /start")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стиральная машина", callback_data="type_wash")],
        [InlineKeyboardButton(text="Сушилка", callback_data="type_dry")]
    ])
    await msg.answer("Выберите тип машины:", reply_markup=kb)

# === Выбор машины ===
@router.callback_query(F.data.startswith("type_"))
async def choose_machine(callback: types.CallbackQuery):
    await callback.answer()  # быстрый ACK
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    type_ = callback.data.split("_")[1]
    machines = get_machines_by_type(type_)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=m[2], callback_data=f"machine_{m[0]}")] for m in machines
    ])
    # добавить кнопку "Назад к типам"
    kb.inline_keyboard.append([InlineKeyboardButton(text="⬅️ К типам", callback_data="back_to_types")])

    await safe_edit(msg=callback.message, text="Выберите машину:", reply_markup=kb)


# === Выбор дня ===
@router.callback_query(F.data.startswith("machine_"))
async def choose_day(callback: types.CallbackQuery, machine_id: int | None = None):
    await callback.answer()
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if not machine_id:
        machine_id = int(callback.data.split("_")[1])

    now = datetime.now()
    today = now.date()

    # если уже 23:00 или позже, убираем сегодняшний день
    if now.hour >= 23:
        start_offset = 1  # начинаем с завтрашнего дня
    else:
        start_offset = 0  # включаем сегодня

    # получаем тип и имя машины (тип нужен для "назад к машинам")
    with get_conn() as conn:
        cur = conn.execute("SELECT type, name FROM machines WHERE id=?", (machine_id,))
        machine_type, machine_name = cur.fetchone()

    total_slots = 15  # 9:00–23:00
    days_buttons = []

    # создаём список кнопок на 3 дня вперёд (но пропускаем сегодня после 23:00)
    for i in range(start_offset, start_offset + 3):
        date = today + timedelta(days=i)
        date_str = date.isoformat()

        # считаем количество занятых слотов
        with get_conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE machine_id=? AND date=?",
                (machine_id, date_str)
            )
            booked = cur.fetchone()[0]

        free = total_slots - booked

        if free <= 0:
            text = f"⚫️ {date.strftime('%d.%m')} — нет свободных мест"
            days_buttons.append([InlineKeyboardButton(text=text, callback_data="none")])
        else:
            text = f"📅 {date.strftime('%d.%m')} — {free} свободно / {booked} занято"
            days_buttons.append([InlineKeyboardButton(text=text, callback_data=f"day_{machine_id}_{date_str}")])

    # кнопка выхода в меню
    days_buttons.append([InlineKeyboardButton(text="⬅️ К машинам", callback_data=f"back_to_machines_{machine_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=days_buttons)
    await safe_edit(
        msg=callback.message,
        text=f"📅 <b>{machine_name}</b>\n\nВыберите день для записи:",
        reply_markup=kb,
        parse_mode="HTML"
    )

# === Выбор времени со статусами ===
@router.callback_query(F.data.startswith("day_"))
async def choose_hour(callback: types.CallbackQuery):
    await callback.answer()  # быстрый ACK
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _, machine_id, date = callback.data.split("_")
    machine_id = int(machine_id)
    free = get_free_hours(machine_id, date)
    all_hours = range(9, 24)

    today = datetime.now().date()
    selected_date = datetime.fromisoformat(date).date()
    current_hour = datetime.now().hour

    kb = InlineKeyboardMarkup(inline_keyboard=[])

    has_any = False
    for h in all_hours:
        # блокируем прошедшие часы для сегодняшнего дня
        if selected_date == today and h <= current_hour:
            continue

        elif h in free:
            text = f"🟢 {h}:00"
            data = f"book_{machine_id}_{date}_{h}"
        else:
            text = f"🔴 {h}:00"
            data = "busy"

        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=data)])
        has_any = True

    # Кнопки "Назад"
    back_buttons = []
    back_buttons.append(InlineKeyboardButton(text="⬅️ К дням", callback_data=f"back_to_days_{machine_id}"))
    back_buttons.append(InlineKeyboardButton(text="🏠 К типам", callback_data="back_to_types"))
    kb.inline_keyboard.append(back_buttons)

    if not has_any:
        return await safe_edit(callback.message, text=f"На {date} свободных часов не осталось.", reply_markup=kb)

    await safe_edit(msg=callback.message, text=f"Выберите время ({date}):", reply_markup=kb)

# === Защита от клика по занятым слотам ===
@router.callback_query(F.data == "busy")
async def busy_slot(callback: types.CallbackQuery):
    await callback.answer("Этот слот уже занят ❌", show_alert=True)

@router.callback_query(F.data == "to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.delete()  # убираем старое сообщение с inline-кнопками
    await callback.message.answer("🏠 Главное меню:", reply_markup=main_menu)

# === Финальное бронирование ===
@router.callback_query(F.data.startswith("book_"))
async def finalize(callback: types.CallbackQuery):
    await callback.answer()  # быстрый ACK
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _, machine_id, date, hour = callback.data.split("_")
    machine_id, hour = int(machine_id), int(hour)
    user = get_user(callback.from_user.id)

    # получаем тип и имя машины
    with get_conn() as conn:
        cur = conn.execute("SELECT type, name FROM machines WHERE id=?", (machine_id,))
        row = cur.fetchone()
        if not row:
            await safe_edit(msg=callback.message, text="Ошибка: машина не найдена.")
        machine_type, machine_name = row

    # проверяем ограничение: 1 запись на ТИП в день (стиралка/сушилка)
    if get_user_bookings_today(user[0], date, machine_type):
        type_text = "стиральную машину" if machine_type == "wash" else "сушилку"
        return await safe_edit(
            msg=callback.message,
            text=(
                f"⚠️ Вы уже записаны на {type_text} в этот день!\n"
                f"Можно только одну запись на каждый тип машины в сутки."
            ),
        )

    # пробуем забронировать 1 раз (и только здесь!)
    try:
        make_booking(user[0], machine_id, date, hour)
    except sqlite3.IntegrityError:
        # слот уже успели занять конкурентно — сообщаем аккуратно
        return await safe_edit(
            msg=callback.message,
            text="⚠️ Этот слот только что заняли.\nПожалуйста, выберите другое время ⏰",
            parse_mode="HTML",
        )

    # подтверждение + напоминание
    await safe_edit(
        msg=callback.message,
        text=(f"✅ Запись подтверждена!\n\n"
              f"📅 Дата: {date}\n"
              f"⏰ Время: {hour}:00\n"
              f"🧺 {machine_name}\n\n"
              f"Для отмены используйте /cancel"),
        parse_mode="HTML"
    )

    # напоминание за час до начала
    bot: Bot = callback.bot
    await schedule_reminder(bot, callback.from_user.id, machine_name, date, hour)


# === Просмотр и отмена записи ===
@router.message(F.text == "/cancel")
async def show_user_bookings(msg: types.Message):
    user = get_user(msg.from_user.id)
    if not user:
        return await msg.answer("Сначала пройдите регистрацию с помощью /start")

    with get_conn() as conn:
        cur = conn.execute("""
            SELECT b.id, m.name, b.date, b.hour
            FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            WHERE b.user_id = ?
            ORDER BY b.date, b.hour
        """, (user[0],))
        bookings = cur.fetchall()

    if not bookings:
        return await msg.answer("У вас нет активных записей.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{m} {d} {h}:00", callback_data=f"cancel_{bid}")]
        for bid, m, d, h in bookings
    ])
    await msg.answer("Ваши записи:", reply_markup=kb)

@router.callback_query(F.data.startswith("cancel_"))
async def cancel_booking(callback: types.CallbackQuery):
    booking_id = int(callback.data.split("_")[1])
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    await safe_edit(msg=callback.message, text="🗑️ Запись отменена.")


# === Просмотр всех активных записей без отмены ===
@router.message(F.text == "/mybookings")
async def show_future_bookings(msg: types.Message):
    user = get_user(msg.from_user.id)
    if not user:
        return await msg.answer("Сначала пройдите регистрацию с помощью /start")

    today = datetime.now().date()
    with get_conn() as conn:
        cur = conn.execute("""
            SELECT m.name, b.date, b.hour
            FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            WHERE b.user_id = ? AND date(b.date) >= ?
            ORDER BY b.date, b.hour
        """, (user[0], today.isoformat()))
        bookings = cur.fetchall()

    if not bookings:
        return await msg.answer("У вас нет активных записей.")

    text = "🧺 <b>Ваши записи:</b>\n\n"
    for name, date, hour in bookings:
        date_obj = datetime.fromisoformat(date).strftime("%d.%m.%Y")
        text += f"📅 {date_obj} — {hour}:00\n• {name}\n\n"

    await msg.answer(text, parse_mode="HTML")

@router.message(F.text == "🧺 Записаться")
async def btn_book(msg: types.Message):
    await choose_type(msg)

@router.message(F.text == "📋 Мои записи")
async def btn_mybookings(msg: types.Message):
    await show_future_bookings(msg)

@router.message(F.text == "❌ Отменить запись")
async def btn_cancel(msg: types.Message):
    await show_user_bookings(msg)

HELP_URL = "https://t.me/c/2528999666/11"

# === Помощь / информация ===
@router.message(F.text == "ℹ️ Помощь")
async def show_help(msg: types.Message):
    help_text = (
        "ℹ️ <b>Помощь по использованию бота</b>\n\n"
        "🧺 <b>Запись</b> — выберите свободное время и машину, чтобы записаться на стирку или сушку.\n"
        "📋 <b>Мои записи</b> — покажет все ваши активные записи.\n"
        "❌ <b>Отменить запись</b> — удалит вашу текущую бронь.\n\n"
        "⏰ Запись доступна с 9:00 до 23:00, не более одного слота в день.\n"
        "📅 Можно записаться максимум на 2 дня вперёд (сегодня, завтра, послезавтра)."
        "Если есть вопросы — пишите в <a href='{HELP_URL}'>Жалобы</a>."
    )
    await msg.answer(help_text, parse_mode="HTML")

@router.message(F.text == "/help")
async def cmd_help(msg: types.Message):
    await show_help(msg)

@router.callback_query(F.data == "none")
async def inactive_day(callback: types.CallbackQuery):
    await callback.answer("⚠️ В этот день все слоты заняты.", show_alert=True)

# из выбора времени → к дням
@router.callback_query(F.data.startswith("back_to_days_"))
async def back_to_days(callback: types.CallbackQuery):
    await callback.answer()
    machine_id = int(callback.data.split("_")[3])
    # вызываем тот же код, что и при выборе машины
    await choose_day(callback=callback, machine_id=machine_id)  # переиспользуем существующий хендлер


# из выбора дней → к выбору машин
@router.callback_query(F.data.startswith("back_to_machines_"))
async def back_to_machines(callback: types.CallbackQuery):
    await callback.answer()
    type_ = callback.data.split("_")[3]
    # имитируем "choose_machine"
    machines = get_machines_by_type(type_)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=m[2], callback_data=f"machine_{m[0]}")] for m in machines
    ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="⬅️ К типам", callback_data="back_to_types")])
    await safe_edit(callback.message, text="Выберите машину:", reply_markup=kb)


# из выбора машин → к выбору типа
@router.callback_query(F.data == "back_to_types")
async def back_to_types(callback: types.CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стиральная машина", callback_data="type_wash")],
        [InlineKeyboardButton(text="Сушилка", callback_data="type_dry")]
    ])
    await safe_edit(callback.message, text="Выберите тип машины:", reply_markup=kb)
