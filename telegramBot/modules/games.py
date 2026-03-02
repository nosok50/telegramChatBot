# -*- coding: utf-8 -*-
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import get_user, update_xp, get_id_by_username, LEVEL_CAPS
from utils import delete_later, answer_temp
import asyncio
from config import OWNER_ID

router = Router()

ANON_BOT_ID = 1087968824

GAME_REQS = {
    "dice": 1,
    "slots": 2,
    "duel": 2,
    "basketball": 3,
}

active_duels = {}


def fmt_num(num: int) -> str:
    return "{:,}".format(num).replace(",", " ")


async def is_admin_or_owner(user_id: int, chat: types.Chat) -> bool:
    if user_id == OWNER_ID:
        return True
    if user_id in [ANON_BOT_ID, 777000]:
        return True
    if chat.type == "private":
        return False
    try:
        member = await chat.get_member(user_id)
        if member.status in ["creator", "administrator"]:
            return True
    except Exception:
        pass
    return False


def get_game_btn(game_key, user_level, is_admin, title, callback_base, owner_id):
    req_lvl = GAME_REQS.get(game_key, 0)
    if user_level >= req_lvl or is_admin:
        return InlineKeyboardButton(text=title, callback_data=f"{callback_base}:{owner_id}")
    return InlineKeyboardButton(text=f"🔒 {req_lvl} Ур.", callback_data=f"locked_game:{req_lvl}")


def build_games_keyboard(user_level: int, is_admin: bool, owner_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                get_game_btn("dice", user_level, is_admin, "🎲 Кости", "game_menu_dice", owner_id),
                get_game_btn("slots", user_level, is_admin, "🎰 Рулетка", "game_menu_slots", owner_id),
            ],
            [
                get_game_btn("duel", user_level, is_admin, "🔫 Дуэль", "game_info_duel", owner_id),
                get_game_btn("basketball", user_level, is_admin, "🏀 Баскет", "game_menu_basket", owner_id),
            ],
            [InlineKeyboardButton(text="👤 В профиль", callback_data="nav_profile")],
        ]
    )


def build_games_text(player_name: str, level: int, xp: int) -> str:
    return (
        "🕹️ <b>ИГРОВАЯ ЗОНА</b>\n"
        f"👤 Игрок: <b>{player_name}</b>\n\n"
        f"📊 Уровень: <b>{level}</b>\n"
        f"💳 Баланс: <code>{fmt_num(xp)} XP</code>\n\n"
        "Выберите автомат:"
    )


def can_afford(xp, level, bet):
    if xp >= bet:
        return True

    needed = bet - xp
    temp_lvl = level
    while temp_lvl > 1 and needed > 0:
        temp_lvl -= 1
        needed -= LEVEL_CAPS.get(temp_lvl, 500)

    return needed <= 0


@router.message(Command("games"))
async def cmd_games(message: types.Message):
    await delete_later(message, 0)

    user_data = await get_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    if not user_data:
        return

    xp, level = user_data[3], user_data[4]
    uid = message.from_user.id
    is_adm = await is_admin_or_owner(uid, message.chat)

    text = build_games_text(message.from_user.full_name, level, xp)
    kb = build_games_keyboard(level, is_adm, uid)
    await answer_temp(message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("locked_game"))
async def locked_game_alert(callback: types.CallbackQuery):
    req = callback.data.split(":")[1]
    await callback.answer(f"🔒 Эта игра доступна с {req} уровня!", show_alert=True)


@router.callback_query(F.data.startswith("game_menu_"))
async def game_bet_menu(callback: types.CallbackQuery):
    try:
        parts = callback.data.split(":")
        game_name = parts[0].replace("game_menu_", "")
        owner_id = int(parts[1])
    except Exception as e:
        print(f"Error in game_bet_menu: {e}")
        return

    is_owner = callback.from_user.id == owner_id
    is_anon_owner = owner_id == ANON_BOT_ID and await is_admin_or_owner(callback.from_user.id, callback.message.chat)
    if not is_owner and not is_anon_owner:
        return await callback.answer("Это не ваше меню.", show_alert=True)

    ui_conf = {
        "dice": {
            "emoji": "🎲",
            "name": "КОСТИ",
            "desc": "Бросаете кубик. Большие значения побеждают, малые проигрывают.",
            "hint": "Подсказка: 4-6: победа x2, 1-3: проигрыш.",
        },
        "slots": {
            "emoji": "🎰",
            "name": "РУЛЕТКА",
            "desc": "Крутите барабаны и ловите удачную комбинацию символов.",
            "hint": "Подсказка: 777 — x10, BAR/🍇/🍋 в линию — x3, иначе проигрыш.",
        },
        "basket": {
            "emoji": "🏀",
            "name": "БАСКЕТБОЛ",
            "desc": "Бросаете мяч в кольцо: попадание дает выигрыш, промах сжигает ставку.",
            "hint": "Подсказка: промах или отскок — проигрыш, попадание в кольцо — x2, чистый свиш — x3.",
        },
    }
    conf = ui_conf.get(
        game_name, {"emoji": "🎮", "name": "ИГРА", "desc": "Сделайте ставку.", "hint": "Подсказка: удача решает исход."}
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50 XP", callback_data=f"play_{game_name}:50:{owner_id}"),
                InlineKeyboardButton(text="100 XP", callback_data=f"play_{game_name}:100:{owner_id}"),
                InlineKeyboardButton(text="500 XP", callback_data=f"play_{game_name}:500:{owner_id}"),
            ],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_games:{owner_id}")],
        ]
    )

    user_data = await get_user(owner_id)
    curr_xp = fmt_num(user_data[3]) if user_data else "0"
    text = (
        f"{conf['emoji']} <b>{conf['name']}</b>\n"
        f"💰 Баланс: <code>{curr_xp} XP</code>\n\n"
        f"{conf['desc']}\n"
        f"{conf['hint']}\n\n"
        "Сделайте ставку:"
    )

    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("play_"))
async def play_game_logic(callback: types.CallbackQuery):
    try:
        parts = callback.data.split(":")
        game = parts[0].replace("play_", "")
        bet = int(parts[1])
        owner_id = int(parts[2])
    except Exception as e:
        print(f"Error parsing play_game: {e}")
        return

    player_id = None
    player_username = None
    player_fullname = None

    if owner_id == ANON_BOT_ID:
        if await is_admin_or_owner(callback.from_user.id, callback.message.chat):
            player_id = ANON_BOT_ID
            player_username = "GroupAnonymousBot"
            player_fullname = "Group Anonymous Bot"
        else:
            return await callback.answer("Только админы могут играть за чат.", show_alert=True)
    else:
        if callback.from_user.id != owner_id:
            return await callback.answer("Не трогайте чужое меню.", show_alert=True)
        player_id = callback.from_user.id
        player_username = callback.from_user.username
        player_fullname = callback.from_user.full_name

    user_data = await get_user(player_id, player_username, player_fullname)
    if not user_data:
        return await callback.answer("Ошибка профиля.", show_alert=True)
    if not can_afford(user_data[3], user_data[4], bet):
        return await callback.answer(f"Не хватает XP. У вас {fmt_num(user_data[3])} XP.", show_alert=True)

    try:
        await callback.message.delete()
    except Exception:
        pass

    old_lvl_start, new_lvl_start, _ = await update_xp(player_id, -bet)

    emoji_map = {"dice": "🎲", "basket": "🏀", "slots": "🎰"}
    dice_emoji = emoji_map.get(game, "🎲")
    dice_msg = await callback.message.answer_dice(emoji=dice_emoji)
    val = dice_msg.dice.value
    await asyncio.sleep(4 if game != "slots" else 2)

    win_mult = 0
    res_text = ""

    if game == "dice":
        if val >= 4:
            win_mult = 2
            res_text = f"🎲 <b>Победа!</b> Выпало <b>{val}</b>."
        else:
            res_text = f"🎲 <b>Поражение.</b> Выпало <b>{val}</b>."
    elif game == "basket":
        if val <= 2:
            res_text = "🏀 <b>Мимо.</b> Мяч пролетел мимо кольца."
        elif val == 3:
            res_text = "🏀 <b>По дужке.</b> Мяч ударился об кольцо и вылетел."
        elif val == 4:
            win_mult = 2
            res_text = "🏀 <b>Гол!</b> Мяч застрял в кольце."
        elif val == 5:
            win_mult = 3
            res_text = "🔥 <b>Свиш!</b> Чистое попадание."
    elif game == "slots":
        if val == 64:
            win_mult = 10
            res_text = "🎰 <b>Джекпот 777!</b>"
        elif val in [1, 22, 43]:
            win_mult = 3
            res_text = "🎰 <b>Выигрыш!</b> Выпала сильная комбинация."
        else:
            res_text = "🎰 <b>Поражение.</b> Комбинация не сыграла."

    if win_mult > 0:
        win_amt = bet * win_mult
        old_lvl_win, new_lvl_win, _ = await update_xp(player_id, win_amt)
        res_text += f"\n💰 <code>+{fmt_num(win_amt)} XP</code>"
        if new_lvl_win > old_lvl_win:
            res_text += f"\n🆙 <b>Уровень повышен до {new_lvl_win}.</b>"
    else:
        res_text += f"\n💸 <code>-{fmt_num(bet)} XP</code>"
        if new_lvl_start < old_lvl_start:
            res_text += f"\n📉 <b>Уровень понижен до {new_lvl_start}.</b>"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить", callback_data=f"game_menu_{game}:{owner_id}")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data=f"back_to_games:{owner_id}")],
        ]
    )
    await answer_temp(callback.message, res_text, reply_markup=kb, user_id=callback.from_user.id)
    await delete_later(dice_msg, 4)


@router.callback_query(F.data.startswith("game_info_duel"))
async def duel_info_menu(callback: types.CallbackQuery):
    try:
        owner_id = int(callback.data.split(":")[1])
    except Exception:
        return

    is_owner = callback.from_user.id == owner_id
    is_anon_owner = owner_id == ANON_BOT_ID and await is_admin_or_owner(callback.from_user.id, callback.message.chat)
    if not is_owner and not is_anon_owner:
        return await callback.answer("Это не ваше меню.", show_alert=True)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_games:{owner_id}")]]
    )
    text = (
        "🔫 <b>ДУЭЛЬ (PvP)</b>\n\n"
        "Игра между двумя участниками за XP.\n\n"
        "<b>Как работает раунд:</b>\n"
        "• ⚔️ Атака побеждает 🛡 Оборону\n"
        "• 🛡 Оборона побеждает ⚡ Хитрость\n"
        "• ⚡ Хитрость побеждает ⚔️ Атаку\n"
        "• Одинаковый выбор = ничья\n\n"
        "<b>Порядок:</b>\n"
        "1. Вызов: <code>/duel @username [ставка]</code>\n"
        "2. Соперник принимает дуэль\n"
        "3. Оба выбирают тактику\n"
        "4. Победитель получает XP, проигравший теряет XP"
    )
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("back_to_games"))
async def back_to_games(callback: types.CallbackQuery):
    try:
        owner_id = int(callback.data.split(":")[1])
    except Exception:
        return

    is_owner = callback.from_user.id == owner_id
    is_anon_owner = owner_id == ANON_BOT_ID and await is_admin_or_owner(callback.from_user.id, callback.message.chat)
    if not is_owner and not is_anon_owner:
        return await callback.answer("Это не ваше меню.", show_alert=True)

    user_data = await get_user(owner_id)
    xp, level = (user_data[3], user_data[4]) if user_data else (0, 1)
    display_name = user_data[2] if user_data else "Unknown"
    is_adm = await is_admin_or_owner(callback.from_user.id, callback.message.chat)

    text = build_games_text(display_name, level, xp)
    kb = build_games_keyboard(level, is_adm, owner_id)
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("duel"))
async def cmd_duel(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not command.args:
        return await answer_temp(message, "⚠️ Введите <code>/duel @username [ставка]</code>.", reply=True)

    args = command.args.split()
    target_username = args[0]
    try:
        bet = int(args[1])
    except Exception:
        return await answer_temp(message, "⚠️ Ставка должна быть числом.", reply=True)
    if bet < 10:
        return await answer_temp(message, "⚠️ Минимальная ставка: <code>10 XP</code>.", reply=True)

    initiator = message.from_user
    init_data = await get_user(initiator.id, initiator.username, initiator.full_name)
    if not init_data:
        return

    is_adm = await is_admin_or_owner(initiator.id, message.chat)
    if init_data[4] < 2 and not is_adm:
        return await answer_temp(message, "🔒 Дуэли доступны с <b>2 уровня</b>.", reply=True)
    if not can_afford(init_data[3], init_data[4], bet):
        return await answer_temp(message, f"❌ Не хватает XP. У вас: <code>{fmt_num(init_data[3])}</code>.", reply=True)

    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    else:
        target_id = await get_id_by_username(target_username.replace("@", ""))
        target_name = target_username
    if not target_id:
        return await answer_temp(message, "❌ Пользователь не найден в базе.", reply=True)
    if target_id == initiator.id:
        return await answer_temp(message, "🤡 Нельзя вызвать самого себя.", reply=True)

    active_duels[message.chat.id] = {
        "initiator": initiator.id,
        "target": target_id,
        "bet": bet,
        "initiator_name": initiator.full_name,
        "target_name": target_name,
        "state": "waiting_accept",
        "p1_choice": None,
        "p2_choice": None,
    }

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚔️ Принять вызов", callback_data="duel_accept")]]
    )
    text = (
        "🥊 <b>ВЫЗОВ НА ДУЭЛЬ</b>\n"
        f"🔴 <b>{initiator.full_name}</b> VS 🔵 <b>{target_name}</b>\n"
        f"💰 Банк: <code>{fmt_num(bet * 2)} XP</code>\n\n"
        f"Ожидаем подтверждения от {target_name}."
    )
    await answer_temp(message, text, reply_markup=kb)


@router.callback_query(F.data == "duel_accept")
async def duel_accept(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    duel = active_duels.get(chat_id)
    if not duel or duel["state"] != "waiting_accept":
        return await callback.answer("Дуэль уже неактивна.", show_alert=True)

    user_id = callback.from_user.id
    player_id = None
    if user_id == duel["target"]:
        player_id = user_id
    elif duel["target"] == ANON_BOT_ID and await is_admin_or_owner(user_id, callback.message.chat):
        player_id = ANON_BOT_ID
    else:
        return await callback.answer("Этот вызов не вам.", show_alert=True)

    player_name = "Group Anonymous Bot" if player_id == ANON_BOT_ID else callback.from_user.full_name
    player_username = "GroupAnonymousBot" if player_id == ANON_BOT_ID else callback.from_user.username
    target_data = await get_user(player_id, player_username, player_name)
    if not target_data:
        return await callback.answer("Ошибка профиля.", show_alert=True)
    if not can_afford(target_data[3], target_data[4], duel["bet"]):
        return await callback.answer("Не хватает XP для принятия ставки.", show_alert=True)

    duel["state"] = "fighting"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ Атака", callback_data="tactics_atk")],
            [InlineKeyboardButton(text="🛡 Оборона", callback_data="tactics_def")],
            [InlineKeyboardButton(text="⚡ Хитрость", callback_data="tactics_trick")],
        ]
    )
    text = (
        "🔥 <b>ДУЭЛЬ НАЧАЛАСЬ</b>\n"
        f"🔴 {duel['initiator_name']} VS 🔵 {duel['target_name']}\n\n"
        "Выберите тактику. Ваш выбор скрыт до конца раунда."
    )
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tactics_"))
async def duel_tactics(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    duel = active_duels.get(chat_id)
    user_id = callback.from_user.id
    if not duel or duel["state"] != "fighting":
        return await callback.answer("Дуэль неактивна.")

    role = None
    if user_id == duel["initiator"] or (duel["initiator"] == ANON_BOT_ID and await is_admin_or_owner(user_id, callback.message.chat)):
        role = "p1"
    elif user_id == duel["target"] or (duel["target"] == ANON_BOT_ID and await is_admin_or_owner(user_id, callback.message.chat)):
        role = "p2"
    if not role:
        return await callback.answer("Вы не участник дуэли.", show_alert=True)

    choice = callback.data.split("_")[1]
    choice_name = {"atk": "Атака ⚔️", "def": "Оборона 🛡", "trick": "Хитрость ⚡"}.get(choice)

    if role == "p1":
        if duel["p1_choice"]:
            return await callback.answer("Вы уже выбрали.", show_alert=True)
        duel["p1_choice"] = choice
        await callback.answer(f"Выбрано: {choice_name}")
    elif role == "p2":
        if duel["p2_choice"]:
            return await callback.answer("Вы уже выбрали.", show_alert=True)
        duel["p2_choice"] = choice
        await callback.answer(f"Выбрано: {choice_name}")

    if duel["p1_choice"] and duel["p2_choice"]:
        await resolve_duel(callback.message, duel)
    else:
        p1_status = "✅ Готов" if duel["p1_choice"] else "⏳ Думает..."
        p2_status = "✅ Готов" if duel["p2_choice"] else "⏳ Думает..."
        text = (
            "🔥 <b>ДУЭЛЬ ИДЕТ</b>\n"
            f"🔴 {duel['initiator_name']}: <b>{p1_status}</b>\n"
            f"🔵 {duel['target_name']}: <b>{p2_status}</b>\n\n"
            "Ждем выбор обоих игроков."
        )
        try:
            await callback.message.edit_text(text, reply_markup=callback.message.reply_markup)
        except Exception:
            pass


async def resolve_duel(message: types.Message, duel):
    p1 = duel["p1_choice"]
    p2 = duel["p2_choice"]

    winner = None
    if p1 == p2:
        winner = None
    elif p1 == "atk":
        winner = 1 if p2 == "trick" else 2
    elif p1 == "def":
        winner = 1 if p2 == "atk" else 2
    elif p1 == "trick":
        winner = 1 if p2 == "def" else 2

    t_map = {"atk": "Атака ⚔️", "def": "Оборона 🛡", "trick": "Хитрость ⚡"}
    res_text = (
        "🏁 <b>РЕЗУЛЬТАТ ДУЭЛИ</b>\n"
        f"🔴 {duel['initiator_name']}: <b>{t_map[p1]}</b>\n"
        f"🔵 {duel['target_name']}: <b>{t_map[p2]}</b>\n\n"
    )

    if winner is None:
        res_text += "🤝 <b>Ничья.</b> Ставки возвращены."
    else:
        w_id = duel["initiator"] if winner == 1 else duel["target"]
        l_id = duel["target"] if winner == 1 else duel["initiator"]
        w_name = duel["initiator_name"] if winner == 1 else duel["target_name"]
        l_name = duel["target_name"] if winner == 1 else duel["initiator_name"]

        old_lvl_w, new_lvl_w, _ = await update_xp(w_id, duel["bet"])
        old_lvl_l, new_lvl_l, _ = await update_xp(l_id, -duel["bet"])

        flavor = ""
        combo = (p1, p2) if winner == 1 else (p2, p1)
        if combo == ("atk", "trick"):
            flavor = "Силовая атака пробила хитрый маневр."
        if combo == ("def", "atk"):
            flavor = "Грамотная защита остановила атаку."
        if combo == ("trick", "def"):
            flavor = "Хитрость обошла защиту."

        res_text += (
            f"🏆 <b>Победитель: {w_name}</b>\n"
            f"💬 <i>{flavor}</i>\n"
            f"💰 <b>{w_name}</b>: <code>+{fmt_num(duel['bet'])} XP</code>\n"
            f"💀 <b>{l_name}</b>: <code>-{fmt_num(duel['bet'])} XP</code>\n"
        )
        if new_lvl_w > old_lvl_w:
            res_text += f"🆙 <b>{w_name}</b> повысил уровень до {new_lvl_w}.\n"
        if new_lvl_l < old_lvl_l:
            res_text += f"📉 <b>{l_name}</b> понизил уровень до {new_lvl_l}."

    active_duels.pop(message.chat.id, None)
    sent_msg = await message.edit_text(res_text, reply_markup=None)
    await delete_later(sent_msg, 60)
