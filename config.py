import os

TIMEZONE = "Europe/Moscow"

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [1438843200, 2038755591, 606585432]

WASHING_MACHINES = ["Стиральная №1", "Стиральная №3", "Стиральная №5", "Стиральная №6"]
DRYERS = ["Сушилка №2", "Сушилка №4"]


WORKING_HOURS = list(range(7, 23))  # 9–23
BOOKING_DAYS_AHEAD = 3  # сегодня + 2 дня вперёд
DB_PATH = "laundry.db"
