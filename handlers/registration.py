from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from database import add_user, get_user, save_user
from keyboards import main_menu, start_menu
from aiogram.types import ReplyKeyboardRemove
from aiogram.filters import CommandStart


router = Router()

class RegForm(StatesGroup):
    surname = State()
    room = State()

@router.message(CommandStart())
async def start_cmd(msg: types.Message, state: FSMContext):
    await state.clear()
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
    user = get_user(msg.from_user.id)
    if user:
        return await msg.answer("Вы уже зарегистрированы! Используйте меню ниже.", reply_markup=main_menu)
    await msg.answer("Введите вашу фамилию для регистрации:")
    await state.set_state(RegForm.surname)


@router.message(RegForm.surname)
async def reg_surname(msg: types.Message, state: FSMContext):
    await state.update_data(surname=msg.text.strip())
    await msg.answer("Введите номер вашей комнаты:")
    await state.set_state(RegForm.room)

@router.message(RegForm.room)
async def reg_room(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    surname = data["surname"]
    room = msg.text.strip()
    save_user(msg.from_user.id, surname, room)
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
    user = get_user(msg.from_user.id)
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
