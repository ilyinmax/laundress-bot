from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def reply_menu(rows: list[list[str]], *, placeholder: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=text) for text in row]
            for row in rows
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


start_menu = reply_menu([["🧺 Начать запись"]])


def build_main_menu(waitlist_active: bool = False) -> ReplyKeyboardMarkup:
    waitlist_text = "🔔 Лист ожидания • активен" if waitlist_active else "🔔 Лист ожидания"
    return reply_menu([
        ["🧺 Записаться", "📋 Мои записи"],
        [waitlist_text, "ℹ️ Помощь"],
    ])


main_menu = build_main_menu(False)


def back_home_menu(*, include_back: bool = True) -> ReplyKeyboardMarkup:
    rows = []
    if include_back:
        rows.append(["⬅️ Назад", "🏠 Главное меню"])
    else:
        rows.append(["🏠 Главное меню"])
    return reply_menu(rows)
