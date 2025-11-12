from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta
from openpyxl import Workbook
import os
import pandas as pd
from database import get_conn, unban_user  # unban_user уже есть в database.py
from aiogram.filters import Command
from database import ban_user, tg_id_by_username


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
    # ADMIN_IDS может быть: списком/множеством, строкой "123,456", строкой "['123','456']" и т.п.
    if isinstance(ADMIN_IDS, (list, tuple, set)):
        raw = ADMIN_IDS
    else:
        s = str(ADMIN_IDS).strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        raw = [part for part in s.split(",") if part.strip()]

    norm = set()
    for x in raw:
        t = str(x).strip().strip("'").strip('"')   # убираем кавычки и пробелы
        if t:
            norm.add(t)
    return norm

ADMIN_SET = _normalize_admin_ids()

def is_admin(user_id: int | str) -> bool:
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
    # корректно извлекаем actor и объект сообщения для ответа
    if isinstance(event, types.CallbackQuery):
        user_id = event.from_user.id         # ← именно кликающий
        msg = event.message
    else:
        user_id = event.from_user.id
        msg = event

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

@router.message(Command("banned"))
async def list_banned(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет доступа.")

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT tg_id, reason, banned_until, banned_at
            FROM banned
            ORDER BY banned_at DESC
        """).fetchall()

    if not rows:
        return await msg.answer("✅ Никто не забанен.")

    text_lines = ["🚫 <b>Заблокированные</b>:\n"]
    buttons = []
    for tg_id, reason, until, _ in rows:
        mention = f"<a href='tg://user?id={tg_id}'>{tg_id}</a>"
        reason = reason or "—"
        until  = until  or "—"
        text_lines.append(f"• {mention} — до {until}\n  Причина: {reason}")
        buttons.append([InlineKeyboardButton(text=f"Разбанить {tg_id}",
                                             callback_data=f"unban_{tg_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await msg.answer("\n".join(text_lines), parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("unban_"))
async def cb_unban(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.")

    try:
        tg_id = int(callback.data.split("_", 1)[1])
    except Exception:
        return await callback.answer("Ошибка данных.", show_alert=True)

    unban_user(tg_id)
    await callback.answer("✅ Пользователь разбанен.", show_alert=True)

    # Обновим список на экране
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT tg_id, reason, banned_until, banned_at
            FROM banned
            ORDER BY banned_at DESC
        """).fetchall()

    if not rows:
        return await callback.message.edit_text("✅ Никто не забанен.")

    text_lines = ["🚫 <b>Заблокированные</b>:\n"]
    buttons = []
    for tg_id2, reason, until, _ in rows:
        mention = f"<a href='tg://user?id={tg_id2}'>{tg_id2}</a>"
        reason = reason or "—"
        until  = until  or "—"
        text_lines.append(f"• {mention} — до {until}\n  Причина: {reason}")
        buttons.append([InlineKeyboardButton(text=f"Разбанить {tg_id2}",
                                             callback_data=f"unban_{tg_id2}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("\n".join(text_lines), parse_mode="HTML", reply_markup=kb)

@router.message(Command("unban"))
async def cmd_unban(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет доступа.")
    parts = msg.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer("Формат: /unban <tg_id>")
    try:
        tg_id = int(parts[1])
    except ValueError:
        return await msg.answer("tg_id должен быть числом.")
    unban_user(tg_id)
    await msg.answer("✅ Разбанено.")



@router.message(Command("ban"))
async def cmd_ban(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет прав администратора.")

    text = (msg.text or "").strip()
    parts = text.split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""

    target_id = None
    days = 7
    reason = "Бан по команде /ban"

    # 1) Если это reply — берём пользователя из ответа
    if msg.reply_to_message:
        target_id = msg.reply_to_message.from_user.id
        if args:
            a = args.split()
            if a and a[0].isdigit():
                days = int(a[0]); reason = " ".join(a[1:]) or reason
            else:
                reason = args or reason

    # 2) Иначе парсим аргументы: @username / tg_id [дней] [причина]
    else:
        if not args:
            return await msg.answer("Формат: /ban @username [дней] [причина]\nЛибо ответом: /ban [дней] [причина]")
        a = args.split()
        first = a[0]

        # @username
        if first.startswith("@"):
            target_id = tg_id_by_username(first)
            if not target_id:
                return await msg.answer("❗ Не нашёл такого username среди пользователей бота.")
            a = a[1:]

        # tg_id
        elif first.lstrip("-").isdigit():
            target_id = int(first)
            a = a[1:]

        else:
            return await msg.answer("Формат: /ban @username [дней] [причина]")

        if a and a[0].isdigit():
            days = int(a[0]); a = a[1:]
        if a:
            reason = " ".join(a)

    # Финальный бан
    ban_user(int(target_id), reason=reason, days=days)
    await msg.answer(f"🚫 Забанен: <code>{target_id}</code> на {days} дн.\nПричина: {reason}", parse_mode="HTML")
