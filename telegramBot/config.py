# -*- coding: utf-8 -*-
from aiogram.types import BotCommand

BOT_TOKEN = "8123130646:AAGpDw3Rp_3Rj7RDSAfNmDh80pB1rEPNk74" # РўРІРѕР№ С‚РѕРєРµРЅ
OWNER_ID = 1089429471 

# РќР°СЃС‚СЂРѕР№РєРё РјРѕРґРµСЂР°С†РёРё
WARN_LIMIT = 3  # РљРѕР»РёС‡РµСЃС‚РІРѕ РїСЂРµРґСѓРїСЂРµР¶РґРµРЅРёР№ РґРѕ Р±Р»РѕРєРёСЂРѕРІРєРё
AUTO_DELETE_TIME = 60 # Р’СЂРµРјСЏ Р¶РёР·РЅРё СЃРѕРѕР±С‰РµРЅРёР№ Р±РѕС‚Р° (СЃРµРєСѓРЅРґС‹)

# РќР°СЃС‚СЂРѕР№РєРё РѕРїС‹С‚Р° 
DEFAULT_XP_PER_MSG = (1, 5) # Р”РёР°РїР°Р·РѕРЅ РѕРїС‹С‚Р° Р·Р° СЃРѕРѕР±С‰РµРЅРёРµ (РјРёРЅ, РјР°РєСЃ)

# РљРѕРјР°РЅРґС‹ РґР»СЏ РјРµРЅСЋ (С‚РѕР»СЊРєРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРёРµ)
COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="games", description="Игровая зона"),
    BotCommand(command="leaders", description="Таблица лидеров"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="duel", description="Вызвать на дуэль"),
]
