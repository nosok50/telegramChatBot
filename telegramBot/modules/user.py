# -*- coding: utf-8 -*-
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile,
)
from database import (
    get_user, update_xp, get_warn_reasons, get_id_by_username, 
    LEVEL_CAPS, give_reputation, check_wipe_cooldown,
    get_top_users, get_user_rank, get_all_staff,
    get_free_dice_remaining, claim_free_dice,
    register_sync_activity,
    get_month_score, get_month_leaders, get_month_wins, get_previous_month_title,
    set_human_verification_pending, set_human_verification_message,
    complete_human_verification, get_human_verification,
    get_pending_human_verifications, update_human_verification_input,
    fail_human_verification_attempt,
    get_chat_verification_access, migrate_existing_chat_access,
    revoke_chat_verification_access,
)
from engagement import process_chat_activity
from level_tags import ensure_level_tag
from modules.factory_orders import track_text_message, track_media_message, get_factory_order_stats
from config import WARN_LIMIT, OWNER_ID
from utils import (
    delete_later,
    answer_temp,
    delete_temp_by_scope,
    get_user_link,
    bump_sticky_message_counter,
    replace_sticky_message,
    is_anonymous_admin_message,
    moderation_help_text,
)
import time
import asyncio
import io
import secrets
import random
import logging
from datetime import datetime
from contextlib import nullcontext
from PIL import Image, ImageDraw, ImageFont
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

router = Router()

# КЕШИ
# Прогресс активности хранится в SQLite, поэтому перезапуск не обнуляет волны
# и не позволяет повторно получать XP за один и тот же контент.

# URL КАРТИНОК
IMG_LEVEL_3 = "https://i.ibb.co/S45s7p2D/Frame-26085979.png"
IMG_LEVEL_4 = "https://i.ibb.co/KjQGJMKL/Frame-26085980.png"
IMG_LEVEL_5 = "https://i.ibb.co/9HCSx0g2/Frame-26085981.png"
IMG_LEVEL_1 = "https://i.ibb.co/v69WY9bc/Frame-26086039.png"
IMG_LEVEL_2 = "https://i.ibb.co/TM2Np5Nc/Frame-26086040.png"
IMG_HELP_LEADERS = "https://i.ibb.co/JwC8C58d/Frame-26085985.png"
IMG_WELCOME = "https://i.ibb.co/Q3GG72fN/Frame-26085986.png"

CAPTCHA_LENGTH = 5
CAPTCHA_TTL_SECONDS = 180
CAPTCHA_MAX_ATTEMPTS = 3
_captcha_timeout_tasks = {}
_captcha_random = secrets.SystemRandom()
_captcha_locks = {}
_captcha_view_tasks = {}
_captcha_views = {}
_captcha_view_next_at = {}
_verification_log = logging.getLogger("human_verification")


def _captcha_lock(chat_id: int, user_id: int):
    return _captcha_locks.setdefault((chat_id, user_id), asyncio.Lock())


def _captcha_task_key(chat_id: int, user_id: int):
    return chat_id, user_id


def _cancel_captcha_timeout(chat_id: int, user_id: int):
    key = _captcha_task_key(chat_id, user_id)
    view_task = _captcha_view_tasks.pop(key, None)
    _captcha_views.pop(key, None)
    _captcha_view_next_at.pop(key, None)
    if view_task and not view_task.done():
        view_task.cancel()
    task = _captcha_timeout_tasks.pop(key, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


def _captcha_keyboard(token: str) -> InlineKeyboardMarkup:
    digits = list("0123456789")
    # Keep the original shuffled layout fixed throughout one challenge.
    random.Random(token).shuffle(digits)
    rows = [
        [InlineKeyboardButton(text=digit, callback_data=f"captcha:{token}:digit:{digit}")
         for digit in digits[index:index + 5]]
        for index in range(0, 10, 5)
    ]
    rows.append([
        InlineKeyboardButton(text="⌫ Стереть", callback_data=f"captcha:{token}:back"),
        InlineKeyboardButton(text="✅ Готово", callback_data=f"captcha:{token}:submit"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _captcha_font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_captcha_png(code: str) -> bytes:
    """Generate a readable but non-machine-trivial numeric CAPTCHA in memory."""
    width, height = 420, 150
    image = Image.new("RGB", (width, height), (242, 245, 250))
    draw = ImageDraw.Draw(image)

    for _ in range(18):
        color = tuple(_captcha_random.randint(125, 215) for _ in range(3))
        draw.line(
            (
                _captcha_random.randint(0, width), _captcha_random.randint(0, height),
                _captcha_random.randint(0, width), _captcha_random.randint(0, height),
            ),
            fill=color,
            width=_captcha_random.randint(1, 3),
        )
    for _ in range(220):
        x, y = _captcha_random.randrange(width), _captcha_random.randrange(height)
        draw.point((x, y), fill=tuple(_captcha_random.randint(90, 220) for _ in range(3)))

    font = _captcha_font(72)
    start_x = 38
    for index, digit in enumerate(code):
        tile = Image.new("RGBA", (78, 110), (255, 255, 255, 0))
        tile_draw = ImageDraw.Draw(tile)
        color = tuple(_captcha_random.randint(15, 90) for _ in range(3)) + (255,)
        tile_draw.text((13, 3), digit, font=font, fill=color)
        tile = tile.rotate(
            _captcha_random.randint(-17, 17),
            resample=Image.Resampling.BICUBIC,
            expand=False,
        )
        image.paste(
            tile,
            (start_x + index * 70, _captcha_random.randint(18, 32)),
            tile,
        )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _captcha_caption(user_mention: str, attempts: int = 0, entered: str = "", notice: str = "") -> str:
    remaining = CAPTCHA_MAX_ATTEMPTS - attempts
    entered = entered[:CAPTCHA_LENGTH]
    slots = " ".join(list(entered) + ["□"] * (CAPTCHA_LENGTH - len(entered)))
    notice_text = f"{notice}\n" if notice else ""
    attempts_text = f"Осталось попыток: <b>{remaining}</b>.\n" if attempts > 0 else ""
    return (
        f"🧩 Привет, {user_mention}!\n\n"
        f"Набери <b>{CAPTCHA_LENGTH} цифр с картинки</b> кнопками ниже и нажми <b>«Готово»</b>.\n\n"
        f"На проверку — 3 минуты.\n"
        f"{attempts_text}\n"
        f"{notice_text}"
        f"<b>Твой ввод:</b> <code>{slots}</code>"
    )


def _schedule_captcha_view(callback, token: str, notice: str = ""):
    """Coalesce rapid keypresses; rendering never blocks input processing."""
    key = _captcha_task_key(callback.message.chat.id, callback.from_user.id)
    _captcha_views[key] = (callback, token, notice)
    task = _captcha_view_tasks.get(key)
    if task is None or task.done():
        _captcha_view_tasks[key] = asyncio.create_task(_render_captcha_view(key))


async def _render_captcha_view(key):
    last_caption = None
    try:
        await asyncio.sleep(0.15)
        while key in _captcha_views:
            await asyncio.sleep(max(0, _captcha_view_next_at.get(key, 0) - time.monotonic()))
            snapshot = _captcha_views[key]
            callback, token, notice = snapshot
            pending = await get_human_verification(*key)
            if not pending or pending.get("challenge_token") != token:
                return
            caption = _captcha_caption(
                callback.from_user.mention_html(),
                int(pending.get("attempts") or 0),
                pending.get("entered_code") or "",
                notice,
            )
            if caption != last_caption:
                try:
                    _captcha_view_next_at[key] = time.monotonic() + 1
                    await callback.bot.edit_message_caption(
                        chat_id=key[0],
                        message_id=pending["welcome_message_id"],
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=_captcha_keyboard(token),
                    )
                except TelegramRetryAfter as error:
                    await asyncio.sleep(error.retry_after)
                    continue
                except TelegramBadRequest as error:
                    if "message is not modified" not in str(error).lower():
                        _verification_log.warning("captcha_view_failed chat=%s user=%s: %s", *key, error)
                        return
                last_caption = caption
            # A newer keypress may have arrived while Telegram was updating the screen.
            if _captcha_views.get(key) is snapshot:
                return
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        return
    except Exception:
        _verification_log.exception("captcha_view_failed chat=%s user=%s", *key)
    finally:
        if _captcha_view_tasks.get(key) is asyncio.current_task():
            _captcha_view_tasks.pop(key, None)
            _captcha_views.pop(key, None)


def _verified_welcome_text(user: types.User) -> str:
    return (
        f"🧩 Привет, {user.mention_html()}!\n\n"
        f"Здесь можно обсудить посты с канала, поделиться мыслями и задать вопросы, которые появились по ходу чтения. Несколько простых правил:\n\n"
        f"🚫 <b>Без спама и лишнего флуда</b> — только интересные обсуждения.\n\n"
        f"💬 <b>Уважай других участников</b>, ведь каждый здесь из любви к играм и их созданию.\n\n"
        f"<i>Приятного общения, и добро пожаловать в наше сообщество!</i> 🎮"
    )


async def _kick_after_failed_captcha(bot, chat_id: int, user_id: int, message_id: int = None):
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except Exception:
        _verification_log.exception("captcha_kick_failed chat=%s user=%s", chat_id, user_id)
        return False

    await complete_human_verification(chat_id, user_id)
    _cancel_captcha_timeout(chat_id, user_id)
    if message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    return True


async def _captcha_timeout(bot, chat_id: int, user_id: int, token: str, expires_at: int):
    try:
        await asyncio.sleep(max(0, expires_at - int(time.time())))
        async with _captcha_lock(chat_id, user_id):
            pending = await get_human_verification(chat_id, user_id)
            if not pending or pending["challenge_token"] != token:
                return
            await _kick_after_failed_captcha(
                bot, chat_id, user_id, pending.get("welcome_message_id"),
            )
    except asyncio.CancelledError:
        return
    finally:
        key = _captcha_task_key(chat_id, user_id)
        if _captcha_timeout_tasks.get(key) is asyncio.current_task():
            _captcha_timeout_tasks.pop(key, None)


def _schedule_captcha_timeout(bot, chat_id: int, user_id: int, token: str, expires_at: int):
    _cancel_captcha_timeout(chat_id, user_id)
    task = asyncio.create_task(_captcha_timeout(bot, chat_id, user_id, token, expires_at))
    _captcha_timeout_tasks[_captcha_task_key(chat_id, user_id)] = task


async def start_human_verification_processor(bot):
    """Restore CAPTCHA expiry timers after a bot restart."""
    await migrate_existing_chat_access()
    for pending in await get_pending_human_verifications():
        token = pending.get("challenge_token") or ""
        if not token:
            # Replace obsolete challenges; deleting their state must not grant access.
            if pending.get("welcome_message_id"):
                try:
                    await bot.delete_message(
                        pending["chat_id"],
                        pending["welcome_message_id"],
                    )
                except Exception:
                    pass
            await complete_human_verification(pending["chat_id"], pending["user_id"])
            await begin_human_verification(
                bot, types.Chat(id=pending["chat_id"], type="supergroup"),
                types.User(id=pending["user_id"], first_name="Участник", is_bot=False),
                source="legacy_challenge",
            )
            continue
        _schedule_captcha_timeout(
            bot,
            pending["chat_id"],
            pending["user_id"],
            token,
            int(pending.get("expires_at") or 0),
        )

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def get_effective_level(user_id: int, chat: types.Chat, db_level: int, sender_chat: types.Chat = None):
    """
    Определяет эффективный уровень (учитывая права админа).
    """
    effective_level = db_level
    
    # 1. Проверка глобальных ID (Владелец, Аноним, Telegram)
    if user_id in [OWNER_ID, 1087968824, 777000]:
        return 5

    if sender_chat and sender_chat.id == chat.id:
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


def can_view_foreign_profile(game_level: int, moderator_level: int) -> bool:
    """Foreign profiles unlock through either game progress or staff rights."""
    return int(game_level or 0) >= 4 or int(moderator_level or 0) >= 2


def format_month_key(key: str) -> str:
    months = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
    try:
        year, month = key.split("-")
        return f"{months[int(month) - 1]} {year}"
    except Exception:
        return key

# Хелпер для кнопки игры
def get_game_btn_simple(game_key, user_level, title, callback_base, owner_id):
    GAME_REQS = {"dice": 1, "slots": 2, "duel": 2, "basketball": 3}
    req_lvl = GAME_REQS.get(game_key, 0)
    
    if user_level >= req_lvl:
        return InlineKeyboardButton(text=title, callback_data=f"{callback_base}:{owner_id}")
    else:
        return InlineKeyboardButton(text=f"🔒 {req_lvl} Ур.", callback_data=f"locked_game:{req_lvl}")


def format_dice_cooldown(seconds: int) -> str:
    if seconds <= 0:
        return "доступен"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м {secs}с"


def help_main_text(user_level: int) -> str:
    return (
        "📚 <b>СПРАВКА И УРОВНИ</b>\n\n"
        "<b>Уровень 1:</b>\n"
        "• <code>/profile</code> — ваш профиль\n"
        "• <code>/games</code> — доступ к игровой зоне\n"
        "• <code>/factory</code> — быстрый вход в управление цехом\n"
        "• 🎲 Кости\n\n"
        "<b>Уровень 2:</b>\n"
        "• <code>/leaders</code> — топ игроков\n"
        "• 🎰 Рулетка\n\n"
        "<b>Уровень 3:</b>\n"
        "• 🏀 Баскетбол\n"
        "• <code>/staff</code> — состав персонала\n\n"
        "<b>Уровень 4:</b>\n"
        "• Ответ <code>+rep</code> — дать репутацию\n"
        "• <code>/profile @username</code> — чужой профиль\n\n"
        "<b>Уровень 5:</b>\n"
        "• Ответ <code>/wipe</code> — удалить сообщение (1 раз в сутки)\n\n"
        f"Ваш уровень: <b>{user_level}</b>"
    )


def help_xp_text() -> str:
    return (
        "💡 <b>КАК ПОЛУЧАТЬ XP</b>\n\n"
        "• Первый содержательный ответ после другого участника: <code>+5 XP</code>\n"
        "• Прямой ответ участнику: <code>+5 XP</code>\n"
        "• Длинное сообщение (50+ символов): <code>+10 XP</code> сверху\n"
        "• Оживление чата после долгой паузы: <code>+50 XP</code>\n"
        "• Фото/видео: <code>+15 XP</code>\n"
        "• Ответ уникального человека на ваш пост: <code>+5 XP</code>\n"
        "• Волна из 4+ участников: каждому <code>+25 XP</code>\n"
        "• Репутация от 4/5 уровня: <code>+150/+250 XP</code>\n"
        "• Повторная репутация той же парой за 7 дней: <code>+25 XP</code>\n"
        "• Бесплатный кубик (раз в 12 часов): шанс <code>+50 XP</code>\n\n"
        "Лимита сообщений в день нет. Копии, одни ссылки и эмодзи XP не дают.\n"
        "XP может как повышать, так и понижать уровень в играх и дуэлях."
    )


def help_keyboard(can_moderate: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💡 Как получать XP", callback_data="help_xp")]]
    if can_moderate:
        rows.append([InlineKeyboardButton(text="🛡 Команды модерации", callback_data="help_moderation")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# --- КОМАНДЫ ---

# ИЗМЕНЕНО: Добавлен фильтр F.chat.type == "private", чтобы работало только в ЛС
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: types.Message):
    await answer_temp(
        message,
        photo=IMG_WELCOME,
        text=(
            f"👋 <b>Я бот-модератор для чата.</b>\n"
            f"<i>Чтобы посмотреть мои команды, напиши</i> <code>/help</code>."
        ),
        parse_mode="HTML",
    )

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await delete_later(message, 0)
    
    user_data = await get_user(message.from_user.id)
    lvl = user_data[4]
    text = help_main_text(lvl)
    effective_mod_level = await get_effective_level(
        message.from_user.id,
        message.chat,
        int(user_data[6] or 0),
        message.sender_chat,
    )
    kb = help_keyboard(effective_mod_level >= 1)

    await answer_temp(
        message,
        text=text,
        photo=IMG_HELP_LEADERS,
        parse_mode="HTML",
        reply_markup=kb,
        global_key="help_menu",
    )


@router.callback_query(F.data == "help_xp")
async def cb_help_xp(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
        ]
    )
    await answer_temp(
        callback.message,
        text=help_xp_text(),
        photo=IMG_HELP_LEADERS,
        parse_mode="HTML",
        reply_markup=kb,
        global_key="help_menu",
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "help_moderation")
async def cb_help_moderation(callback: CallbackQuery):
    user_data = await get_user(callback.from_user.id)
    effective_mod_level = await get_effective_level(
        callback.from_user.id,
        callback.message.chat,
        int(user_data[6] or 0),
    )
    if effective_mod_level < 1:
        return await callback.answer("Нет доступа к командам модерации.", show_alert=True)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="help_back")]
        ]
    )
    await answer_temp(
        callback.message,
        text=moderation_help_text(),
        parse_mode="HTML",
        reply_markup=kb,
        global_key="help_menu",
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "help_back")
async def cb_help_back(callback: CallbackQuery):
    user_data = await get_user(callback.from_user.id)
    effective_mod_level = await get_effective_level(
        callback.from_user.id,
        callback.message.chat,
        int(user_data[6] or 0),
    )
    kb = help_keyboard(effective_mod_level >= 1)
    await answer_temp(
        callback.message,
        text=help_main_text(user_data[4]),
        photo=IMG_HELP_LEADERS,
        parse_mode="HTML",
        reply_markup=kb,
        global_key="help_menu",
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()

@router.message(Command("staff"))
async def cmd_staff(message: types.Message):
    await delete_later(message, 0)
    
    user_data = await get_user(message.from_user.id)
    lvl = user_data[4]
    
    eff_lvl = await get_effective_level(message.from_user.id, message.chat, lvl, message.sender_chat)
    if eff_lvl < 3:
        return await answer_temp(message, "🔒 Команда <code>/staff</code> доступна с <b>3 уровня</b>.")

    staff_list = await get_all_staff()
    if not staff_list:
        return await answer_temp(message, "Список персонала пуст.")
    
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
    text_commands = moderation_help_text(include_title=False)
    
    full_text = "\n".join(text_lines) + "\n" + "_"*15 + "\n\n" + text_commands
            
    await answer_temp(message, full_text)

@router.message(Command("leaders"))
async def cmd_leaders(message: types.Message):
    await delete_later(message, 0)
    
    user_data = await get_user(message.from_user.id)
    lvl = user_data[4]
    
    eff_lvl = await get_effective_level(message.from_user.id, message.chat, lvl, message.sender_chat)
    if eff_lvl < 2:
        return await answer_temp(message, "🔒 Список лидеров доступен со <b>2 уровня</b>.")
    
    text = await generate_leaders_text(message.from_user.id)
    
    await answer_temp(
        message,
        text=text,
        photo=IMG_HELP_LEADERS,
        parse_mode="HTML",
        global_key="leaders_message",
    )

async def generate_leaders_text(user_id):
    top_users = await get_top_users(limit=10)
    month_users = await get_month_leaders(limit=10)
    title = await get_previous_month_title()
    text = "🏆 <b>ОБЩИЙ ТОП</b>\n\n"
    
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
        
    text += "\n\n📅 <b>АКТИВНОСТЬ ЭТОГО МЕСЯЦА</b>\n"
    if month_users:
        for i, (name, score, uid) in enumerate(month_users, 1):
            text += f"\n<b>{i}.</b> <a href='tg://user?id={uid}'>{name}</a> — <code>{format_xp(score)} XP</code>"
    else:
        text += "\nПока никто не заработал XP за полезную активность."
    if title:
        key, uid, name = title
        text += f"\n\n👑 Лидер {format_month_key(key)}: <a href='tg://user?id={uid}'>{name}</a>"
    return text

@router.message(Command("profile"))
async def show_profile(message: types.Message, command: CommandObject = None):
    await delete_later(message, 0)
    user_id = message.from_user.id

    caller_data = await get_user(
        user_id=message.from_user.id, 
        username=message.from_user.username, 
        full_name=message.from_user.full_name
    )
    db_level = caller_data[6] if caller_data and len(caller_data) > 6 and caller_data[6] is not None else 0
    lvl = caller_data[4] if caller_data and len(caller_data) > 4 and caller_data[4] is not None else 1
    
    effective_level = await get_effective_level(message.from_user.id, message.chat, db_level, message.sender_chat)

    target_id = None
    cmd_args = command.args if command else None
    is_foreign_request = (cmd_args is not None and cmd_args.strip()) or message.reply_to_message

    if is_foreign_request:
        if not can_view_foreign_profile(lvl, effective_level):
            return await answer_temp(
                message,
                "⛔ Просмотр чужих профилей доступен с <b>4 игрового уровня</b> или ранга <b>Moder²</b>.",
            )
        
        if cmd_args:
            username_arg = cmd_args.split()[0].replace("@", "")
            found_id = await get_id_by_username(username_arg)
            if found_id: target_id = found_id
            else: return await answer_temp(message, "❌ Пользователь не найден.")
        elif message.reply_to_message:
            target_id = message.reply_to_message.from_user.id
    else:
        target_id = message.from_user.id
    
    if not target_id: return

    text, photo, free_dice_ready = await generate_profile_content(target_id)
    
    markup = None
    if target_id == message.from_user.id:
        final_lvl = effective_level if effective_level > lvl else lvl
        markup = get_profile_keyboard(final_lvl, free_dice_ready=free_dice_ready, owner_id=message.from_user.id)

    sent = await answer_temp(message, text=text, photo=photo, parse_mode="HTML", reply_markup=markup)
    if sent is None and photo is not None:
        # Fallback: if external image URL is unavailable, send text-only profile.
        await answer_temp(message, text=text, parse_mode="HTML", reply_markup=markup)


async def generate_profile_content(user_id):
    data = await get_user(user_id)
    if not data:
        return "Нет данных.", None, False
    
    _, _, db_full_name, xp, lvl, warns, mod_lvl, rep, _last_free_dice_ts = data
    
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

    month_score, month_rank = await get_month_score(user_id)
    wins, last_win = await get_month_wins(user_id)
    month_line = f"\n📅 За месяц: <b>{format_xp(month_score)} XP</b>"
    if month_rank:
        month_line += f" · место <b>#{month_rank}</b>"
    if wins:
        month_line += f"\n🏅 Побед в месяце: <b>{wins}</b> (последняя: {format_month_key(last_win)})"
    orders, successful, involved, distributed = await get_factory_order_stats(user_id)
    factory_line = ""
    if orders:
        factory_line = (f"\n🏭 Заказы: <b>{successful}/{orders}</b> успешно"
                        f" · участников <b>{involved}</b> · банк <b>{format_xp(distributed)} XP</b>")

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
        f"{rep_line}{month_line}{factory_line}\n\n"
        f"{level_display}: {progress_bar}\n"
        f"{xp_line}"
        f"{warn_text}" 
    )

    remaining = await get_free_dice_remaining(user_id)
    if remaining <= 0:
        profile_text += "\n\n🎁 <b>Доступен бесплатный бросок кубика</b>"
        free_dice_ready = True
    else:
        profile_text += f"\n\n⏳ Бросок кубика будет доступен через <b>{format_dice_cooldown(remaining)}</b>"
        free_dice_ready = False
    
    photo = None
    if lvl == 1:
        photo = IMG_LEVEL_1
    elif lvl == 2:
        photo = IMG_LEVEL_2
    elif lvl == 3:
        photo = IMG_LEVEL_3
    elif lvl == 4:
        photo = IMG_LEVEL_4
    else:
        photo = IMG_LEVEL_5
        
    return profile_text, photo, free_dice_ready

def get_profile_keyboard(user_lvl, free_dice_ready=False, owner_id=None):
    if user_lvl >= 2:
        btn_leaders = InlineKeyboardButton(text="🏆 Лидеры", callback_data="nav_leaders")
    else:
        btn_leaders = InlineKeyboardButton(text="🔒 Лидеры (Ур.2)", callback_data="locked_2")
        
    if user_lvl >= 1:
        btn_games = InlineKeyboardButton(text="🎮 Игры", callback_data="nav_games")
    else:
        btn_games = InlineKeyboardButton(text="🔒 Игры (Ур.1)", callback_data="locked_1")
        
    rows = [[btn_leaders, btn_games]]
    if owner_id is not None:
        rows.append([InlineKeyboardButton(text="🏭 Управление цехом", callback_data=f"farm_open:{owner_id}")])
    if free_dice_ready and owner_id is not None:
        rows.append([InlineKeyboardButton(text="🎁 Бросить бесплатный кубик", callback_data=f"free_dice_roll:{owner_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return kb

# --- CALLBACK HANDLERS (МЕНЮ ПРОФИЛЯ) ---

@router.callback_query(F.data == "nav_profile")
async def cb_back_profile(callback: CallbackQuery):
    text, _, free_dice_ready = await generate_profile_content(callback.from_user.id)
    
    caller_data = await get_user(callback.from_user.id)
    lvl = caller_data[4]
    db_level = caller_data[6]
    
    eff_lvl = await get_effective_level(callback.from_user.id, callback.message.chat, db_level)
    final_lvl = eff_lvl if eff_lvl > lvl else lvl
    
    markup = get_profile_keyboard(final_lvl, free_dice_ready=free_dice_ready, owner_id=callback.from_user.id)
    
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await answer_temp(
            callback.message,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            user_id=callback.from_user.id,
        )
        try:
            await callback.message.delete()
        except Exception:
            pass
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
            await answer_temp(
                callback.message,
                text=text,
                photo=IMG_HELP_LEADERS,
                parse_mode="HTML",
                reply_markup=kb,
                user_id=callback.from_user.id,
            )
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
    
    if eff_lvl < 1 and lvl < 1:
        return await callback.answer("Нужен уровень 1!", show_alert=True)
    
    uid = callback.from_user.id
    final_lvl = eff_lvl if eff_lvl > lvl else lvl
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            get_game_btn_simple('dice', final_lvl, "🎲 Кости", "game_menu_dice", uid),
            get_game_btn_simple('slots', final_lvl, "🎰 Рулетка", "game_menu_slots", uid)
        ],
        [
            get_game_btn_simple('duel', final_lvl, "🔫 Дуэль", "game_info_duel", uid),
            get_game_btn_simple('basketball', final_lvl, "🏀 Баскет", "game_menu_basket", uid)
        ],
        [InlineKeyboardButton(text="🏭 Управление цехом", callback_data=f"farm_open:{uid}")],
        [InlineKeyboardButton(text="👤 В профиль", callback_data="nav_profile")]
    ])
    
    user_link = get_user_link(uid, callback.from_user.full_name or "Игрок")
    text = (
        f"🕹️ <b>Игровая зона</b> • {user_link}\n"
        f"\n"
        f"📊 Уровень: <b>{final_lvl}</b>\n"
        f"💳 Баланс: <code>{format_xp(user_data[3])} XP</code>\n"
        f"\n"
        f"Выберите автомат:"
    )

    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("free_dice_roll:"))
async def cb_free_dice_roll(callback: CallbackQuery):
    owner_id = int(callback.data.split(":", 1)[1])
    if callback.from_user.id != owner_id:
        return await callback.answer("Этот бросок не для вас.", show_alert=True)

    user_id = callback.from_user.id
    remaining = await get_free_dice_remaining(user_id)
    if remaining > 0:
        return await callback.answer(
            f"Кубик будет доступен через {format_dice_cooldown(remaining)}.",
            show_alert=True,
        )

    claimed = await claim_free_dice(user_id)
    if not claimed:
        remaining = await get_free_dice_remaining(user_id)
        return await callback.answer(
            f"Кубик будет доступен через {format_dice_cooldown(remaining)}.",
            show_alert=True,
        )

    dice_msg = await callback.message.answer_dice(emoji="🎲")
    await asyncio.sleep(4)
    value = dice_msg.dice.value

    if value >= 4:
        await update_xp(user_id, 50)
        result_text = f"🎉 <b>Бесплатный бросок: {value}</b>\nВыигрыш: <code>+50 XP</code>!"
    else:
        result_text = f"🎲 <b>Бесплатный бросок: {value}</b>\nНа этот раз без награды."

    text, _, free_dice_ready = await generate_profile_content(user_id)

    user_data = await get_user(user_id)
    lvl = user_data[4]
    db_level = user_data[6]
    eff_lvl = await get_effective_level(user_id, callback.message.chat, db_level)
    final_lvl = eff_lvl if eff_lvl > lvl else lvl
    markup = get_profile_keyboard(final_lvl, free_dice_ready=free_dice_ready, owner_id=user_id)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        # Fallback: if profile edit fails, send profile as a fresh message.
        await answer_temp(
            callback.message,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            user_id=user_id,
        )
        try:
            await callback.message.delete()
        except Exception:
            pass

    # Separate message with game result (not embedded into profile).
    await answer_temp(
        callback.message,
        text=result_text,
        parse_mode="HTML",
        key=f"free_dice_result:{user_id}",
    )

    await delete_later(dice_msg, 4)
    await callback.answer("Бросок выполнен!")

@router.callback_query(F.data.startswith("locked_") & ~F.data.startswith("locked_game"))
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
    if is_anonymous_admin_message(message):
        is_chat_admin = True
    elif message.from_user.id == OWNER_ID or mod_lvl >= 5:
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
        await answer_temp(message, f"🗑 <b>Народный модератор {message.from_user.mention_html()} удалил сообщение!</b>")
    except Exception as e:
        await answer_temp(message, f"❌ Не удалось удалить: {e}")


# --- ПРИВЕТСТВИЕ И ВСЕ ПУТИ ВХОДА ---
async def begin_human_verification(
    bot, chat, new_user, joined_at=None, *, source="message_fallback", _lock_held=False,
):
    if new_user.is_bot:
        return
    async with (nullcontext() if _lock_held else _captcha_lock(chat.id, new_user.id)):
        pending = await get_human_verification(chat.id, new_user.id)
        access = await get_chat_verification_access(chat.id, new_user.id)
        # A join can arrive as both a service message and a chat_member update.
        # A fallback message can also race them. Preserve a single challenge.
        if pending and (joined_at is None or int(pending["joined_at"]) >= joined_at):
            if pending.get("welcome_message_id"):
                return
        if access and (joined_at is None or int(access["joined_at"]) >= joined_at):
            return
        challenge_code = "".join(_captcha_random.choice("23456789") for _ in range(CAPTCHA_LENGTH))
        challenge_token = secrets.token_urlsafe(6)
        expires_at = int(time.time()) + CAPTCHA_TTL_SECONDS
        old_welcome_id = await set_human_verification_pending(
            chat.id, new_user.id, joined_at,
            challenge_token=challenge_token,
            challenge_code=challenge_code,
            expires_at=expires_at,
        )
        _cancel_captcha_timeout(chat.id, new_user.id)
        if old_welcome_id:
            try:
                await bot.delete_message(chat.id, old_welcome_id)
            except Exception:
                pass

        welcome_text = _captcha_caption(new_user.mention_html())
        keyboard = _captcha_keyboard(challenge_token)

        sent = None
        try:
            sent = await bot.send_photo(
                chat_id=chat.id,
                photo=BufferedInputFile(
                    build_captcha_png(challenge_code),
                    filename="human-check.png",
                ),
                caption=welcome_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            _verification_log.exception(
                "captcha_send_failed chat=%s user=%s source=%s", chat.id, new_user.id, source,
            )
            # Keep the pending record. The next message retries delivery, never grants access.
            return

        stored = await set_human_verification_message(
            chat.id,
            new_user.id,
            sent.message_id,
        )
        if not stored:
            try:
                await sent.delete()
            except Exception:
                pass
            return
        _schedule_captcha_timeout(
            bot,
            chat.id,
            new_user.id,
            challenge_token,
            expires_at,
        )
        _verification_log.info(
            "captcha_created chat=%s user=%s source=%s message=%s",
            chat.id, new_user.id, source, sent.message_id,
        )


@router.message(F.new_chat_members)
async def on_user_join(message: types.Message):
    for new_user in message.new_chat_members:
        await begin_human_verification(
            message.bot, message.chat, new_user, int(message.date.timestamp()), source="service_join",
        )
    try:
        await message.delete()
    except Exception:
        pass


def _member_present(member) -> bool:
    status = getattr(member.status, "value", str(member.status))
    return status in {"member", "administrator", "creator"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )


@router.chat_member()
async def on_membership_change(event: types.ChatMemberUpdated):
    was_member = _member_present(event.old_chat_member)
    is_member = _member_present(event.new_chat_member)
    target = event.new_chat_member.user
    if not was_member and is_member:
        if getattr(event.new_chat_member.status, "value", str(event.new_chat_member.status)) in {"creator", "administrator"}:
            return
        await begin_human_verification(
            event.bot, event.chat, target, int(event.date.timestamp()), source="chat_member",
        )
    elif was_member and not is_member:
        await cleanup_departed_verification(event.bot, event.chat.id, target.id)


@router.callback_query(F.data.startswith("captcha:"))
async def confirm_human(callback: CallbackQuery):
    async with _captcha_lock(callback.message.chat.id, callback.from_user.id):
        response = await _confirm_human_locked(callback)
    # Acknowledge outside the input lock: slow Telegram replies cannot stall later digits.
    try:
        await callback.answer(**(response or {}))
    except Exception:
        _verification_log.warning(
            "captcha_callback_ack_failed chat=%s user=%s", callback.message.chat.id, callback.from_user.id,
        )


async def _confirm_human_locked(callback: CallbackQuery):
    try:
        _, token, action, *value = callback.data.split(":")
    except (AttributeError, ValueError):
        return {"text": "Некорректная кнопка.", "show_alert": True}

    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    pending = await get_human_verification(chat_id, user_id)
    if not pending or pending.get("challenge_token") != token:
        return {"text": "Эта проверка предназначена другому пользователю.", "show_alert": True}

    if int(pending.get("expires_at") or 0) <= int(time.time()):
        await _kick_after_failed_captcha(
            callback.bot,
            chat_id,
            user_id,
            pending.get("welcome_message_id"),
        )
        return {"text": "Время проверки истекло. Войдите в группу повторно.", "show_alert": True}

    entered = pending.get("entered_code") or ""
    if action == "digit":
        digit = value[0] if value else ""
        if digit not in "0123456789" or len(digit) != 1:
            return {"text": "Некорректная кнопка.", "show_alert": True}
        if len(entered) >= CAPTCHA_LENGTH:
            _schedule_captcha_view(callback, token)
            return
        entered += digit
        await update_human_verification_input(chat_id, user_id, token, entered)
        _schedule_captcha_view(callback, token)
        return

    if action in {"back", "clear"}:
        entered = entered[:-1] if action == "back" else ""
        await update_human_verification_input(chat_id, user_id, token, entered)
        _schedule_captcha_view(callback, token)
        return

    if action != "submit":
        return {"text": "Некорректная кнопка.", "show_alert": True}

    if len(entered) != CAPTCHA_LENGTH:
        return {"text": f"Введи все {CAPTCHA_LENGTH} цифр с картинки.", "show_alert": True}

    if not secrets.compare_digest(entered, pending.get("challenge_code") or ""):
        attempts = await fail_human_verification_attempt(chat_id, user_id, token)
        if attempts is None:
            return {"text": "Проверка уже завершена.", "show_alert": True}
        if attempts >= CAPTCHA_MAX_ATTEMPTS:
            await _kick_after_failed_captcha(
                callback.bot,
                chat_id,
                user_id,
                pending.get("welcome_message_id"),
            )
            return {"text": "Проверка не пройдена.", "show_alert": True}
        _schedule_captcha_view(callback, token)
        return {
            "text": f"Неверный код. Осталось попыток: {CAPTCHA_MAX_ATTEMPTS - attempts}.",
            "show_alert": True,
        }

    completed, welcome_message_id = await complete_human_verification(
        chat_id,
        user_id,
        challenge_token=token,
    )
    if not completed:
        return {"text": "Проверка устарела. Используйте текущую картинку.", "show_alert": True}
    _cancel_captcha_timeout(chat_id, user_id)
    await delete_temp_by_scope(
        callback.message,
        key=f"human_verification:{user_id}",
    )
    try:
        await callback.bot.delete_message(callback.message.chat.id, welcome_message_id or callback.message.message_id)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await answer_temp(
        callback.message,
        _verified_welcome_text(callback.from_user),
        photo=IMG_WELCOME,
        delay=120,
        key=f"verified_welcome:{user_id}",
        parse_mode="HTML",
    )
    _verification_log.info("captcha_passed chat=%s user=%s", chat_id, user_id)


@router.message(F.left_chat_member)
async def on_user_leave(message: types.Message):
    left_user = message.left_chat_member
    if not left_user or left_user.is_bot:
        return
    await cleanup_departed_verification(message.bot, message.chat.id, left_user.id)


async def cleanup_departed_verification(bot, chat_id, user_id):
    async with _captcha_lock(chat_id, user_id):
        completed, welcome_message_id = await complete_human_verification(chat_id, user_id)
        await revoke_chat_verification_access(chat_id, user_id)
        _cancel_captcha_timeout(chat_id, user_id)
        if completed and welcome_message_id:
            try:
                await bot.delete_message(chat_id, welcome_message_id)
            except Exception:
                pass


# --- ОСНОВНОЙ ХЕНДЛЕР ТЕКСТА (ФАРМ XP + REP) ---
@router.message(F.text & ~F.text.startswith('/'))
async def text_handler(message: types.Message):
    if message.chat.type == 'private': return

    await register_sync_activity()
    bump_sticky_message_counter(message.chat.id, "games_menu_hint")

    if not message.from_user:
        return
    await ensure_level_tag(message)
    
    user_id = message.from_user.id
    text = message.text
    
    # 1. СИСТЕМА РЕПУТАЦИИ (+rep)
    if message.reply_to_message and text.strip().lower() in ["+rep", "+реп", "респект"]:
        giver = await get_user(user_id)
        giver_rpg_lvl = giver[4] # RPG Level
        giver_mod_lvl = giver[6] # Mod Level
        
        is_admin_or_staff = False
        if giver_mod_lvl >= 1 or user_id == OWNER_ID:
             is_admin_or_staff = True
        elif is_anonymous_admin_message(message):
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
            
            if result in ("success_full", "success_repeat"):
                full_reward = 250 if giver_rpg_lvl >= 5 else 150
                reward = 25 if result == "success_repeat" else full_reward
                old, new, added = await update_xp(target_id, reward, count_monthly=True)
                await answer_temp(
                    message,
                    f"🤝 {message.from_user.mention_html()} повысил репутацию {message.reply_to_message.from_user.mention_html()}!\n"
                    f"Получено <code>+{reward} XP</code>."
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
            
    result = await process_chat_activity(message, is_media=False)
    await track_text_message(message)
    if result["revived"]:
        await replace_sticky_message(
            message,
            scope="chat_revived_notice",
            text=(
                f"⚡ <b>Чат оживлён</b>\n"
                f"{message.from_user.mention_html()} получает <code>+50 XP</code> за новую волну активности."
            ),
            reply=True,
            parse_mode="HTML",
        )
    
    if result["wave_users"]:
        await answer_temp(message, f"🌊 <b>Волна активности!</b> {len(result['wave_users'])} участника получили по <code>+25 XP</code>.")

# --- ХЕНДЛЕР КОНТЕНТА (Видео/Фото) ---
@router.message(F.photo | F.video)
async def media_handler(message: types.Message):
    if message.chat.type == 'private': return

    await register_sync_activity()
    bump_sticky_message_counter(message.chat.id, "games_menu_hint")

    if not message.from_user:
        return
    await ensure_level_tag(message)
    
    result = await process_chat_activity(message, is_media=True)
    await track_media_message(message)
    if result["revived"]:
        await answer_temp(message, f"⚡ {message.from_user.mention_html()} оживил чат и получил <code>+50 XP</code>.")
    if result["wave_users"]:
        await answer_temp(message, f"🌊 <b>Волна активности!</b> {len(result['wave_users'])} участника получили по <code>+25 XP</code>.")
