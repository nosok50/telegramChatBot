import asyncio
import re
import time
import difflib
from aiogram import types
from config import AUTO_DELETE_TIME
from database import get_id_by_username

# === 0. MESSAGE TRACKER (Singleton temp messages) ===
# Stores {"scope_key": message_id}
_active_temp_messages = {}
_message_meta = {}
_delete_tasks = {}
_sticky_messages = {}


def _msg_key(chat_id: int, message_id: int):
    return f"{chat_id}:{message_id}"


def _cancel_delete_task(chat_id: int, message_id: int):
    k = _msg_key(chat_id, message_id)
    task = _delete_tasks.get(k)
    if task and not task.done():
        task.cancel()
    _delete_tasks.pop(k, None)


def _scoped_key(
    message: types.Message,
    key: str = None,
    global_key: str = None,
    user_id: int = None
):
    chat_id = message.chat.id
    if key:
        return f"custom:{chat_id}:{key}"
    if global_key:
        return f"global:{chat_id}:{global_key}"

    resolved_user = user_id
    if resolved_user is None and message.from_user:
        resolved_user = message.from_user.id

    if resolved_user is None:
        return f"fallback:{chat_id}"
    return f"user:{chat_id}:{resolved_user}"


def _sticky_scope(chat_id: int, scope: str) -> str:
    return f"sticky:{chat_id}:{scope}"


def is_anonymous_admin_message(message: types.Message) -> bool:
    sender_chat = getattr(message, "sender_chat", None)
    chat = getattr(message, "chat", None)
    return bool(sender_chat and chat and sender_chat.id == chat.id)


async def _send_sticky_message(
    message: types.Message,
    text: str = None,
    photo: str = None,
    reply: bool = False,
    **kwargs,
):
    if photo is not None:
        if reply:
            return await message.reply_photo(photo=photo, caption=text, **kwargs)
        return await message.answer_photo(photo=photo, caption=text, **kwargs)

    if reply:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


async def replace_sticky_message(
    message: types.Message,
    scope: str,
    text: str = None,
    photo: str = None,
    reply: bool = False,
    **kwargs,
):
    chat_id = message.chat.id
    scope_key = _sticky_scope(chat_id, scope)
    old_msg_id = _sticky_messages.get(scope_key, {}).get("message_id")
    if old_msg_id:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except Exception:
            pass

    sent_msg = await _send_sticky_message(message, text=text, photo=photo, reply=reply, **kwargs)
    _sticky_messages[scope_key] = {
        "message_id": sent_msg.message_id,
        "created_at": time.time(),
        "normal_messages": 0,
    }
    return sent_msg


async def ensure_sticky_message(
    message: types.Message,
    scope: str,
    text: str = None,
    min_age_seconds: int = 0,
    min_normal_messages: int = 0,
    photo: str = None,
    reply: bool = False,
    **kwargs,
):
    chat_id = message.chat.id
    scope_key = _sticky_scope(chat_id, scope)
    meta = _sticky_messages.get(scope_key)
    now = time.time()

    if not meta:
        return await replace_sticky_message(message, scope, text=text, photo=photo, reply=reply, **kwargs)

    age_ok = now - meta.get("created_at", 0) >= min_age_seconds
    count_ok = meta.get("normal_messages", 0) >= min_normal_messages
    if age_ok and count_ok:
        return await replace_sticky_message(message, scope, text=text, photo=photo, reply=reply, **kwargs)
    return None


def bump_sticky_message_counter(chat_id: int, scope: str, amount: int = 1):
    scope_key = _sticky_scope(chat_id, scope)
    meta = _sticky_messages.get(scope_key)
    if not meta:
        return
    meta["normal_messages"] = int(meta.get("normal_messages", 0)) + amount


async def answer_temp(
    message: types.Message,
    text: str = None,
    delay: int = None,
    key: str = None,
    global_key: str = None,
    user_id: int = None,
    photo: str = None,
    reply: bool = False,
    **kwargs,
):
    """
    Sends a temporary message.
    Default behavior: one active bot message per user in a chat.
    Use global_key for chat-wide singleton messages.
    """
    chat_id = message.chat.id
    scope_key = _scoped_key(message, key=key, global_key=global_key, user_id=user_id)
    ttl = AUTO_DELETE_TIME if delay is None else delay

    old_msg_id = _active_temp_messages.get(scope_key)
    if old_msg_id:
        _cancel_delete_task(chat_id, old_msg_id)
        _message_meta.pop(_msg_key(chat_id, old_msg_id), None)
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except Exception:
            pass

    try:
        if photo is not None:
            if reply:
                sent_msg = await message.reply_photo(photo=photo, caption=text, **kwargs)
            else:
                sent_msg = await message.answer_photo(photo=photo, caption=text, **kwargs)
        else:
            if reply:
                sent_msg = await message.reply(text, **kwargs)
            else:
                sent_msg = await message.answer(text, **kwargs)

        _active_temp_messages[scope_key] = sent_msg.message_id
        k = _msg_key(sent_msg.chat.id, sent_msg.message_id)
        _message_meta[k] = {"scope_key": scope_key, "ttl": ttl}
        task = asyncio.create_task(delete_later(sent_msg, ttl, scope_key))
        _delete_tasks[k] = task
        return sent_msg
    except Exception:
        pass


async def answer_persistent(
    message: types.Message,
    text: str,
    reply: bool = False,
    **kwargs,
):
    """Send a moderation audit message that is never scheduled for deletion."""
    if reply:
        return await message.reply(text, **kwargs)
    return await message.answer(text, **kwargs)


async def touch_temp_message(message: types.Message, delay: int = None):
    """
    Extends deletion timer for an already tracked temporary message.
    Useful when user interacts with inline menu and message should stay alive.
    """
    k = _msg_key(message.chat.id, message.message_id)
    meta = _message_meta.get(k)
    if not meta:
        return False

    ttl = meta.get("ttl", AUTO_DELETE_TIME) if delay is None else delay
    _cancel_delete_task(message.chat.id, message.message_id)
    task = asyncio.create_task(delete_later(message, ttl, meta.get("scope_key")))
    _delete_tasks[k] = task
    _message_meta[k]["ttl"] = ttl
    return True


async def delete_later(message: types.Message, delay: int = 0, key: str = None):
    try:
        if delay > 0:
            await asyncio.sleep(delay)

        await message.delete()
    except asyncio.CancelledError:
        return
    except Exception:
        pass

    # Clear key only if it still points to this message.
    if key and _active_temp_messages.get(key) == message.message_id:
        del _active_temp_messages[key]
    k = _msg_key(message.chat.id, message.message_id)
    _message_meta.pop(k, None)
    _delete_tasks.pop(k, None)


# === 1. TEXT CLEANER & NORMALIZER ===
class TextAnalyzer:
    def __init__(self):
        self.leet_map = {
            "0": "o",
            "1": "i",
            "3": "e",
            "4": "a",
            "5": "s",
            "7": "t",
            "8": "b",
            "@": "a",
            "$": "s",
            "(": "c",
            "+": "t",
            "_": "",
            ".": "",
            ",": "",
            "-": "",
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
            pattern = r"(^|\s|[^a-zа-яё0-9])" + re.escape(bad) + r"($|\s|[^a-zа-яё0-9])"
            if re.search(pattern, clean_text):
                return True

            for word in words:
                if len(bad) <= 3:
                    if word == bad:
                        return True
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
            self.users[user_id] = {"score": 0.0, "last_msg": "", "last_time": now}

        data = self.users[user_id]
        time_diff = now - data["last_time"]
        data["score"] = max(0.0, data["score"] - (time_diff * self.DECAY_RATE))

        current_weight = self.BASE_WEIGHT
        clean_text = text.lower().strip()

        if len(clean_text) < 5:
            current_weight *= self.SHORT_MSG_MULT
        if clean_text == data["last_msg"]:
            current_weight *= self.DUPLICATE_MULT
        elif self._calculate_similarity(clean_text, data["last_msg"]) > 0.75:
            current_weight *= self.SIMILAR_MULT
        if len(clean_text) > 8 and len(set(clean_text)) < 4:
            current_weight *= 2.0

        data["score"] += current_weight
        data["last_msg"] = clean_text
        data["last_time"] = now

        if data["score"] >= self.MAX_SCORE:
            data["score"] = self.WARN_SCORE
            return "mute"
        if data["score"] >= self.WARN_SCORE:
            return "warn"
        return "ok"


flood_control = SmartFloodControl()


# === HELPER FUNCTIONS ===
def parse_time(time_string):
    if not time_string:
        return None
    time_string = time_string.lower().replace(" ", "")
    total_seconds = 0
    found = False
    matches = re.findall(r"(\d+)([dhms])", time_string)
    multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for amount, unit in matches:
        total_seconds += int(amount) * multipliers[unit]
        found = True
    return total_seconds if found else None


def get_user_link(user_id: int, full_name: str = "User"):
    return f'<a href="tg://user?id={user_id}">{full_name}</a>'


def moderation_help_text(include_title: bool = True) -> str:
    title = "🛡 <b>КОМАНДЫ МОДЕРАЦИИ</b>\n\n" if include_title else ""
    return title + (
        "<b>Moder¹</b>\n"
        "• <code>/warn @user причина</code> — предупреждение\n"
        "• <code>/unwarn @user</code> — снять предупреждение\n"
        "• <code>/mute @user 1h причина</code> — выдать мут\n"
        "• <code>/mute @user 1h -clear 10 причина</code> — мут и очистка сообщений\n"
        "• <code>/clear @user 10</code> — очистка без наказания\n"
        "• <code>/unmute @user</code> — снять мут\n"
        "Можно отвечать командой на сообщение пользователя. Например: "
        "<code>/mute 1h -clear 10 реклама</code> или <code>/clear 10</code>.\n\n"
        "<b>Moder²</b>\n"
        "• <code>/kick @user причина</code> — исключить из чата\n"
        "• <code>/profile @user</code> — посмотреть профиль\n\n"
        "<b>Moder³</b>\n"
        "• <code>/ban @user 7d причина</code> — заблокировать\n"
        "• <code>/unban @user</code> — снять блокировку\n"
        "• <code>/resetdice @user</code> — сбросить бесплатный кубик\n"
        "• <code>/addcoins @user 100</code> — выдать монеты\n\n"
        "<b>Manager</b>\n"
        "• <code>/setlevel @user 0-3</code> — изменить роль персонала\n"
        "• <code>/addxp @user 100</code> — выдать опыт\n\n"
        "Время: <code>m</code> — минуты, <code>h</code> — часы, <code>d</code> — дни."
    )


async def parse_command_complex(message: types.Message, args_str: str):
    target_id = None
    target_name = "User"
    duration = None
    reason_parts = []
    delete_msg_flag = False
    clear_count = 0
    parse_error = None

    args = args_str.split() if args_str else []

    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    elif args:
        first = args[0]
        if first.isdigit():
            target_id = int(first)
            target_name = f"ID:{target_id}"
            args.pop(0)
        else:
            # Accept @username and plain username.
            candidate = first.lstrip("@")
            if candidate:
                found_id = await get_id_by_username(candidate)
                if found_id:
                    target_id = found_id
                    target_name = f"@{candidate}"
                    args.pop(0)

    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-del":
            if clear_count:
                parse_error = "Нельзя одновременно использовать -del и -clear."
            delete_msg_flag = True
            index += 1
            continue
        if arg == "-clear":
            if delete_msg_flag:
                parse_error = "Нельзя одновременно использовать -del и -clear."
            if index + 1 >= len(args):
                parse_error = "После -clear укажите число сообщений от 1 до 100."
                index += 1
                continue
            try:
                parsed_count = int(args[index + 1])
            except ValueError:
                parsed_count = 0
            if not 1 <= parsed_count <= 100:
                parse_error = "Количество сообщений для очистки должно быть от 1 до 100."
            else:
                clear_count = parsed_count
            index += 2
            continue
        if duration is None:
            parsed = parse_time(arg)
            if parsed:
                duration = parsed
                index += 1
                continue
        reason_parts.append(arg)
        index += 1

    reason = " ".join(reason_parts) if reason_parts else "Не указана"
    return {
        "target_id": target_id,
        "target_name": target_name,
        "duration": duration,
        "reason": reason,
        "delete_flag": delete_msg_flag,
        "clear_count": clear_count,
        "parse_error": parse_error,
    }
