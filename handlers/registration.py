# registration.py
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardRemove

from database import (
    get_user, save_user, is_banned, ban_user,
    register_failed_attempt, reset_failed_attempts,
    update_username,
)
from keyboards import main_menu, start_menu, build_main_menu

import re

router = Router()

# --- состояния: раздельно для регистрации и редактирования ---
class RegForm(StatesGroup):
    surname = State()
    room = State()

class EditForm(StatesGroup):
    surname = State()
    room = State()

# --- простейший фильтр нецензурных слов ---
BAD_WORDS = {
    "хуй", "пизд", "еба", "сука", "бля", "fuck", "shit", "asshole",
    "cunt", "dick", "idiot", "дурак", "мраз", "твар", "чмо"
}

def is_offensive(text: str) -> bool:
    text = text.lower().replace("ё", "е")
    return any(bad in text for bad in BAD_WORDS)

def is_valid_room(room: str) -> bool:
    """Корректный номер комнаты (ровно 3 цифры, 100..555)."""
    return bool(re.fullmatch(r"\d{3}", room)) and 100 <= int(room) <= 555

# --- /start ---
@router.message(CommandStart())
async def start_cmd(msg: types.Message, state: FSMContext):
    await state.clear()
    tg_id = msg.from_user.id
    # обновим username (если поменялся в Telegram)
    update_username(tg_id, msg.from_user.username)

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

    user = get_user(tg_id)
    if user and user[2] and user[3]:
        text = ("👋 <b>С возвращением!</b>\n\n"
                "Вы уже зарегистрированы.\n"
                "Выберите действие из меню ниже 👇")
        from waitlist_service import get_active_request_for_tg
        return await msg.answer(
            text,
            reply_markup=build_main_menu(bool(get_active_request_for_tg(tg_id))),
            parse_mode="HTML",
        )

    welcome_text = (
        "Чтобы начать, нажмите кнопку ниже 👇\n\n"
        "<i>*Конфиденциальность: фамилия и номер комнаты нужны только для записи.\n"
        "Данные можно изменить через /edit.</i>"
    )
    return await msg.answer(welcome_text, reply_markup=start_menu, parse_mode="HTML")

# --- запуск регистрации кнопкой ---
@router.message(F.text == "🧺 Начать запись")
async def start_registration(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

    user = get_user(tg_id)
    if user and user[2] and user[3]:
        return await msg.answer("Вы уже зарегистрированы! Используйте меню ниже.", reply_markup=main_menu)

    # на всякий обновим username
    update_username(tg_id, msg.from_user.username)

    await msg.answer("Введите вашу фамилию для регистрации:")
    await state.set_state(RegForm.surname)

# --- регистрация: шаг 1 (фамилия) ---
@router.message(RegForm.surname)
async def reg_surname(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    surname = (msg.text or "").strip()

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")
    if not surname:
        return await msg.answer("Введите фамилию текстом.")

    # проверка на мат
    if is_offensive(surname):
        count = register_failed_attempt(tg_id)
        if count >= 3:
            ban_user(tg_id, reason="3 нецензурные попытки регистрации", days=7)
            return await msg.answer("🚫 Вы заблокированы на 7 дней за неоднократные нарушения при регистрации.")
        return await msg.answer("⚠️ Недопустимая фамилия. Введите корректную фамилию.")

    reset_failed_attempts(tg_id)
    await state.update_data(surname=surname)
    await msg.answer("Введите номер вашей комнаты:")
    await state.set_state(RegForm.room)

# --- регистрация: шаг 2 (комната) ---
@router.message(RegForm.room)
async def reg_room(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    room = (msg.text or "").strip()

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")
    if not is_valid_room(room):
        return await msg.answer("❌ Неверный номер комнаты. Введите три цифры, 100–555.")

    data = await state.get_data()
    surname = data.get("surname", "").strip()

    save_user(tg_id, surname, room)

    await msg.answer(
        f"✅ Регистрация завершена!\nФамилия: {surname}\nКомната: {room}\n\n"
        f"Теперь вы можете записаться на прачечную:",
        reply_markup=main_menu
    )
    await state.clear()

# --- редактирование профиля (/edit) ---
@router.message(F.text == "/edit")
async def edit_profile(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

    user = get_user(tg_id)
    if not user:
        return await msg.answer("Вы ещё не зарегистрированы. Используйте /start для регистрации.")

    await msg.answer("Введите новую фамилию:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(EditForm.surname)

# --- редактирование: фамилия ---
@router.message(EditForm.surname)
async def edit_surname(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    surname = (msg.text or "").strip()

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")
    if not surname:
        return await msg.answer("Введите фамилию текстом.")
    if is_offensive(surname):
        return await msg.answer("⚠️ Недопустимая фамилия. Введите корректную фамилию.")

    await state.update_data(surname=surname)
    await msg.answer("Теперь введите номер комнаты:")
    await state.set_state(EditForm.room)

# --- редактирование: комната ---
@router.message(EditForm.room)
async def edit_room(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    room = (msg.text or "").strip()

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")
    if not is_valid_room(room):
        return await msg.answer("❌ Неверный номер комнаты. Введите три цифры, 100–555.")

    data = await state.get_data()
    surname = data.get("surname", "").strip()

    save_user(tg_id, surname, room)
    from waitlist_service import get_active_request_for_tg
    await msg.answer(
        f"✅ Данные обновлены!\nФамилия: {surname}\nКомната: {room}",
        reply_markup=build_main_menu(bool(get_active_request_for_tg(tg_id))),
    )
    await state.clear()

# --- кнопка из рассылки «Заполнить профиль» ---
@router.callback_query(F.data == "fill_profile")
async def cb_fill_profile(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    user = get_user(callback.from_user.id)
    if user and user[2] and user[3]:
        return await callback.message.answer("Вы уже зарегистрированы ✅\nМожете бронировать из меню.")
    await callback.message.answer("Введите вашу фамилию для регистрации:")
    await state.set_state(RegForm.surname)
