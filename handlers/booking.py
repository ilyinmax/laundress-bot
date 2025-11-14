from aiogram import Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.exceptions import TelegramBadRequest

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from config import TIMEZONE, WORKING_HOURS
from keyboards import main_menu
from scheduler import schedule_reminder
from database import (
    is_banned,
    get_conn,
    get_user,
    get_user_bookings_today,
    get_free_hours,
    create_booking,
)
from sqlite3 import IntegrityError  # для SQLite

# Для Postgres: корректно подхватить UniqueViolation, а без psycopg2 — сделать безопасную заглушку-класс
try:
    from psycopg2.errors import UniqueViolation  # type: ignore
except Exception:
    class UniqueViolation(Exception):
        pass


TZ = ZoneInfo(TIMEZONE)


def now_local() -> datetime:
    return datetime.now(TZ)


router = Router()


# -------- утилиты интерфейса --------
def _norm_kb(kb: InlineKeyboardMarkup | None):
    if not kb:
        return None
    rows = []
    for row in kb.inline_keyboard:
        rows.append(
            tuple(
                (btn.text, getattr(btn, "callback_data", None), getattr(btn, "url", None))
                for btn in row
            )
        )
    return tuple(rows)


async def safe_edit(
    msg: Message,
    *,
    text: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
):
    if msg is None:
        return None

    cur_text = (msg.text or msg.caption or "")
    cur_kb = _norm_kb(getattr(msg, "reply_markup", None))
    new_kb = _norm_kb(reply_markup)

    try:
        if text is not None and text != cur_text:
            return await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        if new_kb is not None and new_kb != cur_kb:
            return await msg.edit_reply_markup(reply_markup=reply_markup)
        return None
    except TelegramBadRequest as e:
        s = str(e).lower()
        if "message is not modified" in s or "message to edit not found" in s:
            return None
        raise


# -------- вспомогательные подсчёты свободных --------
def _free_per_type_for_date(date_iso: str) -> tuple[int, int]:
    """
    Считает КОЛИЧЕСТВО СВОБОДНЫХ ЧАСОВ по типам машин на указанную дату.
    Для 'сегодня' учитываем только будущие часы.
    Возвращает: (free_wash_slots, free_dry_slots)
    """
    now = now_local()
    today_iso = now.date().isoformat()

    with get_conn() as conn:
        cur = conn.execute("SELECT id, type FROM machines")
        machines = cur.fetchall()

    free_wash_slots = 0
    free_dry_slots = 0
    for mid, mtype in machines:
        free = get_free_hours(mid, date_iso)
        if date_iso == today_iso:
            free = [h for h in free if h > now.hour]  # только будущие часы
        cnt = len(free)
        if cnt > 0:
            if mtype == "wash":
                free_wash_slots += cnt
            else:
                free_dry_slots += cnt

    return free_wash_slots, free_dry_slots


def _free_hours_for_machine_on_date(machine_id: int, date_iso: str) -> list[int]:
    """Список СВОБОДНЫХ часов по машине на дату (для 'сегодня' — только будущие)."""
    free = get_free_hours(machine_id, date_iso)
    now = now_local()
    if date_iso == now.date().isoformat():
        free = [h for h in free if h > now.hour]
    return sorted(free)


# =========================================================
#        /book → Дата → Машина (все типы) → Время
# =========================================================
# --- /book: выбор даты ---
@router.message(F.text == "/book")
async def choose_date_first(
    msg: types.Message, user_id: int | None = None, edit: bool = False
):
    uid = user_id or (msg.chat.id if getattr(msg, "chat", None) else msg.from_user.id)
    if is_banned(uid):
        # подтянем срок/причину, чтобы красиво показать
        with get_conn() as conn:
            row = conn.execute(
                "SELECT banned_until, reason FROM banned WHERE tg_id=?", (uid,)
            ).fetchone()
        until_txt = ""
        if row and row[0]:
            try:
                from datetime import datetime as _dt

                until_txt = _dt.fromisoformat(row[0]).strftime("%d.%m %H:%M")
            except Exception:
                until_txt = row[0]
        reason = (row[1] or "").strip() if row else ""
        text = "🚫 Вы заблокированы."
        if until_txt:
            text += f" До {until_txt}."
        if reason:
            text += f"\nПричина: {reason}"
        return await msg.answer(text)
    user = get_user(uid)
    if not user or not (user[2] and user[3]):
        return await msg.answer(
            "Сначала завершите регистрацию: /start → фамилия и номер комнаты."
        )

    now = now_local()
    today = now.date()
    start_offset = 1 if now.hour >= 23 else 0  # после 23:00 «сегодня» скрываем

    days_buttons = []
    for i in range(start_offset, start_offset + 3):
        d = today + timedelta(days=i)
        d_iso = d.isoformat()
        free_wash, free_dry = _free_per_type_for_date(d_iso)
        caption = f"📅 {d.strftime('%d.%m')} — 🧺 {free_wash} / 🌬️ {free_dry}"
        days_buttons.append(
            [InlineKeyboardButton(text=caption, callback_data=f"date_{d_iso}")]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=days_buttons)
    text = "Выберите дату:"
    if edit:
        try:
            await msg.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await msg.edit_reply_markup(reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


async def _show_machines_for_date(message: Message, date: str):
    """Текст + кнопки по всем машинам на выбранную дату."""
    with get_conn() as conn:
        cur = conn.execute("SELECT id, type, name FROM machines ORDER BY type, id")
        machines = cur.fetchall()  # (id, 'wash'|'dry', name)

    if not machines:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К датам", callback_data="back_to_dates")]
            ]
        )
        return await safe_edit(
            message,
            text="Машины ещё не добавлены администратором.",
            reply_markup=kb,
        )

    lines: list[str] = []
    # красиво форматируем дату
    try:
        d_obj = datetime.fromisoformat(date).date()
        header_date = d_obj.strftime("%d.%m.%Y")
    except Exception:
        header_date = date
    lines.append(f"📅 {header_date} — свободные слоты\n")

    rows_btn: list[list[InlineKeyboardButton]] = []

    any_free = False
    for machine_id, machine_type, machine_name in machines:
        free_hours = _free_hours_for_machine_on_date(machine_id, date)
        if not free_hours:
            continue
        any_free = True
        emoji = "🧺" if machine_type == "wash" else "🌬️"
        lines.append(f"{emoji} {machine_name}")
        hours_str = ", ".join(f"{h:02d}" for h in free_hours)
        lines.append(f"   {hours_str}\n")

        # кнопка выбора этой машины
        rows_btn.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {machine_name}",
                    callback_data=f"machine_{machine_id}_{date}",
                )
            ]
        )

    if not any_free:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К датам", callback_data="back_to_dates")]
            ]
        )
        return await safe_edit(
            message,
            text=f"На {date} свободных машин нет.",
            reply_markup=kb,
        )

    # навигация «назад к датам»
    rows_btn.append(
        [InlineKeyboardButton(text="⬅️ К датам", callback_data="back_to_dates")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows_btn)
    text = "\n".join(lines).rstrip()
    await safe_edit(message, text=text, reply_markup=kb)


# Выбрали дату → показываем ВСЕ машины (wash+dry) и список свободных слотов
@router.callback_query(F.data.startswith("date_"))
async def choose_machine_for_date(callback: types.CallbackQuery):
    await callback.answer()
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    date = callback.data.split("_", 1)[1]
    await _show_machines_for_date(callback.message, date)


# Выбрали машину → выбираем ВРЕМЯ
@router.callback_query(F.data.startswith("machine_"))
async def choose_hour(callback: types.CallbackQuery):
    await callback.answer()
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # формат: machine_{machine_id}_{YYYY-MM-DD}
    try:
        _, machine_id_str, date = callback.data.split("_", 2)
        machine_id = int(machine_id_str)
    except Exception:
        return await safe_edit(callback.message, text="⚠️ Неверные данные запроса.")

    with get_conn() as conn:
        cur = conn.execute(
            "SELECT type, name FROM machines WHERE id=?", (machine_id,)
        )
        row = cur.fetchone()
    if not row:
        return await safe_edit(callback.message, text="Ошибка: машина не найдена.")
    machine_type, machine_name = row

    free_hours = set(get_free_hours(machine_id, date))
    all_hours = WORKING_HOURS

    now = now_local()
    selected_date = datetime.fromisoformat(date).date()

    kb_rows = []
    has_free = False
    for h in all_hours:
        slot_dt = datetime.combine(selected_date, time(hour=h, tzinfo=TZ))
        if slot_dt <= now:
            continue  # скрываем прошедшие часы

        if h in free_hours:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🟢 {h:02d}:00",
                        callback_data=f"book_{machine_id}_{date}_{h}",
                    )
                ]
            )
            has_free = True
        else:
            kb_rows.append(
                [InlineKeyboardButton(text=f"🔴 {h:02d}:00", callback_data="busy")]
            )

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К машинам", callback_data=f"back_to_machines_all_{date}"
            ),
            InlineKeyboardButton(text="⬅️ К датам", callback_data="back_to_dates"),
        ]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if not has_free:
        return await safe_edit(
            callback.message,
            text=f"На {date} свободных часов не осталось.",
            reply_markup=kb,
        )

    return await safe_edit(
        callback.message,
        text=f"{'🧺' if machine_type == 'wash' else '🌬️'} <b>{machine_name}</b>\nВыберите время ({date}):",
        reply_markup=kb,
        parse_mode="HTML",
    )


# Защита от клика по занятым слотам
@router.callback_query(F.data == "busy")
async def busy_slot(callback: types.CallbackQuery):
    await callback.answer("Этот слот уже занят ❌", show_alert=True)


# Главное меню
@router.callback_query(F.data == "to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("🏠 Главное меню:", reply_markup=main_menu)


# Подтверждение брони (ограничение: 1 запись на тип в сутки)
@router.callback_query(F.data.startswith("book_"))
async def finalize(callback: types.CallbackQuery):
    await callback.answer()
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    try:
        _, machine_id_str, date_str, hour_str = callback.data.split("_")
        machine_id, hour = int(machine_id_str), int(hour_str)
    except Exception:
        return await safe_edit(
            callback.message, text="Некорректные данные слота. Откройте /book заново."
        )

    user = get_user(callback.from_user.id)
    if is_banned(callback.from_user.id):
        return await safe_edit(
            callback.message, "🚫 Вы заблокированы и не можете записываться."
        )

    if not user or not (user[2] and user[3]):
        return await safe_edit(
            callback.message,
            "Сначала завершите регистрацию: /start → фамилия и комната.",
        )

    try:
        sel_date = datetime.fromisoformat(date_str).date()
    except ValueError:
        return await safe_edit(callback.message, "Некорректная дата слота.")

    now = now_local()
    slot_dt = datetime.combine(sel_date, time(hour=hour, tzinfo=TZ))
    if slot_dt <= now:
        return await safe_edit(
            callback.message, "⏳ Это время уже прошло. Выберите другой слот."
        )

    with get_conn() as conn:
        cur = conn.execute(
            "SELECT type, name FROM machines WHERE id=?", (machine_id,)
        )
        row = cur.fetchone()
        if not row:
            return await safe_edit(msg=callback.message, text="Ошибка: машина не найдена.")
        machine_type, machine_name = row

    if get_user_bookings_today(user[0], date_str, machine_type):
        type_text = "стиральную машину" if machine_type == "wash" else "сушилку"
        return await safe_edit(
            msg=callback.message,
            text=(
                f"⚠️ Вы уже записаны на {type_text} в этот день!\n"
                f"Можно только одну запись на каждый тип машины в сутки."
            ),
        )

    try:
        create_booking(user[0], machine_id, date_str, hour)
    except (IntegrityError, UniqueViolation):
        # проверим, не ваша ли это запись
        with get_conn() as conn:
            mine = conn.execute(
                """
                SELECT 1
                  FROM bookings
                 WHERE user_id = ?
                   AND machine_id = ?
                   AND date =?
                   AND hour =?
                """,
                (user[0], machine_id, date_str, hour),
            ).fetchone()
        if mine:
            return await safe_edit(callback.message, "Вы уже записаны на этот слот.")
        return await safe_edit(
            callback.message,
            text="⚠️ Слот только что заняли. Выберите другое время ⏰",
            parse_mode="HTML",
        )
    except Exception:
        # неожиданные ошибки — аккуратно сообщим
        return await safe_edit(
            callback.message, text="Произошла ошибка сервера. Попробуйте ещё раз."
        )

    icon = "🧺" if machine_type == "wash" else "🌬️"
    await safe_edit(
        msg=callback.message,
        text=(
            f"✅ Запись подтверждена!\n\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {hour:02d}:00\n"
            f"{icon} {machine_name}\n\n"
            f"Для отмены используйте /cancel"
        ),
        parse_mode="HTML",
    )

    # напоминание за 30 минут
    try:
        if slot_dt - timedelta(minutes=30) > now:
            await schedule_reminder(
                callback.from_user.id,
                machine_name,
                date_str,
                hour,
                minutes_before=30,
            )
    except Exception:
        pass

    # --- авто-предложение сушки ---
    if machine_type == "wash":
        next_hour = hour + 1
        if next_hour <= max(WORKING_HOURS):
            # если ещё нет сушки в этот день
            if not get_user_bookings_today(user[0], date_str, "dry"):
                with get_conn() as conn:
                    cur = conn.execute(
                        "SELECT id, name FROM machines WHERE type='dry' ORDER BY id"
                    )
                    dryers = cur.fetchall()
                for dry_id, dry_name in dryers:
                    free = get_free_hours(dry_id, date_str)
                    if next_hour in free:
                        text = (
                            "🌬️ Нужна сушка после стирки?\n\n"
                            f"Могу сразу записать вас на <b>{dry_name}</b>\n"
                            f"{date_str} в {next_hour:02d}:00."
                        )
                        kb = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="✅ Да, добавить сушку",
                                        callback_data=f"auto_dry_{dry_id}_{date_str}_{next_hour}",
                                    ),
                                    InlineKeyboardButton(
                                        text="🙅‍♂️ Нет, спасибо",
                                        callback_data="auto_dry_cancel",
                                    ),
                                ]
                            ]
                        )
                        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
                        break


# авто-создание сушки после стирки
@router.callback_query(F.data.startswith("auto_dry_"))
async def auto_add_dryer(callback: types.CallbackQuery):
    await callback.answer()
    try:
        _, dry_id_str, date_str, hour_str = callback.data.split("_", 3)
        dry_id = int(dry_id_str)
        hour = int(hour_str)
    except Exception:
        return await safe_edit(callback.message, text="Некорректные данные сушки.")

    user = get_user(callback.from_user.id)
    if not user:
        return await safe_edit(
            callback.message, text="Сначала завершите регистрацию: /start."
        )
    if is_banned(callback.from_user.id):
        return await safe_edit(
            callback.message, text="🚫 Вы заблокированы и не можете записываться."
        )

    # проверим, что машина — сушилка и слот ещё свободен
    with get_conn() as conn:
        row = conn.execute(
            "SELECT type, name FROM machines WHERE id=?", (dry_id,)
        ).fetchone()
    if not row:
        return await safe_edit(callback.message, text="Машина не найдена.")
    m_type, m_name = row
    if m_type != "dry":
        return await safe_edit(callback.message, text="Этот слот не для сушки.")

    # уже есть сушка в этот день?
    if get_user_bookings_today(user[0], date_str, "dry"):
        return await safe_edit(
            callback.message,
            text="У вас уже есть запись на сушку в этот день.",
        )

    free = get_free_hours(dry_id, date_str)
    if hour not in free:
        return await safe_edit(
            callback.message,
            text="К сожалению, этот слот сушки уже заняли. Выберите другой вручную через /book.",
        )

    try:
        create_booking(user[0], dry_id, date_str, hour)
    except (IntegrityError, UniqueViolation):
        return await safe_edit(
            callback.message,
            text="К сожалению, этот слот сушки уже заняли. Выберите другой вручную через /book.",
        )
    except Exception:
        return await safe_edit(
            callback.message, text="Произошла ошибка при добавлении сушки."
        )

    # текст подтверждения
    await safe_edit(
        callback.message,
        text=(
            "✅ Добавлена запись на сушку!\n\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {hour:02d}:00\n"
            f"🌬️ {m_name}"
        ),
        parse_mode="HTML",
    )

    # напоминание за 30 минут для сушки (scheduler сам решит, слать ли, если идёт сразу после стирки)
    try:
        sel_date = datetime.fromisoformat(date_str).date()
        slot_dt = datetime.combine(sel_date, time(hour=hour, tzinfo=TZ))
        now = now_local()
        if slot_dt - timedelta(minutes=30) > now:
            await schedule_reminder(
                callback.from_user.id,
                m_name,
                date_str,
                hour,
                minutes_before=30,
            )
    except Exception:
        pass


@router.callback_query(F.data == "auto_dry_cancel")
async def auto_dry_cancel(callback: types.CallbackQuery):
    await callback.answer("Ок, без сушки 👍")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# -----------------------------------------
# Просмотр и отмена записей
# -----------------------------------------
# --- Отмена: показываем только будущие записи ---
@router.message(F.text == "/cancel")
async def show_user_bookings(msg: types.Message):
    user = get_user(msg.from_user.id)
    if not user:
        return await msg.answer("Сначала пройдите регистрацию с помощью /start")

    now = now_local()
    today = now.date().isoformat()
    cur_hour = now.hour

    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT b.id, m.name, b.date, b.hour
              FROM bookings b
              JOIN machines m ON b.machine_id = m.id
             WHERE b.user_id = ?
               AND ((b.date > ?) OR (b.date = ? AND b.hour >= ?))
             ORDER BY b.date, b.hour
        """,
            (user[0], today, today, cur_hour),
        )
        bookings = cur.fetchall()

    if not bookings:
        return await msg.answer("У вас нет активных записей.")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{m} {d} {h}:00", callback_data=f"cancel_{bid}")]
            for bid, m, d, h in bookings
        ]
    )
    await msg.answer("Выберите запись для отмены:", reply_markup=kb)


@router.callback_query(F.data.startswith("cancel_"))
async def cancel_booking(callback: types.CallbackQuery):
    await callback.answer()  # ← быстрый ACK
    booking_id = int(callback.data.split("_")[1])
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    await safe_edit(msg=callback.message, text="🗑️ Запись отменена.")


# --- Мои записи: только будущие ---
@router.message(F.text == "/mybookings")
async def show_future_bookings(msg: types.Message):
    user = get_user(msg.from_user.id)
    if not user:
        return await msg.answer("Сначала пройдите регистрацию с помощью /start")

    now = now_local()
    today = now.date().isoformat()
    cur_hour = now.hour

    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT m.name, b.date, b.hour
              FROM bookings b
              JOIN machines m ON b.machine_id = m.id
             WHERE b.user_id = ?
               AND ((b.date > ?) OR (b.date = ? AND b.hour >= ?))
             ORDER BY b.date, b.hour
        """,
            (user[0], today, today, cur_hour),
        )
        rows = cur.fetchall()

    if not rows:
        return await msg.answer("У вас нет активных записей.")

    text = "🧺 <b>Ваши записи:</b>\n\n"
    for name, date_val, hour in rows:
        # date_val может быть и str (SQLite), и date (Postgres)
        ds = (
            date_val.strftime("%d.%m.%Y")
            if hasattr(date_val, "strftime")
            else datetime.fromisoformat(str(date_val)).strftime("%d.%m.%Y")
        )
        text += f"📅 {ds} — {hour:02d}:00\n• {name}\n\n"
    await msg.answer(text, parse_mode="HTML")


# Кнопки из главного меню
@router.message(F.text == "🧺 Записаться")
async def btn_book(msg: types.Message):
    await choose_date_first(msg)


@router.message(F.text == "📋 Мои записи")
async def btn_mybookings(msg: types.Message):
    await show_future_bookings(msg)


@router.message(F.text == "❌ Отменить запись")
async def btn_cancel(msg: types.Message):
    await show_user_bookings(msg)


# Помощь
@router.message(F.text == "ℹ️ Помощь")
async def show_help(msg: types.Message):
    help_text = (
        "ℹ️ <b>Помощь по использованию бота</b>\n\n"
        "🧺 <b>Запись</b> – выберите дату → машину → время.\n"
        "📋 <b>Мои записи</b> – покажет все ваши активные записи.\n"
        "❌ <b>Отменить запись</b> – удалит вашу текущую бронь.\n\n"
        "⏰ Запись доступна с 9:00 до 23:00, не более одного слота в день.\n"
        "📅 Можно записаться максимум на 2 дня вперёд (сегодня, завтра, послезавтра).\n\n"
        "Если есть вопросы и предложения – пишите @ilyinmax."
    )
    await msg.answer(help_text, parse_mode="HTML")


@router.message(F.text == "/help")
async def cmd_help(msg: types.Message):
    await show_help(msg)


@router.callback_query(F.data == "none")
async def inactive_day(callback: types.CallbackQuery):
    await callback.answer("⚠️ В этот день все слоты заняты.", show_alert=True)


# -------- Навигация «Назад» --------
@router.callback_query(F.data == "back_to_dates")
async def back_to_dates(callback: types.CallbackQuery):
    await callback.answer()
    await choose_date_first(callback.message, user_id=callback.from_user.id, edit=True)


@router.callback_query(F.data.startswith("back_to_machines_all_"))
async def back_to_machines_all(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_", 4)
    if len(parts) != 5 or not parts[4]:
        return await safe_edit(callback.message, text="⚠️ Неверные данные навигации.")
    date = parts[4]
    await _show_machines_for_date(callback.message, date)
