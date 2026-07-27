# -*- coding: utf-8 -*-
import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommandScopeDefault
from config import BOT_TOKEN, COMMANDS
from database import create_tables, DB_NAME, start_month_processor
from engagement import create_engagement_tables
from level_tags import start_level_tag_processor

# Импорт модулей
from modules import admin, admin_factory, moderation, user, games, factory_orders

# Импорт функции для Render
from keep_alive import keep_alive 

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Run keep-alive web server only in Render-like environment.
    if os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"):
        keep_alive()

    # Инициализация БД
    await create_tables()
    await create_engagement_tables()
    print(f"DB path: {DB_NAME}")
    
    bot = Bot(
        token=BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    # Регистрация роутеров
    dp.include_router(admin.router)
    dp.include_router(admin_factory.router)
    dp.include_router(moderation.router)
    dp.include_router(factory_orders.router)
    dp.include_router(user.router)
    dp.include_router(games.router)

    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeDefault())
    factory_orders.start_factory_processor(bot)
    games.start_duel_processor(bot)
    start_month_processor()
    start_level_tag_processor(bot)

    print("Бот запущен. Система уровней 2.0 активирована.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
