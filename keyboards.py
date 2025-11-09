from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# Клавиатура для не зарегистрированных
start_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧺 Начать запись")]
    ],
    resize_keyboard=True
)

# Главное меню (для зарегистрированных)
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧺 Записаться")],
        [KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="❌ Отменить запись")],
        [KeyboardButton(text="ℹ️ Помощь")]
    ],
    resize_keyboard=True
)

