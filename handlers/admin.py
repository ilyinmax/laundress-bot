from aiogram import Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta
from database import get_conn, b64_decode_field
from config import ADMIN_IDS
from openpyxl import Workbook
import os

router = Router()


# Проверка прав
def is_admin(user_id) -> bool:
    try:
        return int(user_id) in [int(x) for x in ADMIN_IDS]
    except Exception:
        return False


@router.message(F.text.in_({"/admin_panel", "/panel"}))
async def admin_panel(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 У вас нет прав администратора.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Расписание", callback_data="admin_menu_schedule"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_menu_stats"),
        ],
        [
            InlineKeyboardButton(text="📤 Экспорт", callback_data="admin_menu_export"),
        ]
    ])

    await msg.answer(
        "🧺 <b>Панель администратора</b>\n\n"
        "Выберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )

# === Расписание ===
@router.callback_query(F.data == "admin_menu_schedule")
async def open_schedule(callback: types.CallbackQuery):
    today = datetime.now().date()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=(today + timedelta(days=i)).strftime("%d.%m.%Y"),
            callback_data=f"admin_day_{(today + timedelta(days=i)).isoformat()}"
        )]
        for i in range(3)
    ])
    await callback.message.edit_text(
        "📅 Выберите день для просмотра расписания:",
        reply_markup=kb
    )


# === Статистика ===
@router.callback_query(F.data == "admin_menu_stats")
async def show_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")

    today = datetime.now().date()
    week_ago = today - timedelta(days=7)

    with get_conn() as conn:
        cur = conn.execute("""
            SELECT COUNT(*) FROM bookings WHERE date >= ? AND date <= ?
        """, (week_ago.isoformat(), today.isoformat()))
        total = cur.fetchone()[0]

        cur = conn.execute("""
            SELECT m.type, COUNT(*) FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            WHERE date >= ? AND date <= ?
            GROUP BY m.type
        """, (week_ago.isoformat(), today.isoformat()))
        by_type = cur.fetchall()

    text = f"📊 <b>Статистика за неделю ({week_ago.strftime('%d.%m')} – {today.strftime('%d.%m')})</b>\n\n"
    text += f"Всего записей: <b>{total}</b>\n\n"

    for t, count in by_type:
        emoji = "🧺" if t == "wash" else "🌬"
        name = "Стиральные" if t == "wash" else "Сушилки"
        text += f"{emoji} {name}: <b>{count}</b>\n"

    await callback.message.edit_text(text, parse_mode="HTML")


# === Главная команда администратора ===
@router.message(F.text == "/admin")
async def admin_panel(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 У вас нет прав администратора.")

    today = datetime.now().date()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=(today + timedelta(days=i)).strftime("%d.%m.%Y"),
            callback_data=f"admin_day_{(today + timedelta(days=i)).isoformat()}"
        )]
        for i in range(3)
    ])
    await msg.answer("📅 Выберите день для просмотра расписания:", reply_markup=kb)


# === Просмотр расписания по дню ===
@router.callback_query(F.data.startswith("admin_day_"))
async def show_admin_schedule(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")

    date = callback.data.split("_")[2]
    with get_conn() as conn:
        cur = conn.execute("""
            SELECT b.id, m.name, b.hour, u.surname, u.room
            FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            JOIN users u ON b.user_id = u.id
            WHERE b.date = ?
            ORDER BY m.name, b.hour
        """, (date,))
        records = cur.fetchall()

    if not records:
        return await callback.message.edit_text(f"📅 {date}: записей нет.")

    text = f"🧺 <b>Записи на {date}</b>\n\n"
    buttons = []
    current_machine = None

    for booking_id, machine, hour, surname, room in records:
        surname = b64_decode_field(surname)
        room = b64_decode_field(room)
        if machine != current_machine:
            text += f"\n<b>{machine}</b>\n"
            current_machine = machine
        text += f"  ⏰ {hour}:00 — {surname} (комн. {room})\n"
        buttons.append([InlineKeyboardButton(
            text=f"❌ {machine} {hour}:00 ({surname})",
            callback_data=f"admin_del_{booking_id}_{date}"
        )])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

# === Удаление конкретной записи ===
@router.callback_query(F.data.startswith("admin_del_"))
async def delete_booking(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")

    _, _, booking_id, date = callback.data.split("_")
    booking_id = int(booking_id)

    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))

    await callback.answer("🗑️ Запись удалена!", show_alert=True)

    # Обновляем список после удаления
    with get_conn() as conn:
        cur = conn.execute("""
                           SELECT b.id, m.name, b.hour, u.surname, u.room
                           FROM bookings b
                                    JOIN machines m ON b.machine_id = m.id
                                    JOIN users u ON b.user_id = u.id
                           WHERE b.date = ?
                           ORDER BY m.name, b.hour
                           """, (date,))
        records = cur.fetchall()

    if not records:
        return await callback.message.edit_text(f"📅 {date}: записей больше нет.")

    text = f"🧺 <b>Записи на {date}</b>\n\n"
    buttons = []
    current_machine = None

    for booking_id, machine, hour, surname, room in records:
        if machine != current_machine:
            text += f"\n<b>{machine}</b>\n"
            current_machine = machine
        text += f"  ⏰ {hour}:00 — {surname} (комн. {room})\n"
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ {machine} {hour}:00 ({surname})",
                callback_data=f"admin_del_{booking_id}_{date}"
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# === Экспорт всех записей в Excel ===
@router.message(F.text == "/export")
async def export_bookings(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет доступа.")

    await msg.answer("📤 Формирую таблицу...")

    wb = Workbook()
    ws = wb.active
    ws.title = "Bookings"

    # Заголовки
    headers = ["ID", "Дата", "Час", "Машина", "Тип", "Фамилия", "Комната"]
    ws.append(headers)

    with get_conn() as conn:
        cur = conn.execute("""
            SELECT b.id, b.date, b.hour, m.name, m.type, u.surname, u.room
            FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            JOIN users u ON b.user_id = u.id
            ORDER BY b.date, b.hour
        """)
        rows = cur.fetchall()

    if not rows:
        return await msg.answer("Нет данных для экспорта.")

    # Добавляем строки
    for row in rows:
        id_, date, hour, machine, mtype, surname, room = row
        surname = b64_decode_field(surname)
        room = b64_decode_field(room)
        ws.append([id_, date, f"{hour}:00", machine, mtype, surname, room])

    # Красивый авторазмер колонок
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_len:
                    max_len = len(str(cell.value))
            except:
                pass
        ws.column_dimensions[col_letter].width = max_len + 2

    # Сохраняем во временный файл
    filename = f"bookings_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
    filepath = os.path.join(os.getcwd(), filename)
    wb.save(filepath)

    # Отправляем файл админу
    await msg.answer_document(types.FSInputFile(filepath), caption="📊 Экспорт всех записей")

    # Удаляем временный файл
    os.remove(filepath)

# === Экспорт всех записей в Excel ===
@router.callback_query(F.data == "admin_menu_export")
async def export_bookings(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")

    await callback.message.edit_text("📤 Формирую Excel-файл...")

    wb = Workbook()
    ws = wb.active
    ws.title = "Bookings"
    ws.append(["ID","Дата","Час","Машина","Тип","Фамилия","Комната"])

    with get_conn() as conn:
        cur = conn.execute("""
            SELECT b.id, b.date, b.hour, m.name, m.type, u.surname, u.room
            FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            JOIN users u ON b.user_id = u.id
            ORDER BY b.date, b.hour
        """)
        rows = cur.fetchall()

    if not rows:
        return await callback.message.edit_text("Нет данных для экспорта.")

    for id_, date, hour, machine, mtype, surname, room in rows:
        ws.append([id_, date, f"{hour}:00", machine, mtype,
                   b64_decode_field(surname), b64_decode_field(room)])

    # автоширина
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value else 0 for c in col) + 2
        ws.column_dimensions[col[0].column_letter].width = width

    fname = f"bookings_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
    wb.save(fname)
    await callback.message.answer_document(types.FSInputFile(fname), caption="📊 Экспорт всех записей")
    os.remove(fname)

    # вернём панель
    await admin_panel(callback.message)