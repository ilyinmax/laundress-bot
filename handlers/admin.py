from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta
from openpyxl import Workbook
import os
import pandas as pd

from config import ADMIN_IDS
from database import (
    get_conn,
    _b64d_try,
    init_db,
    ensure_user_by_surname_room,
    get_machine_id_by_name,
    create_booking,
    ban_user,
)

router = Router()

# === Проверка на администратора ===
def _normalize_admin_ids():
    if isinstance(ADMIN_IDS, (list, tuple, set)):
        return {str(x).strip() for x in ADMIN_IDS if str(x).strip()}
    s = str(ADMIN_IDS).strip().strip("[]")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return {p for p in parts}

ADMIN_SET = _normalize_admin_ids()

def is_admin(user_id: int) -> bool:
    try:
        return str(int(user_id)) in ADMIN_SET
    except Exception:
        return False

async def _render_schedule(message: types.Message, date: str):
    with get_conn() as conn:
        cur = conn.execute("""
            SELECT b.id, m.name, b.hour, u.surname, u.room, u.tg_id
            FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            JOIN users u ON b.user_id = u.id
            WHERE b.date = ?
            ORDER BY m.name, b.hour
        """, (date,))
        records = cur.fetchall()

    if not records:
        return await message.edit_text(f"📅 {date}: записей нет.")

    text = f"🧺 <b>Записи на {date}</b>\n\n"
    buttons = []
    current_machine = None
    for booking_id, machine, hour, surname, room, tg_id in records:
        surname = _b64d_try(surname); room = _b64d_try(room)
        if machine != current_machine:
            text += f"\n<b>{machine}</b>\n"; current_machine = machine
        text += f"  ⏰ {hour}:00 — {surname} (комн. {room})\n"
        buttons.append([
            InlineKeyboardButton(text=f"❌ Удалить {hour}:00 ({surname})",
                                 callback_data=f"admin_del_{booking_id}_{date}"),
            InlineKeyboardButton(text="🚫 Бан",
                                 callback_data=f"admin_ban_{tg_id}_{date}")
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# === Импорт из Excel ===
def import_bookings_from_xlsx(path: str) -> tuple[int, int, list[str]]:
    df = pd.read_excel(path)
    df["date_iso"] = pd.to_datetime(df["Дата"]).dt.date.astype(str)
    df["hour"] = pd.to_datetime(df["Час"].astype(str)).dt.hour

    inserted, skipped = 0, 0
    errors: list[str] = []

    for row in df.itertuples(index=False):
        try:
            surname = str(getattr(row, "Фамилия")).strip()
            room = str(getattr(row, "Комната")).strip()
            m_name = str(getattr(row, "Машина")).strip()
            date_iso = str(getattr(row, "date_iso"))
            hour = int(getattr(row, "hour"))

            uid = ensure_user_by_surname_room(surname, room)
            mid = get_machine_id_by_name(m_name)
            if not mid:
                errors.append(f"Нет машины в БД: {m_name}")
                skipped += 1
                continue

            try:
                create_booking(uid, mid, date_iso, hour)
                inserted += 1
            except Exception:
                skipped += 1
        except Exception as e:
            skipped += 1
            errors.append(f"Ошибка строки: {e}")

    return inserted, skipped, errors


@router.message(Command("import"))
async def cmd_import(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет прав администратора.")
    await msg.answer("📥 Пришлите Excel-файл (.xlsx) с записями для импорта.")


@router.message(F.document & (F.document.mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
async def handle_xlsx(msg: types.Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет прав администратора.")

    f = await bot.get_file(msg.document.file_id)
    path = f"/tmp/{msg.document.file_unique_id}.xlsx"
    await bot.download_file(f.file_path, path)

    init_db()
    added, skipped, errors = import_bookings_from_xlsx(path)

    text = f"✅ Импорт завершён.\nДобавлено: {added}\nПропущено: {skipped}"
    if errors:
        text += f"\n⚠️ Замечания: {len(errors)} (см. логи на сервере)"
    await msg.answer(text)


# === Панель администратора ===
@router.message(Command("admin"))
@router.message(F.text == "/admin")
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
        "🧺 <b>Панель администратора</b>\n\nВыберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )


# === Расписание ===
@router.callback_query(F.data == "admin_menu_schedule")
async def open_schedule(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")

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
    week_end = today + timedelta(days=6)

    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE date BETWEEN ? AND ?",
            (today.isoformat(), week_end.isoformat())
        ).fetchone()[0]

        by_type = conn.execute("""
            SELECT m.type, COUNT(*) FROM bookings b
            JOIN machines m ON b.machine_id = m.id
            WHERE b.date BETWEEN ? AND ?
            GROUP BY m.type
        """, (today.isoformat(), week_end.isoformat())).fetchall()

    text = (
        f"📊 <b>Статистика на неделю ({today.strftime('%d.%m')} – {week_end.strftime('%d.%m')})</b>\n\n"
        f"Всего записей: <b>{total}</b>\n\n"
    )
    for t, count in by_type:
        emoji = "🧺" if t == "wash" else "🌬️"
        name = "Стиральные" if t == "wash" else "Сушилки"
        text += f"{emoji} {name}: <b>{count}</b>\n"

    await callback.message.edit_text(text, parse_mode="HTML")


# === Просмотр расписания по дню ===
@router.callback_query(F.data.startswith("admin_day_"))
async def show_admin_schedule(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")
    date = callback.data.split("_", 2)[2]
    await _render_schedule(callback.message, date)


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
    await _render_schedule(callback.message, date)


# === Бан пользователя ===
@router.callback_query(F.data.startswith("admin_ban_"))
async def admin_ban_user(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")

    try:
        _, _, tg_id_str, date = callback.data.split("_", 3)
        tg_id = int(tg_id_str)
    except Exception:
        return await callback.answer("Ошибка данных бан-кнопки.", show_alert=True)

    ban_user(tg_id, reason="Бан из админ-панели", days=7)
    await callback.answer("🚫 Пользователь заблокирован на 7 дней.", show_alert=True)
    await _render_schedule(callback.message, date)



# === Экспорт записей ===
@router.message(Command("export"))
@router.callback_query(F.data == "admin_menu_export")
async def export_bookings(event: types.Message | types.CallbackQuery):
    msg = event.message if isinstance(event, types.CallbackQuery) else event
    user_id = msg.from_user.id if msg.from_user else event.from_user.id

    if not is_admin(user_id):
        if isinstance(event, types.CallbackQuery):
            return await event.answer("🚫 Нет доступа.")
        return await msg.answer("🚫 Нет доступа.")

    await msg.answer("📤 Формирую таблицу...")

    wb = Workbook()
    ws = wb.active
    ws.title = "Bookings"
    ws.append(["ID", "Дата", "Час", "Машина", "Тип", "Фамилия", "Комната"])

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

    for id_, date, hour, machine, mtype, surname, room in rows:
        ws.append([id_, date, f"{hour}:00", machine, mtype,
                   _b64d_try(surname), _b64d_try(room)])

    # автоширина
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value else 0 for c in col) + 2
        ws.column_dimensions[col[0].column_letter].width = width

    fname = f"bookings_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
    wb.save(fname)
    await msg.answer_document(types.FSInputFile(fname), caption="📊 Экспорт всех записей")
    os.remove(fname)
