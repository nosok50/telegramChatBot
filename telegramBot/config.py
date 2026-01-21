# -*- coding: utf-8 -*-
from aiogram.types import BotCommand

BOT_TOKEN = "8123130646:AAGpDw3Rp_3Rj7RDSAfNmDh80pB1rEPNk74" # Твой токен
OWNER_ID = 1089429471 

# Настройки модерации
WARN_LIMIT = 3  # Количество варнов до бана
AUTO_DELETE_TIME = 60 # Время жизни сообщений бота (секунды)

# Настройки опыта (ВОТ ЭТОЙ СТРОКИ НЕ ХВАТАЛО)
DEFAULT_XP_PER_MSG = (1, 5) # Диапазон опыта за сообщение (мин, макс)

# Команды для меню (чтобы они подсказывались)
COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="staff", description="Команды персонала"),
]