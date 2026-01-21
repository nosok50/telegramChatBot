# -*- coding: utf-8 -*-
import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommandScopeDefault
from config import BOT_TOKEN, COMMANDS
from database import create_tables

# Импорт модулей
from modules import admin, moderation, user, games # Добавлен games

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Инициализация БД
    await create_tables()
    
    bot = Bot(
        token=BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    # Регистрация роутеров
    dp.include_router(admin.router)      # Админка
    dp.include_router(moderation.router) # Модерация
    dp.include_router(user.router)       # Пользовательские (XP, Profile, Rep)
    dp.include_router(games.router)      # Игры (Dice, Duel) - НОВОЕ

    # Установка команд в меню
    # Добавляем новые команды в список для удобства, если они не в config
    # Но лучше обновить config.py. Предполагаем, что COMMANDS берется оттуда.
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeDefault())

    print("🚀 Бот запущен! Система уровней 2.0 активирована.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")