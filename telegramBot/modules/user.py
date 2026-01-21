# -*- coding: utf-8 -*-
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from database import (
    get_user, update_xp, get_warn_reasons, get_id_by_username, 
    LEVEL_CAPS, give_reputation, check_wipe_cooldown,
    get_top_users, get_user_rank, get_all_staff 
)
from config import DEFAULT_XP_PER_MSG, WARN_LIMIT, OWNER_ID
from utils import delete_later, answer_temp, get_user_link
import random
import time
from datetime import datetime

router = Router()

# КЕШИ
user_last_msg = {}
media_cooldown = {}
chat_last_active = {}
last_welcome_messages = {}

# КЕШ ДЛЯ ПРОФИЛЕЙ: {user_id: message_id}
profile_messages = {}

# URL КАРТИНОК
IMG_LEVEL_3 = "https://i.ibb.co/S45s7p2D/Frame-26085979.png"
IMG_LEVEL_4 = "https://i.ibb.co/KjQGJMKL/Frame-26085980.png"
IMG_LEVEL_5 = "https://i.ibb.co/9HCSx0g2/Frame-26085981.png"
IMG_HELP_LEADERS = "https://i.ibb.co/JwC8C58d/Frame-26085985.png"
IMG_WELCOME = "https://i.ibb.co/Q3GG72fN/Frame-26085986.png"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def get_effective_level(user_id: int, chat: types.Chat, db_level: int):
    """
    Определяет эффективный уровень (учитывая права админа).
    """
    effective_level = db_level
    
    # 1. Проверка глобальных ID (Владелец, Аноним, Telegram)
    if user_id in [OWNER_ID, 1087968824, 777000]:
        return 5
        
    # 2. Проверка админки в текущем чате
    if chat.type != 'private':
        try:
            member = await chat.get_member(user_id)
            if member.status in ['creator', 'administrator']: 
                return 5
        except: 
            pass
            
    return effective_level

def format_xp(value):
    """Форматирует число с пробелами (10 000)"""
    return "{:,}".format(value).replace(",", " ")

# Хелпер для кнопки игры
def get_game_btn_simple(game_key, user_level, title, callback_base, owner_id):
    GAME_REQS = {'dice': 3, 'slots': 3, 'basketball': 4, 'duel': 4}
    req_lvl = GAME_REQS.get(game_key, 0)
    
    if user_level >= req_lvl:
        return InlineKeyboardButton(text=title, callback_data=f"{callback_base}:{owner_id}")
    else:
        return InlineKeyboardButton(text=f"🔒 {req_lvl} Ур.", callback_data=f"locked_game:{req_lvl}")

# --- КОМАНДЫ ---

# ИЗМЕНЕНО: Добавлен фильтр F.chat.type == "private", чтобы работало только в ЛС
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: types.Message):
    # delete_later убран, так как в ЛС бот не может удалять сообщения пользователя
    await message.answer_photo(
        photo=IMG_WELCOME,
        caption=(
            f"👋 <b>Я бот-модератор для чата.</b>\n"
            f"<i>Чтобы посмотреть мои команды, напиши</i> <code>/help</code>."
        )
    )

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await delete_later(message, 0)
    
    user_data = await get_user(message.from_user.id)
    lvl = user_data[4]

    text = (
        "📚 <b>УРОВНИ</b>\n\n"
        "<b>Уровень 1:</b>\n"
        "Новички — доступен только чат.\n\n"
        "<b>Уровень 2:</b>\n"
        "• <code>/profile</code> — Просмотр профиля\n"
        "• <code>/leaders</code> — Список лидеров\n\n"
        "<b>Уровень 3:</b>\n"
        "• <code>/games</code> — Меню аркад\n"
        "• <code>/duel @user</code> — Вызвать на дуэль\n"
        "• <code>/staff</code> — Состав персонала\n\n"
        "<b>Уровень 4:</b>\n"
        "• Ответь <code>+rep</code> — Повысить репутацию\n"
        "• <code>/profile @username</code> — Просмотр чужих профилей\n\n"
        "<b>Уровень 5:</b>\n"
        "• Ответь <code>/wipe</code> — Удалить сообщение (1 раз в день)\n\n"
        f"Ваш уровень: <b>{lvl}</b>"
    )

    await message.answer_photo(
        photo=IMG_HELP_LEADERS,
        caption=text,
        parse_mode="HTML"
    )

@router.message(Command("staff"))
async def cmd_staff(message: types.Message):
    await delete_later(message, 0)
    
    user_data = await get_user(message.from_user.id)
    lvl = user_data[4]
    
    eff_lvl = await get_effective_level(message.from_user.id, message.chat, lvl)
    if eff_lvl < 3:
        return await answer_temp(message, "🔒 Команда <code>/staff</code> доступна с <b>3 уровня</b>.", delay=5)

    staff_list = await get_all_staff()
    if not staff_list:
        return await answer_temp(message, "Список персонала пуст.", delay=10)
    
    # Новые названия ролей
    roles = {
        4: "Manager",
        3: "Moder³",
        2: "Moder²",
        1: "Moder¹"
    }
    
    grouped = {}
    for name, mod_lvl, username, uid in staff_list:
        if mod_lvl >= 5: continue
        
        role_title = roles.get(mod_lvl, f"Role {mod_lvl}")
        if role_title not in grouped:
            grouped[role_title] = []
        
        link = f"<a href='tg://user?id={uid}'>{name}</a>"
        grouped[role_title].append(f"• {link}")

    text_lines = ["📕 <b>Команды модератора</b>\n"]
    
    # 1. Список персонала
    for lvl_idx in [4, 3, 2, 1]:
        role_title = roles.get(lvl_idx)
        if role_title in grouped and grouped[role_title]:
            text_lines.append(f"<b>{role_title}</b>")
            text_lines.extend(grouped[role_title])
            text_lines.append("")

    # 2. Список команд
    text_commands = (
        "<b>Moder¹</b>\n"
        "• <code>/warn @user [причина]</code> — Выдать предупреждение (3 пред. = блок 30 мин)\n"
        "• <code>/unwarn @user</code> — Снять предупреждение\n"
        "• <code>/mute @user [причина] [время]</code> — Ограничить чат. Формат: 1d 1h 1m\n"
        "• <code>/unmute @user</code> — Снять ограничение\n"
        "• <code>/modhelp</code> — Эта панель\n\n"
        "<b>Moder²</b>\n"
        "• <code>/kick @user [причина]</code> — Исключить пользователя\n"
        "• <code>/profile @user</code> — Просмотр чужого профиля\n\n"
        "<b>Moder³</b>\n"
        "• <code>/ban @user [причина] [время]</code> — Заблокировать пользователя\n"
        "• <code>/unban @user</code> — Снять блокировку\n\n"
        "<b>Manager</b>\n"
        "• <code>/setlevel @user [0-3]</code> — Назначить персонал\n"
        "• <code>/addxp @user [кол-во]</code> — Выдать опыт"
    )
    
    full_text = "\n".join(text_lines) + "\n" + "_"*15 + "\n\n" + text_commands
            
    await answer_temp(message, full_text, delay=60)

@router.message(Command("leaders"))
async def cmd_leaders(message: types.Message):
    await delete_later(message, 0)
    
    user_data = await get_user(message.from_user.id)
    lvl = user_data[4]
    
    eff_lvl = await get_effective_level(message.from_user.id, message.chat, lvl)
    if eff_lvl < 2:
        return await answer_temp(message, "🔒 Список лидеров доступен со <b>2 уровня</b>.", delay=5)
    
    text = await generate_leaders_text(message.from_user.id)
    
    await message.answer_photo(
        photo=IMG_HELP_LEADERS,
        caption=text,
        parse_mode="HTML"
    )

async def generate_leaders_text(user_id):
    top_users = await get_top_users(limit=10)
    text = "🏆 <b>ТОП ЛИДЕРОВ</b>\n\n"
    
    top_ids = []

    for i, (name, lvl, xp, uid) in enumerate(top_users, 1):
        top_ids.append(uid)
        link_name = f"<a href='tg://user?id={uid}'>{name}</a>"
        text += f"<b>{i}.</b> [LEVEL <b>{lvl}</b>] {link_name} (<code>{format_xp(xp)} XP</code>)\n"
    
    if user_id not in top_ids:
        my_stats = await get_user_rank(user_id)
        if my_stats:
            rank, my_lvl, my_xp = my_stats
            text += f"\n\n<b>{rank}.</b> [LEVEL <b>{my_lvl}</b>] Вы (<code>{format_xp(my_xp)} XP</code>)"
        
    return text

@router.message(Command("profile"))
async def show_profile(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    user_id = message.from_user.id

    if user_id in profile_messages:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=profile_messages[user_id])
        except Exception:
            pass

    caller_data = await get_user(
        user_id=message.from_user.id, 
        username=message.from_user.username, 
        full_name=message.from_user.full_name
    )
    db_level = caller_data[6] if caller_data and len(caller_data) > 6 else 0
    lvl = caller_data[4]
    
    effective_level = await get_effective_level(message.from_user.id, message.chat, db_level)

    target_id = None
    is_foreign_request = (command.args is not None and command.args.strip()) or message.reply_to_message

    if is_foreign_request:
        if effective_level < 4 and lvl < 4:
            return await answer_temp(message, "⛔ Просмотр чужих профилей доступен с <b>4 уровня</b>.")
        
        if command.args:
            username_arg = command.args.split()[0].replace("@", "")
            found_id = await get_id_by_username(username_arg)
            if found_id: target_id = found_id
            else: return await answer_temp(message, "❌ Пользователь не найден.")
        elif message.reply_to_message:
            target_id = message.reply_to_message.from_user.id
    else:
        target_id = message.from_user.id
    
    if not target_id: return

    text, photo = await generate_profile_content(target_id)
    
    markup = None
    if target_id == message.from_user.id:
        final_lvl = effective_level if effective_level > lvl else lvl
        markup = get_profile_keyboard(final_lvl)

    try:
        if photo:
            msg = await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            msg = await message.answer(text, parse_mode="HTML", reply_markup=markup)
        
        profile_messages[user_id] = msg.message_id
        await delete_later(msg, 60)
        
    except Exception as e:
        await answer_temp(message, f"Ошибка: {e}")


async def generate_profile_content(user_id):
    data = await get_user(user_id)
    if not data: return "Нет данных.", None
    
    _, _, db_full_name, xp, lvl, warns, mod_lvl, rep = data
    
    role_map = {
        1: "Moder¹",
        2: "Moder²",
        3: "Moder³",
        4: "Manager",
        5: "Admin",
        999: "Owner"
    }
    
    user_link = get_user_link(user_id, db_full_name)
    
    if mod_lvl > 0:
        role_tag = role_map.get(mod_lvl, "Staff")
        name_line = f"<b>[{role_tag}]</b> {user_link}"
    else:
        name_line = f"{user_link}"

    MAX_LEVEL = 5
    cap = LEVEL_CAPS.get(lvl, float('inf'))
    
    if lvl >= MAX_LEVEL:
        level_display = "LEVEL <b>MAX</b>"
        progress_bar = "▰▰▰▰▰▰▰▰▰▰ <b>100%</b>"
        xp_line = f"Опыт: <code>{format_xp(xp)} XP</code>"
    else:
        level_display = f"Level <b>{lvl}</b>"
        percent = min(100, int((xp / cap) * 100))
        blocks = int(percent / 10)
        bar_visual = f"{'▰'*blocks}{'▱'*(10-blocks)}"
        progress_bar = f"{bar_visual} <b>{percent}%</b>"
        xp_line = f"Опыт: <code>{xp}/{cap} XP</code>"

    rep_line = ""
    if rep > 0:
        rep_line = f"\n🤝 Репутация: <b>{rep}</b>"

    warn_text = ""
    if warns > 0:
        reasons_list = await get_warn_reasons(user_id)
        if reasons_list:
            reasons_formatted = "\n".join([f"• <i>{r}</i>" for r in reasons_list])
            warn_text = f"\n\n⚠️ <b>Предупреждения:</b>\n{reasons_formatted}"
        else:
            warn_text = f"\n\n⚠️ <b>Предупреждения:</b>\n• {warns}/{WARN_LIMIT}"

    profile_text = (
        f"👤 Профиль: {name_line}"
        f"{rep_line}\n\n"
        f"{level_display}: {progress_bar}\n"
        f"{xp_line}"
        f"{warn_text}" 
    )
    
    photo = None
    if lvl >= 3:
        if lvl == 3: photo = IMG_LEVEL_3
        elif lvl == 4: photo = IMG_LEVEL_4
        else: photo = IMG_LEVEL_5
        
    return profile_text, photo

def get_profile_keyboard(user_lvl):
    if user_lvl >= 2:
        btn_leaders = InlineKeyboardButton(text="🏆 Лидеры", callback_data="nav_leaders")
    else:
        btn_leaders = InlineKeyboardButton(text="🔒 Лидеры (Ур.2)", callback_data="locked_2")
        
    if user_lvl >= 3:
        btn_games = InlineKeyboardButton(text="🎮 Игры", callback_data="nav_games")
    else:
        btn_games = InlineKeyboardButton(text="🔒 Игры (Ур.3)", callback_data="locked_3")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn_leaders, btn_games]
    ])
    return kb

# --- CALLBACK HANDLERS (МЕНЮ ПРОФИЛЯ) ---

@router.callback_query(F.data == "nav_profile")
async def cb_back_profile(callback: CallbackQuery):
    text, _ = await generate_profile_content(callback.from_user.id)
    
    caller_data = await get_user(callback.from_user.id)
    lvl = caller_data[4]
    db_level = caller_data[6]
    
    eff_lvl = await get_effective_level(callback.from_user.id, callback.message.chat, db_level)
    final_lvl = eff_lvl if eff_lvl > lvl else lvl
    
    markup = get_profile_keyboard(final_lvl)
    
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()

@router.callback_query(F.data == "nav_leaders")
async def cb_leaders(callback: CallbackQuery):
    user_data = await get_user(callback.from_user.id)
    lvl = user_data[4]
    db_level = user_data[6]
    
    eff_lvl = await get_effective_level(callback.from_user.id, callback.message.chat, db_level)
    
    if eff_lvl < 2 and lvl < 2:
        return await callback.answer("Нужен уровень 2!", show_alert=True)
        
    text = await generate_leaders_text(callback.from_user.id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="nav_profile")]
    ])
    
    from aiogram.types import InputMediaPhoto
    try:
        if callback.message.photo:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=IMG_HELP_LEADERS, caption=text, parse_mode="HTML"),
                reply_markup=kb
            )
        else:
            await callback.message.answer_photo(photo=IMG_HELP_LEADERS, caption=text, parse_mode="HTML", reply_markup=kb)
            await callback.message.delete()
    except Exception:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
            
    await callback.answer()

@router.callback_query(F.data == "nav_games")
async def cb_games(callback: CallbackQuery):
    user_data = await get_user(callback.from_user.id)
    lvl = user_data[4]
    db_level = user_data[6]
    
    eff_lvl = await get_effective_level(callback.from_user.id, callback.message.chat, db_level)
    
    if eff_lvl < 3 and lvl < 3:
        return await callback.answer("Нужен уровень 3!", show_alert=True)
    
    uid = callback.from_user.id
    final_lvl = eff_lvl if eff_lvl > lvl else lvl
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            get_game_btn_simple('dice', final_lvl, "🎲 Кости", "game_menu_dice", uid),
            get_game_btn_simple('slots', final_lvl, "🎰 Слоты", "game_menu_slots", uid)
        ],
        [
            get_game_btn_simple('basketball', final_lvl, "🏀 Баскет", "game_menu_basket", uid),
            get_game_btn_simple('duel', final_lvl, "🔫 Дуэль", "game_info_duel", uid)
        ],
        [InlineKeyboardButton(text="🔙 В профиль", callback_data="nav_profile")]
    ])
    
    text = (
        f"🕹️<b>Список игр</b>\n\n"
        f"👤 Пользователь: <b>{callback.from_user.full_name}</b>\n"
        f"💳 Баланс: <code>{format_xp(user_data[3])} XP</code>\n\n"
        f"Выбрать игру:"
    )

    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("locked_"))
async def cb_locked(callback: CallbackQuery):
    req_lvl = callback.data.split("_")[1]
    await callback.answer(f"🔒 Этот раздел доступен с {req_lvl} уровня!", show_alert=True)

# --- WIPE (Народный модератор) ---
@router.message(Command("wipe"))
async def cmd_wipe(message: types.Message):
    if not message.reply_to_message:
        return await delete_later(message, 0)
    
    user_data = await get_user(message.from_user.id)
    rpg_lvl = user_data[4]
    mod_lvl = user_data[6]
    
    is_chat_admin = False
    if message.from_user.id == OWNER_ID or mod_lvl >= 5:
        is_chat_admin = True
    elif message.chat.type != 'private':
        try:
            member = await message.chat.get_member(message.from_user.id)
            if member.status in ['administrator', 'creator']:
                is_chat_admin = True
        except: pass
    
    if rpg_lvl < 5 and not is_chat_admin:
        return await delete_later(message, 0)
        
    if not is_chat_admin:
        can_wipe = await check_wipe_cooldown(message.from_user.id)
        if not can_wipe:
            await delete_later(message, 0)
            return await answer_temp(message, "⏳ <b>Команду /wipe можно использовать 1 раз в сутки.</b>")
        
    try:
        await message.reply_to_message.delete()
        await delete_later(message, 0)
        await message.answer(f"🗑 <b>Народный модератор {message.from_user.mention_html()} удалил сообщение!</b>")
    except Exception as e:
        await answer_temp(message, f"❌ Не удалось удалить: {e}")


# --- ПРИВЕТСТВИЕ ---
@router.message(F.new_chat_members)
async def on_user_join(message: types.Message):
    try:
        await message.delete()
    except Exception:
        pass

    chat_id = message.chat.id
    if chat_id in last_welcome_messages:
        old_message_id = last_welcome_messages[chat_id]
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=old_message_id)
        except Exception:
            pass

    new_user = message.new_chat_members[0]
    welcome_text = (
        f"🧩 Привет, {new_user.mention_html()}!\n\n"
        f"Здесь можно обсудить посты с канала, поделиться мыслями и задать вопросы, которые появились по ходу чтения. Несколько простых правил:\n\n"
        f"🚫 <b>Без спама и лишнего флуда</b> — только интересные обсуждения.\n\n"
        f"💬 <b>Уважай других участников</b>, ведь каждый здесь из любви к играм и их созданию.\n\n"
        f"<i>Приятного общения, и добро пожаловать в наше сообщество!</i> 🎮"
    )

    try:
        sent_message = await message.answer_photo(
            photo=IMG_WELCOME,
            caption=welcome_text,
            parse_mode="HTML"
        )
        last_welcome_messages[chat_id] = sent_message.message_id
        await delete_later(sent_message, 600)
    except Exception as e:
        print(f"Ошибка при отправке приветствия: {e}")


# --- ОСНОВНОЙ ХЕНДЛЕР ТЕКСТА (ФАРМ XP + REP) ---
@router.message(F.text & ~F.text.startswith('/'))
async def text_handler(message: types.Message):
    if message.chat.type == 'private': return
    
    user_id = message.from_user.id
    now = time.time()
    text = message.text
    
    # 1. СИСТЕМА РЕПУТАЦИИ (+rep)
    if message.reply_to_message and text.strip().lower() in ["+rep", "+реп", "респект"]:
        giver = await get_user(user_id)
        giver_rpg_lvl = giver[4] # RPG Level
        giver_mod_lvl = giver[6] # Mod Level
        
        is_admin_or_staff = False
        if giver_mod_lvl >= 1 or user_id == OWNER_ID:
             is_admin_or_staff = True
        elif message.chat.type != 'private':
            try:
                member = await message.chat.get_member(user_id)
                if member.status in ['administrator', 'creator']:
                    is_admin_or_staff = True
            except: pass
        
        if giver_rpg_lvl >= 4 or is_admin_or_staff:
            target_id = message.reply_to_message.from_user.id
            result = await give_reputation(user_id, target_id)
            
            if result == "success":
                old, new, added = await update_xp(target_id, 150)
                await message.answer(
                    f"🤝 {message.from_user.mention_html()} повысил репутацию {message.reply_to_message.from_user.mention_html()}!\n"
                    f"Получено <code>+150 XP</code>."
                )
            elif result == "daily_limit_user":
                await answer_temp(message, "⚠️ Вы уже повышали репутацию этому игроку сегодня.")
            elif result == "daily_limit_total":
                await answer_temp(message, "⚠️ Ваш лимит (3 раза в сутки) исчерпан.")
            elif result == "self_rep":
                await answer_temp(message, "🗿 Повышать репутацию самому себе нельзя.")
            return 
        else:
            await answer_temp(message, "🔒 <b>Репутация доступна с 4 уровня!</b>")
            return
            
    # 2. ФАРМ XP
    last_msg = user_last_msg.get(user_id, 0)
    if now - last_msg < 60:
        return
    user_last_msg[user_id] = now
    
    earned_xp = 5
    
    clean_text = ' '.join([w for w in text.split() if not w.startswith('http')])
    if len(clean_text) > 50:
        earned_xp += 10
        
    chat_last = chat_last_active.get(message.chat.id, now)
    if now - chat_last > 3600:
        earned_xp += 50
        # ИЗМЕНЕНО: БОНУС НЕКРОМАНТА -> Бонус за оживление чата
        await message.reply("⚡ <b>Бонус за оживление чата!</b>\n<code>+50 XP</code>!")
    
    chat_last_active[message.chat.id] = now
    
    current_hour = datetime.now().hour
    if 2 <= current_hour < 7:
        earned_xp = int(earned_xp * 1.5)
        
    await get_user(user_id, message.from_user.username, message.from_user.full_name)
    old_lvl, new_lvl, _ = await update_xp(user_id, earned_xp)
    
    # ИЗМЕНЕНО: LEVEL UP/DOWN -> Уровень повышен/понижен
    if new_lvl > old_lvl:
        await message.reply(
            f"🆙 <b>Уровень повышен до {new_lvl}!</b>\n"
            f"{message.from_user.mention_html()} достиг новых высот!"
        )
    elif new_lvl < old_lvl:
        await message.reply(
            f"📉 <b>Уровень понижен до {new_lvl}...</b>\n"
            f"{message.from_user.mention_html()} потерял позиции."
        )

# --- ХЕНДЛЕР КОНТЕНТА (Видео/Фото) ---
@router.message(F.photo | F.video)
async def media_handler(message: types.Message):
    if message.chat.type == 'private': return
    
    user_id = message.from_user.id
    now = time.time()
    
    last_media = media_cooldown.get(user_id, 0)
    
    if now - last_media > 600:
        media_cooldown[user_id] = now
        
        await get_user(user_id, message.from_user.username, message.from_user.full_name)
        
        amount = 15
        current_hour = datetime.now().hour
        if 2 <= current_hour < 7:
            amount = int(amount * 1.5)
            
        old_lvl, new_lvl, _ = await update_xp(user_id, amount)
        
        # ИЗМЕНЕНО: LEVEL UP
        if new_lvl > old_lvl:
            await message.reply(f"🆙 <b>Уровень повышен до {new_lvl}! (Контент-мейкер)</b>")