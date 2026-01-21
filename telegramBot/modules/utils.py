import asyncio
import re
from aiogram import types
from datetime import timedelta

# Парсер времени (10d 20m 30s -> секунды)
def parse_time(time_string):
    if not time_string:
        return None
    
    # Регулярки для поиска дней, часов, минут, секунд
    # Поддерживает форматы: 1d, 10m, 30s, 1d10h, 10m 20s
    time_string = time_string.lower().replace(" ", "")
    
    total_seconds = 0
    found = False

    # Дни
    days = re.search(r'(\d+)d', time_string)
    if days:
        total_seconds += int(days.group(1)) * 86400
        found = True
        
    # Часы
    hours = re.search(r'(\d+)h', time_string)
    if hours:
        total_seconds += int(hours.group(1)) * 3600
        found = True

    # Минуты
    minutes = re.search(r'(\d+)m', time_string)
    if minutes:
        total_seconds += int(minutes.group(1)) * 60
        found = True

    # Секунды
    seconds = re.search(r'(\d+)s', time_string)
    if seconds:
        total_seconds += int(seconds.group(1))
        found = True

    return total_seconds if found else None

# Функция для создания ссылки на юзера
def get_user_link(user: types.User):
    return f'<a href="tg://user?id={user.id}">{user.full_name}</a>'

# Обертка для отправки "временных" сообщений
async def answer_temp(message: types.Message, text: str, delay: int = 60):
    try:
        sent_msg = await message.answer(text)
        # Запускаем фоновую задачу на удаление
        asyncio.create_task(delete_later(sent_msg, delay))
    except Exception:
        pass

# Удаление сообщения через N секунд
async def delete_later(message: types.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass # Сообщение уже удалено или нет прав

# Помощник для разбора аргументов команд
# Возвращает: (целевой_юзер, время_в_сек, причина)
async def extract_args(message: types.Message, bot):
    args = message.text.split()[1:] # Отбрасываем саму команду
    target_user = None
    duration = None
    reason = "Не указана"

    # 1. Ищем цель (Reply или Mention)
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        # Аргументы это всё, что написано в сообщении
        # Пример: /mute 10m спам
    elif args and args[0].startswith('@'):
        username = args[0][1:] # Убираем @
        # Пытаемся найти, но aiogram не умеет искать по юзернейму без кеша. 
        # В идеале нужно хранить ID в своей БД.
        # Для упрощения считаем, что аргумент @username - это просто текст, 
        # но для реальной работы нужен ID.
        # Хак: просим админа переслать сообщение или ответить. 
        # НО, если очень надо, можно попробовать разрешить, но бот должен "видеть" юзера.
        args.pop(0) # Убираем юзернейм из аргументов
        # ВНИМАНИЕ: Без UserID (числового) забанить нельзя. 
        # Поэтому в этом коде мы будем требовать Reply, 
        # либо доработаем это через базу данных (get_user_by_username - если сохраняли ранее)
        return None, None, "Ошибка: Для надежности используйте Reply (ответ) на сообщение нарушителя. Поиск по @username работает только если пользователь уже писал боту."
    else:
        return None, None, "Не указан пользователь."

    # 2. Парсим оставшиеся аргументы (Время и Причина)
    if args:
        # Пробуем первый аргумент как время
        parsed_time = parse_time(args[0])
        if parsed_time:
            duration = parsed_time
            args.pop(0) # Убираем время
            if args:
                reason = " ".join(args)
        else:
            # Если первый аргумент не время, значит это причина (для /kick, /ban без времени)
            reason = " ".join(args)
    
    return target_user, duration, reason

# Получение объекта User из сообщения (Улучшенная версия для модерации)
async def get_target_from_msg(message: types.Message, bot):
    # Если реплай
    if message.reply_to_message:
        return message.reply_to_message.from_user, message.text.split(maxsplit=1)[1:]
    
    # Если меншн @username (сложная логика, т.к. API не дает ID по юзернейму напрямую)
    # Здесь мы упростим: требуем Reply для надежности, так как это гарантирует наличие ID.
    return None, None