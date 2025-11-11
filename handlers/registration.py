from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from database import add_user, get_user, save_user, is_banned, ban_user
from database import register_failed_attempt, reset_failed_attempts
from keyboards import main_menu, start_menu
from aiogram.types import ReplyKeyboardRemove
from aiogram.filters import CommandStart
import re


router = Router()

# --- состояния ---
class RegForm(StatesGroup):
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
    """Корректный номер комнаты (от 100 до 555)."""
    return re.fullmatch(r"\d{3}", room) and 100 <= int(room) <= 555

@router.message(CommandStart())
async def start_cmd(msg: types.Message, state: FSMContext):
    await state.clear()
    tg_id = msg.from_user.id

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

    user = get_user(msg.from_user.id)
    if user:
        text = (
            "👋 <b>С возвращением!</b>\n\n"
            "Вы уже зарегистрированы.\n"
            "Выберите действие из меню ниже 👇"
        )
        await msg.answer(text, reply_markup=main_menu, parse_mode="HTML")
    else:
        welcome_text = (
            "Чтобы начать, нажмите кнопку ниже 👇\n\n"
            "<i>*Конфиденциальность: фамилия и номер комнаты нужны только для записи.\n"
            "Данные могут быть изменены через /edit.</i>"
        )
        await msg.answer(welcome_text, reply_markup=start_menu, parse_mode="HTML")

@router.message(F.text == "🧺 Начать запись")
async def start_registration(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

    user = get_user(tg_id)
    if user:
        return await msg.answer("Вы уже зарегистрированы! Используйте меню ниже.", reply_markup=main_menu)
    await msg.answer("Введите вашу фамилию для регистрации:")
    await state.set_state(RegForm.surname)


@router.message(RegForm.surname)
async def reg_surname(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    surname = msg.text.strip()

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

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


@router.message(RegForm.room)
async def reg_room(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id
    room = msg.text.strip()

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

    if not is_valid_room(room):
        return await msg.answer("❌ Неверный номер комнаты.")

    data = await state.get_data()
    surname = data["surname"]

    save_user(tg_id, surname, room)
    # add_user(msg.from_user.id, surname, room)
    #await msg.answer(f"Регистрация завершена!\nФамилия: {surname}\nКомната: {room}\n\nТеперь введите /book для записи.")
    await msg.answer(
        f"✅ Регистрация завершена!\nФамилия: {surname}\nКомната: {room}\n\n"
        f"Теперь вы можете записаться на прачечную:",
        reply_markup=main_menu
    )
    await state.clear()

@router.message(F.text == "/edit")
async def edit_profile(msg: types.Message, state: FSMContext):
    tg_id = msg.from_user.id

    if is_banned(tg_id):
        return await msg.answer("🚫 Вы заблокированы на 7 дней за нарушение правил. Попробуйте позже.")

    user = get_user(tg_id)
    if not user:
        return await msg.answer("Вы ещё не зарегистрированы. Используйте /start для регистрации.")

    await msg.answer("Введите новую фамилию:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegForm.surname)

@router.message(RegForm.surname)
async def edit_surname(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    if "editing" in data:
        room = data["room"]
    else:
        room = None

    surname = msg.text.strip()
    await state.update_data(surname=surname)
    await msg.answer("Теперь введите номер комнаты:")
    await state.set_state(RegForm.room)

@router.message(RegForm.room)
async def edit_room(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    surname = data["surname"]
    room = msg.text.strip()
    #add_user(msg.from_user.id, surname, room)
    save_user(msg.from_user.id, surname, room)

    await msg.answer(f"✅ Данные обновлены!\nФамилия: {surname}\nКомната: {room}")
    await state.clear()
