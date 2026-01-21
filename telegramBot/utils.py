import asyncio
import re
import time
import difflib
from aiogram import types, Bot
from database import get_id_by_username

# === 0. MESSAGE TRACKER (Синглтон сообщений) ===
# Хранит { 'unique_key': message_id }
_active_temp_messages = {}

async def answer_temp(message: types.Message, text: str, delay: int = 60, key: str = None):
    """
    Отправляет временное сообщение.
    :param key: Уникальный ключ. Если сообщение с таким ключом уже есть в чате, оно удалится перед отправкой нового.
    """
    bot = message.bot
    chat_id = message.chat.id

    # Если передан ключ, пытаемся удалить старое сообщение этого типа
    if key:
        old_msg_id = _active_temp_messages.get(key)
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except:
                pass # Сообщение могло быть удалено вручную или устареть
    
    try:
        sent_msg = await message.answer(text)
        
        # Регистрируем новое сообщение, если есть ключ
        if key:
            _active_temp_messages[key] = sent_msg.message_id

        # Запускаем таймер удаления
        asyncio.create_task(delete_later(sent_msg, delay, key))
        return sent_msg
    except Exception:
        pass

async def delete_later(message: types.Message, delay: int = 0, key: str = None):
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass
    
    # Очистка ключа, если он все еще указывает на это сообщение
    # (чтобы не удалить ключ, если он уже был перезаписан более новым сообщением)
    if key and _active_temp_messages.get(key) == message.message_id:
        del _active_temp_messages[key]

# === 1. TEXT CLEANER & NORMALIZER ===
class TextAnalyzer:
    def __init__(self):
        self.leet_map = {
            '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', 
            '7': 't', '8': 'b', '@': 'a', '$': 's', '(': 'c',
            '+': 't', '_': '', '.': '', ',': '', '-': ''
        }
    
    def normalize(self, text: str) -> str:
        text = text.lower()
        for char, repl in self.leet_map.items():
            text = text.replace(char, repl)
        return text

    def is_bad_word(self, text: str, badwords: list) -> bool:
        clean_text = self.normalize(text)
        words = clean_text.split()
        
        for bad in badwords:
            pattern = r'(^|\s|[^a-zа-яё0-9])' + re.escape(bad) + r'($|\s|[^a-zа-яё0-9])'
            if re.search(pattern, clean_text):
                return True

            for word in words:
                if len(bad) <= 3: 
                    if word == bad: return True
                    continue
                if difflib.SequenceMatcher(None, word, bad).ratio() > 0.85:
                    return True
        return False

text_analyzer = TextAnalyzer()

# === 2. SMART FLOOD CONTROL ===
class SmartFloodControl:
    def __init__(self):
        self.users = {}
        self.DECAY_RATE = 0.5      
        self.MAX_SCORE = 10.0      
        self.WARN_SCORE = 6.0      
        self.BASE_WEIGHT = 1.0     
        self.SHORT_MSG_MULT = 1.5  
        self.DUPLICATE_MULT = 4.0  
        self.SIMILAR_MULT = 2.0    
        
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        return difflib.SequenceMatcher(None, text1, text2).ratio()

    def check(self, user_id: int, text: str):
        now = time.time()
        if user_id not in self.users:
            self.users[user_id] = {'score': 0.0, 'last_msg': "", 'last_time': now}
        
        data = self.users[user_id]
        time_diff = now - data['last_time']
        data['score'] = max(0.0, data['score'] - (time_diff * self.DECAY_RATE))
        
        current_weight = self.BASE_WEIGHT
        clean_text = text.lower().strip()
        
        if len(clean_text) < 5: current_weight *= self.SHORT_MSG_MULT
        if clean_text == data['last_msg']: current_weight *= self.DUPLICATE_MULT
        elif self._calculate_similarity(clean_text, data['last_msg']) > 0.75: current_weight *= self.SIMILAR_MULT
        if len(clean_text) > 8 and len(set(clean_text)) < 4: current_weight *= 2.0

        data['score'] += current_weight
        data['last_msg'] = clean_text
        data['last_time'] = now

        if data['score'] >= self.MAX_SCORE:
            data['score'] = self.WARN_SCORE 
            return 'mute'
        if data['score'] >= self.WARN_SCORE:
            return 'warn'
        return 'ok'

flood_control = SmartFloodControl()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

def parse_time(time_string):
    if not time_string: return None
    time_string = time_string.lower().replace(" ", "")
    total_seconds = 0
    found = False
    matches = re.findall(r'(\d+)([dhms])', time_string)
    multipliers = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
    for amount, unit in matches:
        total_seconds += int(amount) * multipliers[unit]
        found = True
    return total_seconds if found else None

def get_user_link(user_id: int, full_name: str = "User"):
    return f'<a href="tg://user?id={user_id}">{full_name}</a>'

async def parse_command_complex(message: types.Message, args_str: str):
    target_id = None
    target_name = "User"
    duration = None
    reason_parts = []
    delete_msg_flag = False

    args = args_str.split() if args_str else []

    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    elif args and args[0].startswith('@'):
        username = args[0]
        target_id = await get_id_by_username(username)
        target_name = username
        args.pop(0)
    elif args and args[0].isdigit():
        target_id = int(args[0])
        target_name = f"ID:{target_id}"
        args.pop(0)

    for arg in args:
        if arg == '-del':
            delete_msg_flag = True
            continue
        if duration is None:
            parsed = parse_time(arg)
            if parsed:
                duration = parsed
                continue
        reason_parts.append(arg)

    reason = " ".join(reason_parts) if reason_parts else "Не указана"
    return {'target_id': target_id, 'target_name': target_name, 'duration': duration, 'reason': reason, 'delete_flag': delete_msg_flag}