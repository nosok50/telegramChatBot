# -*- coding: utf-8 -*-
import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommandScopeDefault
from config import BOT_TOKEN, COMMANDS
from database import create_tables, DB_NAME

# РРјРїРѕСЂС‚ РјРѕРґСѓР»РµР№
from modules import admin, moderation, user, games 

# !!! РРњРџРћР Рў Р¤РЈРќРљР¦РР Р”Р›РЇ RENDER !!!
from keep_alive import keep_alive 

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Run keep-alive web server only in Render-like environment.
    if os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"):
        keep_alive()

    # РРЅРёС†РёР°Р»РёР·Р°С†РёСЏ Р‘Р”
    await create_tables()
    print(f"DB path: {DB_NAME}")
    
    bot = Bot(
        token=BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    # Р РµРіРёСЃС‚СЂР°С†РёСЏ СЂРѕСѓС‚РµСЂРѕРІ
    dp.include_router(admin.router)
    dp.include_router(moderation.router)
    dp.include_router(user.router)
    dp.include_router(games.router)

    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeDefault())

    print("рџљЂ Р‘РѕС‚ Р·Р°РїСѓС‰РµРЅ! РЎРёСЃС‚РµРјР° СѓСЂРѕРІРЅРµР№ 2.0 Р°РєС‚РёРІРёСЂРѕРІР°РЅР°.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Р‘РѕС‚ РѕСЃС‚Р°РЅРѕРІР»РµРЅ")
