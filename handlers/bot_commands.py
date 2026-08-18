from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import ADMIN_IDS
from handlers.admin_access import (
    is_root_admin,
    sync_dynamic_admins,
)
from handlers import admin_extra as admin_extra_module

# Дополняем экран «📚 Все команды» новыми root-only командами.
if "/addadmin" not in admin_extra_module.ADMIN_COMMANDS_TEXT:
    admin_extra_module.ADMIN_COMMANDS_TEXT += """

<b>Управление администраторами (только постоянные админы):</b>
/addadmin @username — выдать права администратора
/deladmin @username — забрать выданные права
/admins — показать всех администраторов"""


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

ROOT_ONLY_COMMANDS = [
    BotCommand(command="addadmin", description="Добавить администратора по @username"),
    BotCommand(command="deladmin", description="Удалить добавленного администратора"),
    BotCommand(command="admins", description="Список администраторов бота"),
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
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in result:
            result.append(value)
    return result


def _commands_for_admin(admin_id: int) -> list[BotCommand]:
    commands = USER_COMMANDS + ADMIN_ONLY_COMMANDS
    if is_root_admin(admin_id):
        commands += ROOT_ONLY_COMMANDS
    return commands


async def set_admin_commands_for_chat(bot: Bot, admin_id: int) -> None:
    """Сразу показывает нужный slash-список конкретному администратору."""
    await bot.set_my_commands(
        _commands_for_admin(int(admin_id)),
        scope=BotCommandScopeChat(chat_id=int(admin_id)),
    )


async def remove_admin_commands_for_chat(bot: Bot, user_id: int) -> None:
    """Убирает персональный admin-scope; пользователь снова видит default-команды."""
    await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=int(user_id)))


async def setup_bot_commands(bot: Bot) -> None:
    """
    Обычным пользователям показываем только пользовательские команды.
    Постоянным и добавленным администраторам — персональный полный набор.
    """
    # До настройки Telegram scope загружаем динамические права из БД в ADMIN_IDS,
    # чтобы database.is_admin() после перезапуска сразу видел добавленных админов.
    try:
        sync_dynamic_admins()
    except Exception as exc:
        print(f"⚠️ Не удалось загрузить динамических админов: {exc}")

    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())

    for admin_id in _admin_ids():
        try:
            await set_admin_commands_for_chat(bot, admin_id)
        except Exception as exc:
            # Один недоступный chat_id не должен мешать запуску бота.
            print(f"⚠️ Не удалось установить команды для admin {admin_id}: {exc}")
