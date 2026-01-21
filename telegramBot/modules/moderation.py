# -*- coding: utf-8 -*-
import re
import asyncio
import time
from typing import Callable, Dict, Any, Awaitable, Union
from aiogram import Router, types, F, Bot, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, ContentType
from config import WARN_LIMIT, OWNER_ID
from database import (
    get_list, manage_warn, get_user, 
    set_moderator_level, get_user_stats_full,
    update_xp
)
from utils import (
    answer_temp, get_user_link, delete_later, 
    parse_command_complex, flood_control, text_analyzer
)

router = Router()

# === КОНСТАНТЫ УРОВНЕЙ ДОСТУПА ===
LVL_USER = 0
LVL_HELPER = 1      # Ур1: мут, варн, снятие, профиль, стафф
LVL_MODER = 2       # Ур2: кик, профиль @user
LVL_SENIOR = 3      # Ур3: бан, разбан
LVL_MANAGER = 4     # Менеджер: выдача прав до ур3
LVL_ADMIN = 5       # Владелец

async def get_sender_level(chat: types.Chat, user_id: int) -> int:
    """
    Определяет уровень доступа пользователя.
    """
    # 1. Хардкод
    if user_id == OWNER_ID: 
        return LVL_ADMIN
    if user_id in [1087968824, 777000]: # Group Anonymous Bot, Telegram
        return LVL_ADMIN
    
    # 2. База данных
    user_data = await get_user(user_id)
    db_level = user_data[6] if user_data and len(user_data) > 6 else 0
    
    if db_level > 0:
        return db_level

    # 3. Админы чата
    if chat.type != 'private':
        try:
            member = await chat.get_member(user_id)
            if member.status in ['creator', 'administrator']: 
                return LVL_ADMIN
        except:
            pass
            
    return LVL_USER

async def is_admin(chat: types.Chat, user_id: int, sender_chat: types.Chat = None, required_level: int = 1) -> bool:
    if sender_chat and sender_chat.id == chat.id: 
        return True 
    
    actual_level = await get_sender_level(chat, user_id)
    return actual_level >= required_level

# === MIDDLEWARE: АНТИФЛУД ===
class FloodMiddleware(BaseMiddleware):
    async def __call__(
        self, 
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]], 
        event: types.Message, 
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, types.Message):
            return await handler(event, data)
        
        if event.chat.type == 'private':
            return await handler(event, data)

        if event.from_user:
            await get_user(event.from_user.id, event.from_user.username, event.from_user.full_name)
            
            user_id = event.from_user.id
            is_adm = await is_admin(event.chat, user_id, event.sender_chat, required_level=LVL_HELPER)

            content_for_flood = event.text or event.caption or "content"
            
            flood_status = flood_control.check(user_id, content_for_flood)
            
            if flood_status != 'ok' and not is_adm:
                try: 
                    await event.delete()
                except: 
                    pass
                
                user_name = event.from_user.full_name
                user_link = get_user_link(user_id, user_name)
                
                if flood_status == 'mute':
                    until = int(time.time()) + 600
                    try:
                        await event.chat.restrict(
                            user_id=user_id, 
                            permissions=ChatPermissions(can_send_messages=False), 
                            until_date=until
                        )
                        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
                        await answer_temp(event, f"🔇 {user_link} получил блокировку чата на <b>10 мин</b>,\nпричина: <i>Флуд</i>.")
                    except: 
                        pass
                    return 
                elif flood_status == 'warn':
                    await answer_temp(event, f"⚠️ {user_link}, не отправляйте много сообщений подряд.", delay=5, key=f"flood_warn_{user_id}")
                    return 

        return await handler(event, data)

router.message.outer_middleware(FloodMiddleware())


# === ЛОГИКА ФИЛЬТРАЦИИ КОНТЕНТА ===
async def bad_content_checker(message: types.Message) -> Union[bool, Dict[str, Any]]:
    if message.chat.type == 'private': return False
    
    if (message.text and message.text.startswith('/')) or (message.caption and message.caption.startswith('/')):
        return False
    
    user_id = message.from_user.id
    is_adm = await is_admin(message.chat, user_id, message.sender_chat, required_level=LVL_HELPER)
    if is_adm: return False

    text_to_analyze = message.text or message.caption or ""
    if not text_to_analyze: return False
    
    reason = None
    whitelist = await get_list('whitelist')
    badwords = await get_list('badwords')
    text_lower = text_to_analyze.lower()
    
    # Ссылки
    link_patterns = [r"(https?://|www\.|t\.me/)[^\s]+", r"[a-zA-Z0-9-]{2,}\.[a-zA-Z]{2,6}\b"]
    is_link = False
    for pattern in link_patterns:
        if re.search(pattern, text_lower):
            is_link = True
            break
            
    if is_link:
        is_allowed = False
        for wl_item in whitelist:
            if wl_item in text_lower:
                is_allowed = True
                break
        if not is_allowed: 
            reason = "Реклама / Ссылки"

    # Маты
    if not reason and text_analyzer.is_bad_word(text_to_analyze, badwords):
        reason = "Запрещенное слово"

    if reason:
        return {'reason': reason}
    
    return False

@router.message(
    F.content_type.in_({'text', 'sticker', 'photo', 'animation', 'video', 'voice', 'video_note', 'document', 'audio'}),
    bad_content_checker
)
async def handle_bad_content(message: types.Message, reason: str):
    await delete_later(message, 0)
    
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    
    current_warns = await manage_warn(user_id, "add", reason=reason)
    # Используем кликабельное имя из БД или текущего сообщения
    user_link_html = get_user_link(user_id, user_name)

    if current_warns >= WARN_LIMIT:
        try:
            until = int(time.time()) + 1800 # 30 минут
            await message.chat.restrict(
                user_id=user_id, 
                permissions=ChatPermissions(can_send_messages=False), 
                until_date=until
            )
            await manage_warn(user_id, "reset")
            # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
            await answer_temp(message, 
                f"🔇 {user_link_html} получил блокировку чата на <b>30 мин</b>,\n"
                f"причина: <i>{reason}</i> (Лимит предупреждений)."
            )
        except Exception as e:
            await answer_temp(message, f"Ошибка при выдаче наказания: {e}")
    else:
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        await answer_temp(message, 
            f"⚠️ {user_link_html} получил предупреждение ({current_warns}/{WARN_LIMIT}),\n"
            f"причина: <i>{reason}</i>."
        )


# === КОМАНДЫ МОДЕРАЦИИ ===

@router.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return await answer_temp(message, "Нет прав (Нужен <b>Moder¹</b>).", key=f"perm_err_{message.from_user.id}")

    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.", key=f"args_err_{message.from_user.id}")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя заглушить равного или старшего по званию.")

    duration = data['duration'] if data['duration'] else 600 # default 10 min
    minutes = int(duration / 60)
    
    try:
        await message.chat.restrict(user_id=data['target_id'], permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time())+duration)
        if data['delete_flag'] and message.reply_to_message: 
            await delete_later(message.reply_to_message, 0)
        
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        target_link = get_user_link(data['target_id'], data['target_name'])
        await answer_temp(message, 
            f"🔇 {target_link} получил блокировку чата на <b>{minutes} мин</b>,\n"
            f"причина: <i>{data['reason']}</i>."
        )
    except Exception as e: 
        await answer_temp(message, f"Ошибка: {e}")

@router.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return
    
    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя выдать предупреждение равному или старшему.")
    
    cnt = await manage_warn(data['target_id'], "add", reason=data['reason'])
    target_link = get_user_link(data['target_id'], data['target_name'])
    
    if data['delete_flag'] and message.reply_to_message: 
        await delete_later(message.reply_to_message, 0)
        
    if cnt >= WARN_LIMIT:
        until = int(time.time()) + 1800
        try:
            await message.chat.restrict(
                user_id=data['target_id'], 
                permissions=ChatPermissions(can_send_messages=False), 
                until_date=until
            )
            await manage_warn(data['target_id'], "reset")
            # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
            await answer_temp(message, 
                f"🔇 {target_link} получил блокировку чата на <b>30 мин</b>,\n"
                f"причина: <i>{data['reason']}</i> (Лимит предупреждений)."
            )
        except Exception as e:
            await answer_temp(message, f"Ошибка блокировки: {e}")
    else:
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        await answer_temp(message, 
            f"⚠️ {target_link} получил предупреждение ({cnt}/{WARN_LIMIT}),\n"
            f"причина: <i>{data['reason']}</i>."
        )

@router.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return
        
    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    action = "reset" if "all" in (command.args or "").lower() else "remove"
    cnt = await manage_warn(data['target_id'], action)
    target_link = get_user_link(data['target_id'], data['target_name'])
    await answer_temp(message, f"✅ Предупреждение снято для {target_link}. Текущее количество: {cnt}")

@router.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return
        
    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    await message.chat.restrict(
        user_id=data['target_id'], 
        permissions=ChatPermissions(
            can_send_messages=True, 
            can_send_media_messages=True, 
            can_send_other_messages=True, 
            can_add_web_page_previews=True
        )
    )
    target_link = get_user_link(data['target_id'], data['target_name'])
    # ИЗМЕНЕНО: СЛЕНГ УБРАН
    await answer_temp(message, f"🔊 Ограничения чата сняты с пользователя: {target_link}")

# --- УРОВЕНЬ 2: МОДЕРАТОР (Kick) ---

@router.message(Command("kick"))
async def cmd_kick(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_MODER):
        return await answer_temp(message, "Нужен уровень <b>Moder²</b>.")

    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя исключить равного или старшего.")

    try:
        await message.chat.ban(user_id=data['target_id'])
        await message.chat.unban(data['target_id']) 
        if data['delete_flag'] and message.reply_to_message: 
            await delete_later(message.reply_to_message, 0)
            
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        target_link = get_user_link(data['target_id'], data['target_name'])
        await answer_temp(message, 
            f"🚪{target_link} был исключен из чата,\n"
            f"причина: <i>{data['reason']}</i>."
        )
    except Exception as e: 
        await answer_temp(message, f"Ошибка: {e}")

# --- УРОВЕНЬ 3: СТАРШИЙ МОДЕРАТОР (Ban, Unban) ---

@router.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_SENIOR):
        return await answer_temp(message, "Нужен уровень <b>Moder³</b>.")

    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя заблокировать равного или старшего.")

    try:
        until = int(time.time()) + data['duration'] if data['duration'] else 0
        if until > 0: 
            await message.chat.ban(user_id=data['target_id'], until_date=until)
            days = int(data['duration'] / 86400)
            time_str = f"на {days} дней" if days > 0 else "временно"
        else: 
            await message.chat.ban(user_id=data['target_id'])
            time_str = "навсегда"
        
        if data['delete_flag'] and message.reply_to_message: 
            await delete_later(message.reply_to_message, 0)
            
        target_link = get_user_link(data['target_id'], data['target_name'])
        
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        await answer_temp(message, 
            f"⛔ {target_link} был заблокирован <b>{time_str}</b>,\n"
            f"причина: <i>{data['reason']}</i>."
        )
    except Exception as e: 
        await answer_temp(message, f"Ошибка: {e}")

@router.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_SENIOR): 
        return
        
    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    try:
        await message.chat.unban(data['target_id'])
        target_link = get_user_link(data['target_id'], data['target_name'])
        # ИЗМЕНЕНО: СЛЕНГ УБРАН
        await answer_temp(message, f"✅ Блокировка снята с пользователя: {target_link}")
    except: 
        await answer_temp(message, "Ошибка снятия блокировки.")

# --- УРОВЕНЬ МЕНЕДЖЕРА: ВЫДАЧА ПРАВ (promote) ---

@router.message(Command("promote", "setlevel"))
async def cmd_promote(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    if sender_lvl < LVL_MANAGER:
        return await answer_temp(message, "Доступно только <b>Manager</b> и выше.")

    args = command.args.split() if command.args else []
    if len(args) < 2:
        return await answer_temp(message, "Использование: <code>/setlevel @user [0-3]</code>")

    try:
        new_level = int(args[-1])
    except ValueError:
        return await answer_temp(message, "Уровень должен быть числом.")

    user_str = " ".join(args[:-1])
    fake_msg = message.model_copy(update={'text': f"/cmd {user_str}"})
    data = await parse_command_complex(fake_msg, user_str)
    
    if not data['target_id']:
         return await answer_temp(message, "Пользователь не найден.")

    if sender_lvl == LVL_MANAGER:
        if new_level >= LVL_MANAGER:
            return await answer_temp(message, "Manager может назначать только до 3 уровня.")
        target_current_lvl = await get_sender_level(message.chat, data['target_id'])
        if target_current_lvl >= LVL_MANAGER:
            return await answer_temp(message, "Нельзя менять права равного или старшего.")

    await set_moderator_level(data['target_id'], new_level)
    
    # Новые названия ролей
    role_name = "Пользователь"
    if new_level == 1: role_name = "Moder¹"
    if new_level == 2: role_name = "Moder²"
    if new_level == 3: role_name = "Moder³"
    
    target_link = get_user_link(data['target_id'], data['target_name'])
    await answer_temp(message, f"🆙 Пользователю {target_link} установлен уровень <b>{new_level} ({role_name})</b>.")

@router.message(Command("addxp", "givexp", "addexp"))
async def cmd_addxp(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_MANAGER):
        return await answer_temp(message, "Доступно с уровня <b>Manager (4)</b>.")

    args = command.args.split() if command.args else []
    if len(args) < 2:
        return await answer_temp(message, "Использование: <code>/addxp @user [кол-во]</code>")

    try:
        amount = int(args[-1])
    except ValueError:
        return await answer_temp(message, "Сумма XP должна быть целым числом.")

    user_str = " ".join(args[:-1])
    fake_msg = message.model_copy(update={'text': f"/cmd {user_str}"})
    data = await parse_command_complex(fake_msg, user_str)
    
    if not data['target_id']:
         return await answer_temp(message, "Пользователь не найден.")

    old_lvl, new_lvl, _ = await update_xp(data['target_id'], amount)
    
    target_link = get_user_link(data['target_id'], data['target_name'])
    msg_text = f"💳 Администратор выдал <code>{amount} XP</code> пользователю {target_link}."
    
    if new_lvl > old_lvl:
        msg_text += f"\n\n🆙 <b>Уровень повышен до {new_lvl}!</b>"
    elif new_lvl < old_lvl:
        msg_text += f"\n\n📉 <b>Уровень понижен до {new_lvl}...</b>"

    await answer_temp(message, msg_text)

@router.message(Command("modhelp"))
async def cmd_modhelp(message: types.Message):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): return
    
    text = (
        "📕 <b>Команды модератора</b>\n\n"
        "<b>Moder¹</b>\n"
        "• <code>/warn @username [причина]</code> — Выдать предупреждение (3 предупреждения = ограничение чата на 30 минут)\n"
        "• <code>/unwarn @username</code> — Снять предупреждение\n"
        "• <code>/mute @username [причина] [длительность]</code> — Ограничить доступ к чату. Формат времени: 1d 1h 1m 1s\n"
        "• <code>/unmute @username</code> — Снять ограничение чата\n"
        "• <code>/modhelp</code> — Эта панель\n\n"
        "<b>Moder²</b>\n"
        "• <code>/kick @username [причина]</code> — Исключить пользователя из чата\n"
        "• <code>/profile @username</code> — Просмотр чужого профиля\n\n"
        "<b>Moder³</b>\n"
        "• <code>/ban @username [причина] [Длительность]</code> — Заблокировать пользователя. Формат времени: 1d 1h 1m 1s\n"
        "• <code>/unban @username</code> — Снять блокировку с пользователя\n\n"
        "<b>Manager</b>\n"
        "• <code>/setlevel @username [0-3]</code> — Назначить пользователю звание персонала.\n"
        "• <code>/addxp @username [количество]</code> — Выдать пользователю опыт"
    )
    
    await answer_temp(
        message, 
        text, 
        delay=60,
        key=f"staff_global_{message.chat.id}" 
    )