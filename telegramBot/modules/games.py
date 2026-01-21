from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import get_user, update_xp, get_id_by_username, LEVEL_CAPS
from utils import delete_later
import asyncio
import random
from config import OWNER_ID

router = Router()

# ID Анонимного бота Telegram
ANON_BOT_ID = 1087968824

# Настройки уровней для доступа к играм
GAME_REQS = {
    'dice': 3,
    'slots': 3,
    'basketball': 4,
    'duel': 4
}

# Хранилище активных дуэлей
active_duels = {}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def fmt_num(num):
    """Форматирует число: 10000 -> 10.000"""
    return "{:,}".format(num).replace(",", ".")

async def is_admin_or_owner(user_id, chat):
    if user_id == OWNER_ID: return True
    if user_id in [ANON_BOT_ID, 777000]: return True
    if chat.type == 'private': return False
    try:
        member = await chat.get_member(user_id)
        if member.status in ['creator', 'administrator']:
            return True
    except: pass
    return False

def get_game_btn(game_key, user_level, is_admin, title, callback_base, owner_id):
    """Генерирует кнопку игры или замок, если уровень мал"""
    req_lvl = GAME_REQS.get(game_key, 0)
    
    if user_level >= req_lvl or is_admin:
        return InlineKeyboardButton(text=title, callback_data=f"{callback_base}:{owner_id}")
    else:
        # Если уровень мал - показываем замок
        return InlineKeyboardButton(text=f"🔒 {req_lvl} Ур.", callback_data=f"locked_game:{req_lvl}")

def can_afford(xp, level, bet):
    """
    Проверяет, может ли игрок позволить себе ставку,
    учитывая возможность понижения уровня.
    """
    # 1. Если хватает текущего XP - отлично
    if xp >= bet: 
        return True
    
    # 2. Если не хватает, проверяем, хватит ли спуска по уровням
    needed = bet - xp
    temp_lvl = level
    
    # Симулируем понижение уровня
    while temp_lvl > 1 and needed > 0:
        temp_lvl -= 1
        # При падении на уровень ниже мы получаем его емкость (cap)
        # Например, падая с 2 на 1, мы получаем 500 XP (кап 1 уровня)
        gain = LEVEL_CAPS.get(temp_lvl, 500)
        needed -= gain
        
    # Если долг покрыт (needed <= 0), значит играть можно
    if needed <= 0:
        return True
        
    return False

# --- ГЛАВНОЕ МЕНЮ ИГР ---
@router.message(Command("games"))
async def cmd_games(message: types.Message):
    await delete_later(message, 0)
    
    # Регистрируем/обновляем юзера
    user_data = await get_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    if not user_data: return
    
    xp, level = user_data[3], user_data[4]
    uid = message.from_user.id
    is_adm = await is_admin_or_owner(uid, message.chat)

    # Формируем клавиатуру
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            get_game_btn('dice', level, is_adm, "🎲 Кости", "game_menu_dice", uid),
            get_game_btn('slots', level, is_adm, "🎰 Слоты", "game_menu_slots", uid)
        ],
        [
            get_game_btn('basketball', level, is_adm, "🏀 Баскет", "game_menu_basket", uid),
            get_game_btn('duel', level, is_adm, "🔫 Дуэль", "game_info_duel", uid)
        ],
        # Добавляем кнопку В Профиль
        [InlineKeyboardButton(text="👤 В профиль", callback_data="nav_profile")]
    ])
    
    text = (
        f"🕹 <b>ИГРОВАЯ ЗОНА</b>\n"
        f"👤 Игрок: <b>{message.from_user.full_name}</b>\n"
        f"💳 Баланс: <code>{fmt_num(xp)} XP</code>\n"
        f"📊 Уровень: <b>{level}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Выберите автомат:"
    )

    msg = await message.answer(text, reply_markup=kb)
    # Удаляем меню через 60 сек, если неактивно
    await delete_later(msg, 60)

# --- ОБРАБОТЧИК ЗАБЛОКИРОВАННЫХ ИГР ---
@router.callback_query(F.data.startswith("locked_game"))
async def locked_game_alert(callback: types.CallbackQuery):
    req = callback.data.split(":")[1]
    await callback.answer(f"🔒 Эта игра доступна с {req} уровня!", show_alert=True)

# --- УНИВЕРСАЛЬНОЕ МЕНЮ СТАВОК ---
@router.callback_query(F.data.startswith("game_menu_"))
async def game_bet_menu(callback: types.CallbackQuery):
    try:
        # Формат callback: game_menu_dice:123
        parts = callback.data.split(":")
        game_name = parts[0].replace("game_menu_", "") # dice
        owner_id = int(parts[1])
    except Exception as e:
        print(f"Error in game_bet_menu: {e}")
        return

    is_owner = (callback.from_user.id == owner_id)
    is_anon_owner = (owner_id == ANON_BOT_ID) and await is_admin_or_owner(callback.from_user.id, callback.message.chat)

    if not is_owner and not is_anon_owner:
        return await callback.answer("Это не ваш стол!", show_alert=True)

    # Настройки отображения
    ui_conf = {
        'dice': {'emoji': '🎲', 'name': 'КОСТИ'},
        'slots': {'emoji': '🎰', 'name': 'СЛОТЫ'},
        'basket': {'emoji': '🏀', 'name': 'БАСКЕТБОЛ'}
    }
    conf = ui_conf.get(game_name, {'emoji': '🎮', 'name': 'ИГРА'})

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="50 XP", callback_data=f"play_{game_name}:50:{owner_id}"),
            InlineKeyboardButton(text="100 XP", callback_data=f"play_{game_name}:100:{owner_id}"),
            InlineKeyboardButton(text="500 XP", callback_data=f"play_{game_name}:500:{owner_id}")
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_games:{owner_id}")]
    ])
    
    # Получаем актуальный баланс для отображения
    user_data = await get_user(owner_id) 
    curr_xp = fmt_num(user_data[3]) if user_data else "0"

    text = (
        f"{conf['emoji']} <b>{conf['name']}</b>\n"
        f"💰 Баланс: <code>{curr_xp} XP</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Сделайте вашу ставку:"
    )
    
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)

# --- ЛОГИКА ИГР (Dice, Basket, Slots) ---
@router.callback_query(F.data.startswith("play_"))
async def play_game_logic(callback: types.CallbackQuery):
    # format: play_dice:50:123
    try:
        parts = callback.data.split(":")
        game = parts[0].replace("play_", "")
        bet = int(parts[1])
        owner_id = int(parts[2])
    except Exception as e: 
        print(f"Error parsing play_game: {e}")
        return

    # 1. Определение игрока (фикс для Анонима)
    player_id = None
    player_username = None
    player_fullname = None

    if owner_id == ANON_BOT_ID:
        if await is_admin_or_owner(callback.from_user.id, callback.message.chat):
            player_id = ANON_BOT_ID
            player_username = "GroupAnonymousBot"
            player_fullname = "Group Anonymous Bot"
        else:
            return await callback.answer("Только админы могут играть за чат!", show_alert=True)
    else:
        if callback.from_user.id != owner_id:
            return await callback.answer("Не трогай чужой стол!", show_alert=True)
        player_id = callback.from_user.id
        player_username = callback.from_user.username
        player_fullname = callback.from_user.full_name

    # 2. Проверка баланса
    user_data = await get_user(player_id, player_username, player_fullname)
    if not user_data: return await callback.answer("Ошибка профиля", show_alert=True)
    
    # ИСПОЛЬЗУЕМ НОВУЮ ФУНКЦИЮ ПРОВЕРКИ
    if not can_afford(user_data[3], user_data[4], bet):
        return await callback.answer(f"Не хватает XP! У вас {fmt_num(user_data[3])} XP (и уровней не хватает для покрытия)", show_alert=True)

    # 3. Удаляем меню ставок (чтобы не висело)
    try: await callback.message.delete()
    except: pass

    # 4. Списываем ставку
    # Тут возвращаются (старый уровень, новый уровень, изменение)
    old_lvl_start, new_lvl_start, _ = await update_xp(player_id, -bet)
    
    # 5. Бросок кубика
    emoji_map = {'dice': '🎲', 'basket': '🏀', 'slots': '🎰'}
    dice_emoji = emoji_map.get(game, '🎲')
    
    dice_msg = await callback.message.answer_dice(emoji=dice_emoji)
    val = dice_msg.dice.value
    
    # Ждем анимацию
    sleep_time = 4 if game != 'slots' else 2 # Слоты быстрее
    await asyncio.sleep(sleep_time)
    
    # 6. Расчет результата
    win_mult = 0
    res_text = ""
    
    # --- ЛОГИКА КОСТЕЙ ---
    if game == 'dice':
        # 1-3 lose, 4-6 win x2
        if val >= 4:
            win_mult = 2
            res_text = f"🎲 <b>Победа!</b> Выпало <b>{val}</b>"
        else:
            res_text = f"🎲 <b>Проигрыш.</b> Выпало <b>{val}</b>"

    # --- ЛОГИКА БАСКЕТБОЛА (ИСПРАВЛЕНА) ---
    elif game == 'basket':
        # 1-2: Полный промах (Мимо)
        # 3: Удар об дужку и вылет (По дужке)
        # 4: Застрял (Гол) - Победа
        # 5: Чистый (Свиш) - Победа
        if val <= 2:
            res_text = "🏀 <b>Мимо...</b> Мяч пролетел мимо кольца."
        elif val == 3:
            res_text = "🏀 <b>По дужке!</b> Мяч ударился и вылетел."
        elif val == 4:
            win_mult = 2 # Застрял - это гол (конфетти есть)
            res_text = "🏀 <b>ГОЛ!</b> Мяч застрял в кольце!"
        elif val == 5:
            win_mult = 3
            res_text = "🔥 <b>СВИШ!</b> Чистое попадание!"

    # --- ЛОГИКА СЛОТОВ ---
    elif game == 'slots':
        # 1(bar), 22(berry), 43(lemon) -> x3, 64(777) -> x10
        if val == 64:
            win_mult = 10
            res_text = "🎰 <b>ДЖЕКПОТ!!! (777)</b>"
        elif val in [1, 22, 43]:
            win_mult = 3
            res_text = "🎰 <b>ВЫИГРЫШ!</b> Три в ряд!"
        else:
            res_text = "🎰 <b>Мимо...</b> Попробуй еще раз."

    # 7. Начисление выигрыша
    if win_mult > 0:
        win_amt = bet * win_mult
        old_lvl_win, new_lvl_win, _ = await update_xp(player_id, win_amt)
        res_text += f"\n💰 <code>+{fmt_num(win_amt)} XP</code>"
        
        # Проверка Level Up
        if new_lvl_win > old_lvl_win:
             res_text += f"\n🆙 <b>Уровень повышен до {new_lvl_win}!</b>"
    else:
        res_text += f"\n💸 <code>-{fmt_num(bet)} XP</code>"
        # Проверяем Level Down после списания ставки
        if new_lvl_start < old_lvl_start:
             res_text += f"\n📉 <b>Уровень понижен до {new_lvl_start}...</b>"

    # 8. Финальное сообщение (Меню повтора)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Повторить", callback_data=f"game_menu_{game}:{owner_id}")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data=f"back_to_games:{owner_id}")]
    ])
    
    result_msg = await callback.message.answer(res_text, reply_markup=kb)
    
    # Удаляем кубик чуть позже, чтобы юзер увидел результат на кубике
    await delete_later(dice_msg, 4)
    # Удаляем результат через 60 сек
    await delete_later(result_msg, 60)


# --- ДУЭЛИ (Инфо и Лобби) ---
@router.callback_query(F.data.startswith("game_info_duel"))
async def duel_info_menu(callback: types.CallbackQuery):
    try: owner_id = int(callback.data.split(":")[1])
    except: return

    # Проверка прав (как везде)
    is_owner = (callback.from_user.id == owner_id)
    is_anon_owner = (owner_id == ANON_BOT_ID) and await is_admin_or_owner(callback.from_user.id, callback.message.chat)
    if not is_owner and not is_anon_owner: return await callback.answer("Это не ваше меню!", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_games:{owner_id}")]
    ])
    
    text = (
        "🔫 <b>ДУЭЛЬ (PvP)</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Сражение с другим игроком за XP.\n"
        "Система: <b>Камень-Ножницы-Бумага</b>\n\n"
        "1️⃣ Вызови: <code>/duel @username [ставка]</code>\n"
        "2️⃣ Соперник принимает вызов.\n"
        "3️⃣ Выбираете тактику.\n\n"
        "⚔️ <b>Атака</b> побеждает Хитрость\n"
        "🛡 <b>Оборона</b> побеждает Атаку\n"
        "⚡️ <b>Хитрость</b> побеждает Оборону"
    )
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)

# --- КНОПКА НАЗАД В ГЛАВНОЕ МЕНЮ ---
@router.callback_query(F.data.startswith("back_to_games"))
async def back_to_games(callback: types.CallbackQuery):
    try: owner_id = int(callback.data.split(":")[1])
    except: return
    
    # Проверка прав
    is_owner = (callback.from_user.id == owner_id)
    is_anon_owner = (owner_id == ANON_BOT_ID) and await is_admin_or_owner(callback.from_user.id, callback.message.chat)
    if not is_owner and not is_anon_owner: return await callback.answer("Это не ваше меню!", show_alert=True)

    # Получаем данные владельца меню
    user_data = await get_user(owner_id)
    xp, level = (user_data[3], user_data[4]) if user_data else (0, 0)
    
    is_adm = await is_admin_or_owner(callback.from_user.id, callback.message.chat)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            get_game_btn('dice', level, is_adm, "🎲 Кости", "game_menu_dice", owner_id),
            get_game_btn('slots', level, is_adm, "🎰 Слоты", "game_menu_slots", owner_id)
        ],
        [
            get_game_btn('basketball', level, is_adm, "🏀 Баскет", "game_menu_basket", owner_id),
            get_game_btn('duel', level, is_adm, "🔫 Дуэль", "game_info_duel", owner_id)
        ],
        # Добавляем кнопку В Профиль
        [InlineKeyboardButton(text="👤 В профиль", callback_data="nav_profile")]
    ])
    
    text = (
        f"🕹 <b>ИГРОВАЯ ЗОНА</b>\n"
        f"👤 Игрок: <b>{user_data[2] if user_data else 'Unknown'}</b>\n"
        f"💳 Баланс: <code>{fmt_num(xp)} XP</code>\n"
        f"📊 Уровень: <b>{level}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Выберите автомат:"
    )
    
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)

# --- ЛОГИКА ДУЭЛЕЙ (КОМАНДЫ) ---
@router.message(Command("duel"))
async def cmd_duel(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    
    # Валидация аргументов
    if not command.args:
        msg = await message.reply("⚠️ <b>Ошибка:</b> Введите <code>/duel @username [ставка]</code>")
        return await delete_later(msg, 10)
    
    args = command.args.split()
    target_username = args[0]
    try: bet = int(args[1])
    except: 
        msg = await message.reply("⚠️ <b>Ошибка:</b> Ставка должна быть числом.")
        return await delete_later(msg, 10)
        
    if bet < 10: 
        msg = await message.reply("⚠️ Минимальная ставка: <code>10 XP</code>")
        return await delete_later(msg, 10)

    # Инициатор
    initiator = message.from_user
    init_data = await get_user(initiator.id, initiator.username, initiator.full_name)
    if not init_data: return
    
    is_adm = await is_admin_or_owner(initiator.id, message.chat)

    # Проверка уровня (4 для дуэли)
    if init_data[4] < 4 and not is_adm: 
        msg = await message.reply("🔒 Дуэли доступны с <b>4 уровня</b>!")
        return await delete_later(msg, 10)
    
    # НОВАЯ ПРОВЕРКА БАЛАНСА
    if not can_afford(init_data[3], init_data[4], bet):
        msg = await message.reply(f"❌ <b>Не хватает XP (даже с учетом уровней)!</b> У вас: <code>{fmt_num(init_data[3])}</code>")
        return await delete_later(msg, 10)

    # Поиск цели
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    else:
        target_id = await get_id_by_username(target_username.replace("@", ""))
        target_name = target_username
        
    if not target_id:
        msg = await message.reply("❌ Пользователь не найден в базе.")
        return await delete_later(msg, 10)
        
    if target_id == initiator.id:
        msg = await message.reply("🤡 Нельзя вызвать самого себя.")
        return await delete_later(msg, 10)

    # Создание дуэли
    active_duels[message.chat.id] = {
        'initiator': initiator.id,
        'target': target_id,
        'bet': bet,
        'initiator_name': initiator.full_name,
        'target_name': target_name,
        'state': 'waiting_accept',
        'p1_choice': None,
        'p2_choice': None
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ ПРИНЯТЬ ВЫЗОВ", callback_data="duel_accept")]
    ])
    
    text = (
        f"🥊 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n"
        f"🔴 <b>{initiator.full_name}</b> VS 🔵 <b>{target_name}</b>\n"
        f"💰 Банк: <code>{fmt_num(bet*2)} XP</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Ждем подтверждения от {target_name}..."
    )
    
    msg = await message.answer(text, reply_markup=kb)
    await delete_later(msg, 120) # 2 минуты на принятие

@router.callback_query(F.data == "duel_accept")
async def duel_accept(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    duel = active_duels.get(chat_id)
    
    if not duel or duel['state'] != 'waiting_accept':
        return await callback.answer("⏳ Дуэль истекла.", show_alert=True)
    
    user_id = callback.from_user.id
    
    # Логика определения "Кто принял" (для Анонима)
    player_id = None
    if user_id == duel['target']:
        player_id = user_id
    elif duel['target'] == ANON_BOT_ID and await is_admin_or_owner(user_id, callback.message.chat):
        player_id = ANON_BOT_ID
    else:
        return await callback.answer("🛑 Это вызов не вам!", show_alert=True)
    
    # Проверка баланса цели
    player_name = "Group Anonymous Bot" if player_id == ANON_BOT_ID else callback.from_user.full_name
    player_username = "GroupAnonymousBot" if player_id == ANON_BOT_ID else callback.from_user.username
    
    target_data = await get_user(player_id, player_username, player_name)
    if not target_data: return await callback.answer("Ошибка профиля", show_alert=True)
    
    # НОВАЯ ПРОВЕРКА БАЛАНСА
    if not can_afford(target_data[3], target_data[4], duel['bet']):
        return await callback.answer("❌ У вас не хватает XP (и уровней) для ставки!", show_alert=True)
        
    duel['state'] = 'fighting'
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Атака", callback_data="tactics_atk")],
        [InlineKeyboardButton(text="🛡 Оборона", callback_data="tactics_def")],
        [InlineKeyboardButton(text="⚡️ Хитрость", callback_data="tactics_trick")]
    ])
    
    text = (
        f"🔥 <b>БОЙ НАЧАЛСЯ!</b>\n"
        f"🔴 {duel['initiator_name']} VS 🔵 {duel['target_name']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Выберите тактику (Ваш выбор скрыт):"
    )
    
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data.startswith("tactics_"))
async def duel_tactics(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    duel = active_duels.get(chat_id)
    user_id = callback.from_user.id
    
    if not duel or duel['state'] != 'fighting':
        return await callback.answer("Бой не активен.")
        
    # Кто нажал?
    role = None
    if user_id == duel['initiator'] or (duel['initiator'] == ANON_BOT_ID and await is_admin_or_owner(user_id, callback.message.chat)):
        role = 'p1'
    elif user_id == duel['target'] or (duel['target'] == ANON_BOT_ID and await is_admin_or_owner(user_id, callback.message.chat)):
        role = 'p2'

    if not role: return await callback.answer("Вы зритель, не мешайте!", show_alert=True)
        
    choice = callback.data.split("_")[1] # atk, def, trick
    choice_name = {'atk': 'Атака ⚔️', 'def': 'Оборона 🛡', 'trick': 'Хитрость ⚡️'}.get(choice)

    # Сохраняем выбор
    if role == 'p1':
        if duel['p1_choice']: return await callback.answer("Вы уже выбрали!", show_alert=True)
        duel['p1_choice'] = choice
        await callback.answer(f"Выбрано: {choice_name}")
    elif role == 'p2':
        if duel['p2_choice']: return await callback.answer("Вы уже выбрали!", show_alert=True)
        duel['p2_choice'] = choice
        await callback.answer(f"Выбрано: {choice_name}")

    # Проверка, все ли выбрали
    if duel['p1_choice'] and duel['p2_choice']:
        await resolve_duel(callback.message, duel)
    else:
        # Обновляем текст, показывая, кто готов
        p1_status = "✅ Готов" if duel['p1_choice'] else "⏳ Думает..."
        p2_status = "✅ Готов" if duel['p2_choice'] else "⏳ Думает..."
        
        text = (
            f"🔥 <b>БОЙ ИДЕТ!</b>\n"
            f"🔴 {duel['initiator_name']}: <b>{p1_status}</b>\n"
            f"🔵 {duel['target_name']}: <b>{p2_status}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Выберите тактику:"
        )
        try: await callback.message.edit_text(text, reply_markup=callback.message.reply_markup)
        except: pass

async def resolve_duel(message: types.Message, duel):
    p1 = duel['p1_choice']
    p2 = duel['p2_choice']
    
    # Логика: Atk > Trick > Def > Atk
    winner = None 
    if p1 == p2:
        winner = None # Ничья
    elif p1 == "atk":
        winner = 1 if p2 == "trick" else 2 # Атака бьет Хитрость, но проигрывает Обороне
    elif p1 == "def":
        winner = 1 if p2 == "atk" else 2 # Оборона бьет Атаку, но проигрывает Хитрости
    elif p1 == "trick":
        winner = 1 if p2 == "def" else 2 # Хитрость бьет Оборону, но проигрывает Атаке
        
    t_map = {'atk': 'Атака ⚔️', 'def': 'Оборона 🛡', 'trick': 'Хитрость ⚡️'}
    
    header = "🏁 <b>РЕЗУЛЬТАТ ДУЭЛИ</b>"
    
    res_text = (
        f"{header}\n"
        f"🔴 {duel['initiator_name']}: <b>{t_map[p1]}</b>\n"
        f"🔵 {duel['target_name']}: <b>{t_map[p2]}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    
    if winner is None:
        res_text += "🤝 <b>НИЧЬЯ!</b>\nСилы равны. Ставки возвращены."
    else:
        w_id = duel['initiator'] if winner == 1 else duel['target']
        l_id = duel['target'] if winner == 1 else duel['initiator']
        w_name = duel['initiator_name'] if winner == 1 else duel['target_name']
        l_name = duel['target_name'] if winner == 1 else duel['initiator_name']
        
        # Обмен опытом с проверкой уровней
        old_lvl_w, new_lvl_w, _ = await update_xp(w_id, duel['bet'])
        old_lvl_l, new_lvl_l, _ = await update_xp(l_id, -duel['bet'])
        
        # Описание победы
        flavor = ""
        combo = (p1, p2) if winner == 1 else (p2, p1)
        if combo == ('atk', 'trick'): flavor = "Грубая сила проломила хитрый план!"
        if combo == ('def', 'atk'): flavor = "Идеальная защита измотала врага!"
        if combo == ('trick', 'def'): flavor = "Ловкий маневр обошел защиту!"
        
        res_text += (
            f"🏆 <b>ПОБЕДИТЕЛЬ: {w_name}</b>\n"
            f"💭 <i>{flavor}</i>\n\n"
            f"💰 Выигрыш: <code>+{fmt_num(duel['bet'])} XP</code>\n"
        )
        if new_lvl_w > old_lvl_w:
            res_text += f"🆙 <b>{w_name} повысил уровень до {new_lvl_w}!</b>\n"
            
        res_text += f"💀 {l_name}: <code>-{fmt_num(duel['bet'])} XP</code>\n"
        
        if new_lvl_l < old_lvl_l:
            res_text += f"📉 <b>{l_name} потерял уровень ({new_lvl_l})...</b>"
        
    active_duels.pop(message.chat.id, None)
    
    # Сообщение результата (удаляется через минуту)
    sent_msg = await message.edit_text(res_text, reply_markup=None)
    await delete_later(sent_msg, 60)