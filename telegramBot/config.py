# -*- coding: utf-8 -*-
from aiogram.types import BotCommand

BOT_TOKEN = "8123130646:AAGpDw3Rp_3Rj7RDSAfNmDh80pB1rEPNk74" # Тестовый токен
OWNER_ID = 1089429471 

# Настройки модерации
WARN_LIMIT = 3  # Количество предупреждений до блокировки
AUTO_DELETE_TIME = 60  # Время жизни сообщений бота в секундах

# Настройки опыта
DEFAULT_XP_PER_MSG = (1, 5)  # Диапазон опыта за сообщение (мин, макс)

# Команды для меню
COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="games", description="Игровая зона"),
    BotCommand(command="factory", description="Управление цехом"),
    BotCommand(command="leaders", description="Таблица лидеров"),
    BotCommand(command="help", description="Справка по командам"),
]
