# admin.py
import os
import asyncio
from datetime import datetime, timedelta

import pandas as pd
from openpyxl import Workbook
from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# aiogram v3:
from aiogram.exceptions import TelegramRetryAfter
# (если вдруг у тебя aiogram v2, замени строку выше на:
# from aiogram.utils.exceptions import RetryAfter as TelegramRetryAfter)

from database import (
    get_conn, _b64d_try, init_db,
    ensure_user_by_surname_room, get_machine_id_by_name,
    ban_user, unban_user, tg_id_by_username,
    get_user_bookings_today, daily_limit_reached, get_free_hours, get_free_hours_effective, is_admin, get_incomplete_users,
    set_machine_active, get_all_machines,
)
from config import ADMIN_IDS

from zoneinfo import ZoneInfo
from config import TIMEZONE
from aiogram.types import FSInputFile  # для экспорта
from scheduler import schedule_test_message
from booking_service import create_booking_safe, cancel_booking_safe, BookingError

TZ = ZoneInfo(TIMEZONE)

router = Router()


async def _render_schedule(message: types.Message, date: str):
    with get_conn() as conn:
        cur = conn.execute("""
            SELECT b.id, m.name, b.hour, u.surname, u.room, u.tg_id, u.username
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
    for booking_id, machine, hour, surname, room, tg_id, username in records:
        surname = _b64d_try(surname)
        room = _b64d_try(room)
        who = None
        if surname and username:
            who = f"{surname} (@{username})"
        elif surname:
            who = surname
        elif username:
            who = f"@{username}"
        else:
            who = f"id:{tg_id}"
        room_txt = room or "-"

        if machine != current_machine:
            text += f"\n<b>{machine}</b>\n"
            current_machine = machine

        text += f"  ⏰ {hour:02d}:00 - {who} (комн. {room_txt})\n"
        buttons.append([
            InlineKeyboardButton(text=f"❌ Удалить {hour:02d}:00 ({who})",
                                 callback_data=f"admin_del_{booking_id}_{date}"),
            InlineKeyboardButton(text="🚫 Бан",
                                 callback_data=f"admin_ban_{tg_id}_{date}")
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# === Импорт из Excel ===
async def import_bookings_from_xlsx(path: str) -> tuple[int, int, list[str]]:
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
                result = await create_booking_safe(uid, mid, date_iso, hour)
                inserted += 1
                with get_conn() as conn:
                    tg_row = conn.execute("SELECT tg_id FROM users WHERE id=?", (uid,)).fetchone()
                if tg_row and int(tg_row[0]) > 0:
                    from handlers.laundry_features import schedule_reminder
                    await schedule_reminder(int(tg_row[0]), result.machine_name, result.date, result.hour, 30)
            except BookingError:
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
    added, skipped, errors = await import_bookings_from_xlsx(path)

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
    await callback.answer()  # ← ранний ACK
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    today = datetime.now(TZ).date()  # ← локальная дата
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=(today + timedelta(days=i)).strftime("%d.%m.%Y"),
            callback_data=f"admin_day_{(today + timedelta(days=i)).isoformat()}"
        )]
        for i in range(4)
    ])
    await callback.message.edit_text(
        "📅 Выберите день для просмотра расписания:",
        reply_markup=kb
    )



# === Статистика ===
@router.callback_query(F.data == "admin_menu_stats")
async def show_stats(callback: types.CallbackQuery):
    await callback.answer()  # ← ACK
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    today = datetime.now(TZ).date()       # ← TZ
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
        f"📊 <b>Статистика на неделю ({today.strftime('%d.%m')} - {week_end.strftime('%d.%m')})</b>\n\n"
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
    await callback.answer()  # ← ACK
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    parts = callback.data.split("_", 2)
    if len(parts) < 3:
        return await callback.answer("Некорректные данные даты.", show_alert=True)
    date = parts[2]
    await _render_schedule(callback.message, date)


# === Удаление конкретной записи ===
@router.callback_query(F.data.startswith("admin_del_"))
async def delete_booking(callback: types.CallbackQuery):
    await callback.answer()  # ← ACK
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    parts = callback.data.split("_", 3)
    if len(parts) < 4:
        return await callback.answer("Ошибка данных.", show_alert=True)
    _, _, booking_id, date = parts
    try:
        booking_id = int(booking_id)
    except ValueError:
        return await callback.answer("Неверный ID записи.", show_alert=True)

    try:
        old = await cancel_booking_safe(booking_id, require_future=False)
    except Exception:
        return await callback.answer("Запись уже удалена.", show_alert=True)

    from waitlist_service import distribute_date
    await distribute_date(old.date, context="day")
    await _render_schedule(callback.message, date)


# === Бан пользователя ===
@router.callback_query(F.data.startswith("admin_ban_"))
async def admin_ban_user(callback: types.CallbackQuery):
    await callback.answer()  # ← ACK
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    try:
        _, _, tg_id_str, date = callback.data.split("_", 3)
        tg_id = int(tg_id_str)
    except Exception:
        return await callback.answer("Ошибка данных бан-кнопки.", show_alert=True)

    cleanup = ban_user(tg_id, reason="Бан из админ-панели", days=7)
    from waitlist_service import finalize_ban_cleanup
    await finalize_ban_cleanup(cleanup)
    await _render_schedule(callback.message, date)



# === Экспорт записей ===
@router.message(Command("export"))
@router.callback_query(F.data == "admin_menu_export")
async def export_bookings(event: types.Message | types.CallbackQuery):
    if isinstance(event, types.CallbackQuery):
        await event.answer()  # ← ACK
        user_id = event.from_user.id
        msg = event.message
    else:
        user_id = event.from_user.id
        msg = event

    if not is_admin(user_id):
        if isinstance(event, types.CallbackQuery):
            return await event.answer("🚫 Нет доступа.", show_alert=True)
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

    # вместо локального имени - безопаснее в /tmp
    fname = f"/tmp/bookings_{datetime.now(TZ).strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
    wb.save(fname)
    await msg.answer_document(FSInputFile(fname), caption="📊 Экспорт всех записей")
    try:
        os.remove(fname)
    except Exception:
        pass

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
        reason = reason or "-"
        until  = until  or "-"
        text_lines.append(f"• {mention} - до {until}\n  Причина: {reason}")
        buttons.append([InlineKeyboardButton(text=f"Разбанить {tg_id}",
                                             callback_data=f"unban_{tg_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await msg.answer("\n".join(text_lines), parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("unban_"))
async def cb_unban(callback: types.CallbackQuery):
    await callback.answer()  # ← ACK
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

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
        reason = reason or "-"
        until  = until  or "-"
        text_lines.append(f"• {mention} - до {until}\n  Причина: {reason}")
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

    # 1) Если это reply - берём пользователя из ответа
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
    cleanup = ban_user(int(target_id), reason=reason, days=days)
    from waitlist_service import finalize_ban_cleanup
    await finalize_ban_cleanup(cleanup)
    await msg.answer(
        f"🚫 Забанен: <code>{target_id}</code> на {days} дн.\n"
        f"Причина: {reason}\n"
        f"Активных заявок закрыто: {cleanup.get('cancelled_requests', 0)}\n"
        f"Активных HOLD отменено: {len(cleanup.get('holds', []))}",
        parse_mode="HTML",
    )

@router.message(Command("abookfio"))
async def cmd_abookfio(msg: types.Message):
    """
    Формат: /abookfio <Фамилия> <Комната> <machine_id> <YYYY-MM-DD> <HH> [коммент]
    Пример: /abookfio Иванов 412 3 2025-11-14 19 после пары
    """
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет прав администратора.")

    parts = (msg.text or "").strip().split(maxsplit=6)  # до 7 токенов
    if len(parts) < 6:
        return await msg.answer(
            "Формат: /abookfio <Фамилия> <Комната> <machine_id> <YYYY-MM-DD> <HH> [комментарий]"
        )

    _, surname, room, machine_id_s, date_iso, hour_s, *rest = parts
    comment = rest[0] if rest else ""

    # парсинг чисел и базовая валидация
    try:
        machine_id = int(machine_id_s)
        hour = int(hour_s)
        assert 0 <= hour <= 23
        # простая проверка формата даты
        from datetime import datetime
        datetime.fromisoformat(date_iso)
    except Exception:
        return await msg.answer("Проверьте аргументы: machine_id - число, час 0-23, дата - YYYY-MM-DD.")

    # найдём/создадим пользователя по Фамилии и Комнате (вернётся users.id)
    user_id = ensure_user_by_surname_room(surname, room)

    # узнаём тип и имя машины
    with get_conn() as conn:
        row = conn.execute("SELECT type, name FROM machines WHERE id=?", (machine_id,)).fetchone()
    if not row:
        return await msg.answer("Машина не найдена.")
    machine_type, machine_name = row

    # ограничение: 1 запись на тип в сутки
    if daily_limit_reached(user_id, date_iso, machine_type):
        t = "стиралку" if machine_type == "wash" else "сушилку"
        return await msg.answer(f"⚠️ У пользователя уже есть запись на {t} в этот день.")

    # слот свободен?
    free = get_free_hours_effective(machine_id, date_iso)
    if hour not in free:
        return await msg.answer("Этот час уже занят. Выберите другой.")

    # создаём запись через общий сервис
    try:
        result = await create_booking_safe(user_id, machine_id, date_iso, hour)
    except BookingError:
        return await msg.answer("Не удалось создать запись. Возможно, слот уже занят.")

    with get_conn() as conn:
        tg_row = conn.execute("SELECT tg_id FROM users WHERE id=?", (user_id,)).fetchone()
    if tg_row and int(tg_row[0]) > 0:
        from handlers.laundry_features import schedule_reminder
        await schedule_reminder(int(tg_row[0]), result.machine_name, result.date, result.hour, 30)

    # ответ админу
    text = (f"✅ Запись создана:\n"
            f"{machine_name} • {date_iso} {hour:02d}:00\n"
            f"Для: {surname} (комн. {room})")
    if comment:
        text += f"\nКомментарий: {comment}"
    await msg.answer(text)

def _machines_admin_view():
    """
    Текст + клавиатура для управления машинами.
    """
    rows = get_all_machines()
    if not rows:
        return "Машины не настроены.", None

    lines = ["Список машин:\n"]
    kb_rows: list[list[InlineKeyboardButton]] = []

    for mid, t, name, is_active in rows:
        kind = "стиралка" if t == "wash" else "сушилка"
        status = "🟢 работает" if is_active else "🔴 выключена"
        lines.append(f"#{mid} - {name} ({kind}), {status}")

        toggle_text = "⛔️ Выключить" if is_active else "✅ Включить"
        kb_rows.append([
            InlineKeyboardButton(
                text=f"{toggle_text} {name}",
                callback_data=f"admin_mtoggle_{mid}_{0 if is_active else 1}",
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    return "\n".join(lines), kb

@router.message(Command("machines"))
async def cmd_machines(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет прав администратора.")

    text, kb = _machines_admin_view()
    if kb is None:
        return await msg.answer(text)
    await msg.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("admin_mtoggle_"))
async def admin_toggle_machine(callback: types.CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return await callback.answer("🚫 Нет доступа.", show_alert=True)

    try:
        _, _, mid_str, active_str = callback.data.split("_", 3)
        mid = int(mid_str)
        new_active = bool(int(active_str))   # 1 -> включить, 0 -> выключить
    except Exception:
        return await callback.answer("Некорректные данные кнопки.", show_alert=True)

    set_machine_active(mid, new_active)

    if new_active:
        with get_conn() as conn:
            machine_row = conn.execute("SELECT type FROM machines WHERE id=?", (mid,)).fetchone()
        if machine_row and str(machine_row[0]) == "wash":
            from waitlist_service import distribute_date
            today = datetime.now(TZ).date()
            for offset in range(3):
                await distribute_date((today + timedelta(days=offset)).isoformat(), context="day")

    # перерисовываем список
    text, kb = _machines_admin_view()
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        # на всякий случай, если текст не меняется
        await callback.message.edit_reply_markup(reply_markup=kb)


@router.message(Command("notify_incomplete"))
async def notify_incomplete(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("🚫 Нет доступа.")

    users = get_incomplete_users()
    if not users:
        return await message.answer("Все пользователи уже заполнили профиль ✅")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заполнить профиль", callback_data="fill_profile")]
    ])
    text = (
        "Привет! Чтобы твоя запись в прачечную корректно отображалась, "
        "необходимо еще раз завершить регистрацию.\n\n"
        "Нажмите «Заполнить профиль» ниже 👇"
    )

    sent, skipped = 0, 0
    for tg_id, _ in users:
        try:
            await message.bot.send_message(
                tg_id, text,
                reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True, disable_notification=True,
            )
            sent += 1
            await asyncio.sleep(0.05)  # лёгкий троттлинг
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await message.bot.send_message(
                    tg_id, text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True, disable_notification=True,
                )
                sent += 1
            except Exception:
                skipped += 1
        except Exception:
            skipped += 1

    await message.answer(f"Готово. Отправлено: {sent}, не доставлено: {skipped}.")


@router.message(Command("test_reminder"))
async def cmd_test_reminder(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("🚫 Нет доступа.")
    parts = (msg.text or "").split()
    minutes = 1
    if len(parts) > 1:
        try:
            minutes = int(parts[1])
        except ValueError:
            return await msg.answer("Формат: /test_reminder <минуты> (целое число)")

    minutes = max(1, min(minutes, 180))  # от 1 до 180 минут
    await schedule_test_message(
        msg.from_user.id,
        minutes,
        text=f"⏰ Тестовое напоминание: пришло через <b>{minutes}</b> мин. ✅",
    )
    await msg.answer(f"Готово! Пришлю тест через {minutes} мин (бесшумно).")

@router.message(Command("laundry_news"))
async def cmd_laundry_news(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("🚫 Нет доступа.")

    with get_conn() as conn:
        wash = [name for (name,) in conn.execute(
            "SELECT name FROM machines WHERE type='wash' AND is_active ORDER BY name"
        ).fetchall()]
        dry = [name for (name,) in conn.execute(
            "SELECT name FROM machines WHERE type='dry' AND is_active ORDER BY name"
        ).fetchall()]

    def _short(names):
        # превращаем 'Стиральная №3' -> '№3', 'Сушилка №2' -> '№2'
        result = []
        for n in names:
            if "№" in n:
                result.append(n[n.index("№"):].strip())
            else:
                result.append(n)
        return ", ".join(result) if result else "нет"

    text = (
      #  "Отличные новости по прачечной 🎉\n"
        "Рабочие машины:\n"
        f"🧺 стиралки - {_short(wash)}\n"
        f"🌬 сушилки - {_short(dry)}\n"
      #  "Пользуемся и бережём машинки 🙏"
    )

    # берём всех пользователей бота
    with get_conn() as conn:
        rows = conn.execute("SELECT tg_id FROM users").fetchall()

    sent, skipped = 0, 0

    for (tg_id,) in rows:
        try:
            await message.bot.send_message(
                tg_id,
                text,
            )
            sent += 1
            await asyncio.sleep(0.05)        # лёгкий троттлинг
        except TelegramRetryAfter as e:
            # если телега попросила подождать - ждём и пробуем ещё раз
            await asyncio.sleep(e.retry_after + 1)
            try:
                await message.bot.send_message(
                    tg_id,
                    text,
                )
                sent += 1
            except Exception:
                skipped += 1
        except Exception:
            skipped += 1

    await message.answer(
        f"Готово. Сообщение отправлено: {sent}, не доставлено: {skipped}."
    )

