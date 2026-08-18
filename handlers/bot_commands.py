from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import ADMIN_IDS


USER_COMMANDS = [
    BotCommand(command="start", description="Запуск бота и регистрация"),
    BotCommand(command="book", description="Записаться на стирку или сушку"),
    BotCommand(command="mybookings", description="Мои активные записи"),
    BotCommand(command="cancel", description="Отменить запись"),
    BotCommand(command="edit", description="Изменить фамилию и комнату"),
    BotCommand(command="help", description="Помощь"),
]

ADMIN_ONLY_COMMANDS = [
    BotCommand(command="admin", description="Панель администратора"),
    BotCommand(command="early", description="Ранняя запись на любую дату"),
    BotCommand(command="admin_commands", description="Все команды бота"),
    BotCommand(command="export", description="Экспорт записей в Excel"),
    BotCommand(command="import", description="Импорт записей из Excel"),
    BotCommand(command="machines", description="Включить или выключить машины"),
    BotCommand(command="ban", description="Заблокировать пользователя"),
    BotCommand(command="unban", description="Разблокировать пользователя"),
    BotCommand(command="banned", description="Список заблокированных"),
    BotCommand(command="abookfio", description="Ручная запись старым способом"),
    BotCommand(command="notify_incomplete", description="Напомнить заполнить профиль"),
    BotCommand(command="test_reminder", description="Тест напоминания"),
    BotCommand(command="laundry_news", description="Разослать список работающих машин"),
]


def _admin_ids() -> list[int]:
    raw = ADMIN_IDS
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        text = str(raw).strip().strip("[]")
        items = [part.strip().strip("'\"") for part in text.split(",") if part.strip()]

    result: list[int] = []
    for item in items:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


async def setup_bot_commands(bot: Bot) -> None:
    """Обычным пользователям показываем только их команды, админам — полный набор."""
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())

    admin_commands = USER_COMMANDS + ADMIN_ONLY_COMMANDS
    for admin_id in _admin_ids():
        try:
            await bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:
            # Один недоступный chat_id не должен мешать запуску бота.
            print(f"⚠️ Не удалось установить команды для admin {admin_id}: {exc}")
