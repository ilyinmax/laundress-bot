from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import TIMEZONE
from keyboards import build_main_menu
from waitlist_service import get_active_request_for_tg
from exchange_service import (
    ExchangeError,
    ExchangeConflict,
    ExchangeUnavailable,
    accept_proposal,
    available_listings,
    cancel_listing,
    cancel_proposal,
    create_listing,
    create_proposal,
    decline_proposal,
    future_wash_bookings_for_tg,
    get_listing_view,
    has_future_wash_for_tg,
    listing_for_booking,
    proposal_details,
)

TZ = ZoneInfo(TIMEZONE)
router = Router()

MONTHS = (
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _main_kb(tg_id: int):
    return build_main_menu(
        bool(get_active_request_for_tg(int(tg_id))),
        has_future_wash_for_tg(int(tg_id)),
    )


def _date_short(date_iso: str) -> str:
    d = datetime.fromisoformat(str(date_iso)).date()
    return f"{WEEKDAYS_SHORT[d.weekday()]}, {d.day} {MONTHS[d.month]}"


def _slot_text(slot) -> str:
    return (
        f"📅 {_date_short(slot.date)}\n"
        f"🕐 {int(slot.hour):02d}:00\n"
        f"🧺 {slot.machine_name}"
    )


def _listing_text(view) -> str:
    return (
        f"📅 {_date_short(view.date)}\n"
        f"🕐 {int(view.hour):02d}:00\n"
        f"🧺 {view.machine_name}"
    )


def _home_markup(booking_id: int, listing_id: int | None, locked: bool = False):
    rows = []
    if listing_id:
        if not locked:
            rows.append([
                InlineKeyboardButton(
                    text="🔎 Смотреть обмены",
                    callback_data=f"ex_browse_{listing_id}_0",
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text="🛑 Снять с обмена",
                callback_data=f"ex_unlist_{listing_id}",
            )
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                text="📢 Выставить на обмен",
                callback_data=f"ex_list_{booking_id}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_exchange_home(message: types.Message, tg_id: int, booking_id: int | None = None):
    bookings = future_wash_bookings_for_tg(int(tg_id))
    if not bookings:
        return await message.answer(
            "🔄 Для обмена нужна предстоящая запись на стирку.\n\n"
            "Чтобы получить новое место, используйте лист ожидания.",
            reply_markup=_main_kb(int(tg_id)),
        )

    if booking_id is None and len(bookings) > 1:
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{_date_short(slot.date)} · {slot.hour:02d}:00 · {slot.machine_name}",
                    callback_data=f"ex_home_{slot.booking_id}",
                )
            ]
            for slot in bookings
        ]
        return await message.answer(
            "🔄 <b>Обмен стирками</b>\n\n"
            "Выберите запись, которую хотите обменять:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    slot = next(
        (x for x in bookings if booking_id is not None and x.booking_id == int(booking_id)),
        bookings[0] if booking_id is None else None,
    )
    if not slot:
        return await message.answer(
            "Эта запись уже недоступна.",
            reply_markup=_main_kb(int(tg_id)),
        )

    listing = listing_for_booking(slot.booking_id)
    listing_id = int(listing[0]) if listing else None
    status = str(listing[1]) if listing else None

    if listing_id:
        status_text = (
            "⏳ По этой записи сейчас рассматривается предложение обмена."
            if status == "locked"
            else "🟢 Запись выставлена на обмен."
        )
        text = (
            "🔄 <b>Обмен стирками</b>\n\n"
            "Ваша запись:\n"
            f"{_slot_text(slot)}\n\n"
            f"{status_text}\n"
            "Пока обмен не состоялся, исходная запись остаётся за вами."
        )
    else:
        text = (
            "🔄 <b>Обмен стирками</b>\n\n"
            "Ваша запись:\n"
            f"{_slot_text(slot)}\n\n"
            "Можно выставить её на обмен. Пока второй пользователь не согласится, "
            "эта запись остаётся полностью за вами."
        )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=_home_markup(slot.booking_id, listing_id, locked=status == "locked"),
    )


@router.message(F.text == "🔄 Обмен стирками")
async def exchange_start(msg: types.Message):
    await _show_exchange_home(msg, msg.from_user.id)


@router.callback_query(F.data.startswith("ex_home_"))
async def exchange_home_callback(cb: types.CallbackQuery):
    try:
        booking_id = int(cb.data.removeprefix("ex_home_"))
    except Exception:
        return await cb.answer("Некорректная запись.", show_alert=True)
    await cb.answer()
    await _show_exchange_home(cb.message, cb.from_user.id, booking_id)


@router.callback_query(F.data.startswith("ex_list_"))
async def exchange_list(cb: types.CallbackQuery):
    try:
        booking_id = int(cb.data.removeprefix("ex_list_"))
        listing_id = create_listing(cb.from_user.id, booking_id)
    except ExchangeError as exc:
        return await cb.answer(str(exc), show_alert=True)
    except Exception:
        return await cb.answer("Не удалось выставить запись на обмен.", show_alert=True)

    await cb.answer("Запись выставлена на обмен ✅")
    view = get_listing_view(listing_id)
    if not view:
        return
    await cb.message.edit_text(
        "✅ <b>Запись выставлена на обмен</b>\n\n"
        f"{_listing_text(view)}\n\n"
        "Пока обмен не состоялся, запись остаётся за вами.",
        parse_mode="HTML",
        reply_markup=_home_markup(view.booking_id, listing_id),
    )


@router.callback_query(F.data.startswith("ex_unlist_"))
async def exchange_unlist(cb: types.CallbackQuery):
    try:
        listing_id = int(cb.data.removeprefix("ex_unlist_"))
    except Exception:
        return await cb.answer("Некорректное объявление.", show_alert=True)
    ok = await cancel_listing(cb.from_user.id, listing_id)
    if not ok:
        return await cb.answer("Объявление уже недоступно.", show_alert=True)
    await cb.answer("Объявление снято ✅")
    await cb.message.edit_text(
        "🛑 Объявление снято. Ваша запись осталась без изменений."
    )


async def _render_catalog(
    message: types.Message,
    tg_id: int,
    source_listing_id: int,
    page: int,
    *,
    edit: bool,
):
    try:
        rows, pages = available_listings(
            int(tg_id),
            int(source_listing_id),
            page=int(page),
        )
    except ExchangeError as exc:
        if edit:
            return await message.edit_text(str(exc))
        return await message.answer(str(exc))

    page = max(0, min(int(page), pages - 1))
    if not rows:
        text = (
            "🔎 <b>Доступные обмены</b>\n\n"
            "Сейчас никто другой не выставил подходящую запись. "
            "Ваше объявление останется активным."
        )
        markup = None
    else:
        text = (
            "🔎 <b>Доступные обмены</b>\n\n"
            "⭐ — время соответствует вашей подписке.\n"
            "Можно выбрать и другое время вручную."
        )
        buttons = []
        for item in rows:
            star = "⭐ " if item.suitable else ""
            buttons.append([
                InlineKeyboardButton(
                    text=(
                        f"{star}{_date_short(item.date)} · "
                        f"{item.hour:02d}:00 · {item.machine_name}"
                    ),
                    callback_data=(
                        f"ex_pick_{int(source_listing_id)}_"
                        f"{item.listing_id}_{page}"
                    ),
                )
            ])
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=f"ex_browse_{source_listing_id}_{page-1}",
                )
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page+1}/{pages}",
                callback_data="ex_noop",
            )
        )
        if page + 1 < pages:
            nav.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=f"ex_browse_{source_listing_id}_{page+1}",
                )
            )
        if nav:
            buttons.append(nav)
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("ex_browse_"))
async def exchange_browse(cb: types.CallbackQuery):
    try:
        raw = cb.data.removeprefix("ex_browse_")
        listing_id_s, page_s = raw.rsplit("_", 1)
        listing_id, page = int(listing_id_s), int(page_s)
    except Exception:
        return await cb.answer("Некорректная страница.", show_alert=True)
    await cb.answer()
    await _render_catalog(cb.message, cb.from_user.id, listing_id, page, edit=True)


@router.callback_query(F.data == "ex_noop")
async def exchange_noop(cb: types.CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("ex_pick_"))
async def exchange_pick(cb: types.CallbackQuery):
    try:
        raw = cb.data.removeprefix("ex_pick_")
        source_s, target_s, page_s = raw.split("_")
        source_id, target_id, page = int(source_s), int(target_s), int(page_s)
    except Exception:
        return await cb.answer("Некорректный обмен.", show_alert=True)

    source = get_listing_view(source_id)
    target = get_listing_view(target_id)
    if not source or not target or source.status != "active" or target.status != "active":
        return await cb.answer("Этот вариант уже недоступен.", show_alert=True)

    await cb.answer()
    await cb.message.edit_text(
        "🔄 <b>Предложить обмен?</b>\n\n"
        "<b>Вы отдаёте:</b>\n"
        f"{_listing_text(source)}\n\n"
        "<b>Вы получите:</b>\n"
        f"{_listing_text(target)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Предложить обмен",
                        callback_data=f"ex_offer_{source_id}_{target_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"ex_browse_{source_id}_{page}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("ex_offer_"))
async def exchange_offer(cb: types.CallbackQuery):
    try:
        raw = cb.data.removeprefix("ex_offer_")
        source_s, target_s = raw.split("_")
        result = create_proposal(cb.from_user.id, int(source_s), int(target_s))
    except ExchangeError as exc:
        return await cb.answer(str(exc), show_alert=True)
    except Exception:
        return await cb.answer("Не удалось отправить предложение.", show_alert=True)

    await cb.answer("Предложение отправлено ✅")
    await cb.message.edit_text(
        "✅ <b>Предложение отправлено</b>\n\n"
        "Вы предлагаете обмен:\n"
        f"{_slot_text(result.from_slot)}\n\n"
        "на:\n"
        f"{_slot_text(result.to_slot)}\n\n"
        f"Ответ ожидается до {result.expires_at.strftime('%H:%M')}. "
        "Обе исходные записи пока остаются у владельцев.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="❌ Отменить предложение",
                    callback_data=f"ex_cancelprop_{result.proposal_id}",
                )
            ]]
        ),
    )

    try:
        await cb.bot.send_message(
            result.to_slot.tg_id,
            "🔄 <b>Вам предложили обмен стирками</b>\n\n"
            "<b>Ваша запись:</b>\n"
            f"{_slot_text(result.to_slot)}\n\n"
            "<b>Вам предлагают:</b>\n"
            f"{_slot_text(result.from_slot)}\n\n"
            f"Предложение действует до {result.expires_at.strftime('%H:%M')}. "
            "До вашего решения запись остаётся за вами.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="✅ Обменяться",
                        callback_data=f"ex_accept_{result.proposal_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отказать",
                        callback_data=f"ex_decline_{result.proposal_id}",
                    ),
                ]]
            ),
        )
    except Exception:
        await cancel_proposal(cb.from_user.id, result.proposal_id)
        await cb.message.answer(
            "⚠️ Не удалось доставить предложение второму пользователю. "
            "Предложение отменено, обе записи остались без изменений."
        )


@router.callback_query(F.data.startswith("ex_accept_"))
async def exchange_accept(cb: types.CallbackQuery):
    try:
        proposal_id = int(cb.data.removeprefix("ex_accept_"))
        result = await accept_proposal(cb.from_user.id, proposal_id)
    except ExchangeError as exc:
        return await cb.answer(str(exc), show_alert=True)
    except Exception:
        return await cb.answer("Не удалось выполнить обмен.", show_alert=True)

    await cb.answer("Обмен выполнен ✅")
    await cb.message.edit_text(
        "✅ <b>Обмен состоялся</b>\n\n"
        "Ваша новая запись:\n"
        f"{_slot_text(result.to_slot)}",
        parse_mode="HTML",
    )
    try:
        await cb.bot.send_message(
            result.from_slot.tg_id,
            "✅ <b>Обмен состоялся</b>\n\n"
            "Ваша новая запись:\n"
            f"{_slot_text(result.from_slot)}",
            parse_mode="HTML",
            reply_markup=_main_kb(result.from_slot.tg_id),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("ex_decline_"))
async def exchange_decline(cb: types.CallbackQuery):
    try:
        proposal_id = int(cb.data.removeprefix("ex_decline_"))
    except Exception:
        return await cb.answer("Некорректное предложение.", show_alert=True)
    details = proposal_details(proposal_id)
    if not details:
        return await cb.answer("Предложение уже недоступно.", show_alert=True)
    ok = await decline_proposal(cb.from_user.id, proposal_id)
    if not ok:
        return await cb.answer("Предложение уже недоступно.", show_alert=True)

    await cb.answer("Вы отказались от обмена.")
    await cb.message.edit_text(
        "❌ Обмен отклонён. Ваша запись осталась без изменений."
    )
    try:
        await cb.bot.send_message(
            details.from_slot.tg_id,
            "❌ Пользователь отказался от обмена. Ваша запись осталась без изменений."
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("ex_cancelprop_"))
async def exchange_cancel_proposal(cb: types.CallbackQuery):
    try:
        proposal_id = int(cb.data.removeprefix("ex_cancelprop_"))
    except Exception:
        return await cb.answer("Некорректное предложение.", show_alert=True)
    details = proposal_details(proposal_id)
    if not details:
        return await cb.answer("Предложение уже недоступно.", show_alert=True)
    ok = await cancel_proposal(cb.from_user.id, proposal_id)
    if not ok:
        return await cb.answer("Предложение уже недоступно.", show_alert=True)

    await cb.answer("Предложение отменено.")
    await cb.message.edit_text(
        "❌ Предложение обмена отменено. Ваша запись осталась без изменений."
    )
    try:
        await cb.bot.send_message(
            details.to_slot.tg_id,
            "❌ Предложение обмена было отменено второй стороной. Ваша запись не изменилась."
        )
    except Exception:
        pass
