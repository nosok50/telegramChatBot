# -*- coding: utf-8 -*-
import os
from pathlib import Path

from aiogram.types import BotCommand
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it to .env or the environment.")

OWNER_ID = int(os.getenv("OWNER_ID", "1089429471"))

# Настройки модерации
WARN_LIMIT = 3
AUTO_DELETE_TIME = 60

# Настройки опыта
DEFAULT_XP_PER_MSG = (1, 5)

# Основное меню команд Telegram. Модераторская справка доступна персоналу
# отдельной кнопкой внутри /help и командой /modhelp.
COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="games", description="Игровая зона"),
    BotCommand(command="factory", description="Управление цехом"),
    BotCommand(command="factory_order", description="Запустить заказ завода"),
    BotCommand(command="leaders", description="Таблица лидеров"),
    BotCommand(command="staff", description="Состав персонала"),
    BotCommand(command="help", description="Справка по командам"),
]
