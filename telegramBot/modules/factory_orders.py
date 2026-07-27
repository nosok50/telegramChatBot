import asyncio
import html
import json
import logging
import random
import time
from collections import defaultdict
from functools import wraps
from datetime import datetime

import aiosqlite
from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import (DB_NAME, farm_is_complete, get_user,
                      total_available_xp, _mark_dirty, LEVEL_CAPS)
from level_tags import queue_level_tag_refresh
from utils import answer_temp, delete_later, touch_temp_message


router = Router()

ORDER_SIZES = {
    "small": (50_000, 500, 100),
    "medium": (200_000, 2_000, 500),
    "large": (500_000, 5_000, 1_000),
}
TYPE_ALIASES = {"discussion": "discussion", "topic": "discussion", "обсуждение": "discussion",
                "photo": "photo", "фото": "photo", "tournament": "tournament", "турнир": "tournament"}
SIZE_ALIASES = {"small": "small", "малый": "small", "medium": "medium", "средний": "medium",
                "large": "large", "большой": "large"}
_processor_task = None
_order_locks = defaultdict(asyncio.Lock)
_chat_order_locks = defaultdict(asyncio.Lock)


def build_factory_help_main(owner_id: int):
    text = (
        "🏭 <b>Заводские заказы</b>\n\n"
        "Потратьте монеты полностью прокачанного завода, чтобы запустить событие "
        "для всего чата и разыграть XP.\n\n"
        "Пример команды:\n"
        "<code>/factory_order</code> <code>обсуждение</code> "
        "<code>малый</code> <code>Ваша тема</code>\n\n"
        "Заказ запускается в общем чате. При провале возвращается 80% монет."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Подробнее", callback_data=f"factory_help_more:{owner_id}")],
        [InlineKeyboardButton(text="🏭 Назад в завод", callback_data=f"farm_open:{owner_id}")],
    ])
    return text, kb


def build_factory_help_more(owner_id: int):
    text = (
        "🏭 <b>Как работает заказ</b>\n\n"
        "<b>1.</b> Вы платите монетами завода.\n"
        "<b>2.</b> Бот публикует в чате задание.\n"
        "<b>3.</b> Другие игроки выполняют его, отвечая на сообщение бота.\n"
        "<b>4.</b> Если условие выполнено вовремя, бот автоматически раздаёт XP.\n\n"
        "Организатор не считается участником. В чате может идти только один заказ.\n\n"
        "💬 <code>обсуждение</code> — 5 игроков обсуждают тему и отвечают друг другу "
        "не менее 10 раз. Время: 6 часов.\n\n"
        "📸 <code>фото</code> — 4 игрока присылают фото или видео. Затем бот запускает голосование.\n\n"
        "⚔️ <code>турнир</code> — первые 4 игрока входят по кнопке и играют два полуфинала и финал.\n\n"
        "<b>Как устроена команда</b>\n"
        "<code>/factory_order</code> — запустить заказ\n"
        "<code>обсуждение</code> — что делают игроки\n"
        "<code>малый</code> — цена и размер награды\n"
        "<code>Ваша тема</code> — тема задания\n\n"
        "Пример:\n"
        "<code>/factory_order</code> <code>обсуждение</code> "
        "<code>малый</code> <code>Какая игра вас разочаровала?</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Размеры и награды", callback_data=f"factory_help_sizes:{owner_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"factory_help_main:{owner_id}")],
    ])
    return text, kb


def build_factory_help_sizes(owner_id: int):
    text = (
        "💰 <b>Размер заказа</b>\n\n"
        "Размер не меняет задание. Он меняет стоимость запуска и количество разыгрываемого XP.\n\n"
        "• <code>малый</code> — 50 000 монет → 500 XP\n"
        "• <code>средний</code> — 200 000 монет → 2 000 XP\n"
        "• <code>большой</code> — 500 000 монет → 5 000 XP\n\n"
        "Чем больше заказ, тем больше монет вы рискуете и тем больше XP получат участники."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Как играть", callback_data=f"factory_help_more:{owner_id}")],
        [InlineKeyboardButton(text="🏭 В завод", callback_data=f"farm_open:{owner_id}")],
    ])
    return text, kb


async def _edit_help(callback, text, kb):
    await touch_temp_message(callback.message)
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, reply_markup=kb)
    else:
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


async def _check_help_owner(callback, owner_id):
    if callback.from_user.id != owner_id:
        await callback.answer("Это не ваш завод.", show_alert=True)
        return False
    return True


@router.callback_query(F.data.startswith("factory_help_main:"))
async def factory_help_main(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if not await _check_help_owner(callback, owner_id):
        return
    await _edit_help(callback, *build_factory_help_main(owner_id))


@router.callback_query(F.data.startswith("factory_help_more:"))
async def factory_help_more(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if not await _check_help_owner(callback, owner_id):
        return
    await _edit_help(callback, *build_factory_help_more(owner_id))


@router.callback_query(F.data.startswith("factory_help_sizes:"))
async def factory_help_sizes(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if not await _check_help_owner(callback, owner_id):
        return
    await _edit_help(callback, *build_factory_help_sizes(owner_id))


def _serialized_order(handler):
    @wraps(handler)
    async def wrapped(callback, *args, **kwargs):
        try:
            order_id = int(callback.data.split(":")[1])
        except Exception:
            return await callback.answer("Некорректный заказ.", show_alert=True)
        async with _order_locks[order_id]:
            return await handler(callback, *args, **kwargs)
    return wrapped


def _kb_tournament(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Войти в турнир", callback_data=f"fjoin:{order_id}")]
    ])


def _kb_tactics(order_id, match_no):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Атака", callback_data=f"ftac:{order_id}:{match_no}:atk"),
        InlineKeyboardButton(text="🛡 Защита", callback_data=f"ftac:{order_id}:{match_no}:def"),
        InlineKeyboardButton(text="⚡ Хитрость", callback_data=f"ftac:{order_id}:{match_no}:trick"),
    ]])


async def _order(order_id=None, chat_id=None):
    where, value = ("id", order_id) if order_id is not None else ("chat_id", chat_id)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            f"SELECT * FROM factory_orders WHERE {where}=? AND status IN ('active','voting','tournament') ORDER BY id DESC LIMIT 1",
            (value,))).fetchone()


async def _participants(order_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM factory_order_participants WHERE order_id=? ORDER BY joined_at,user_id", (order_id,))).fetchall()


async def _set_order(order_id, **values):
    if not values:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"UPDATE factory_orders SET {','.join(k+'=?' for k in values)} WHERE id=?",
                         (*values.values(), order_id)); await db.commit()
    await _mark_dirty("factory_orders")


async def launch_factory_order(
    bot,
    chat_id: int,
    owner_id: int,
    owner_name: str,
    order_type: str,
    size: str,
    topic: str = "",
    *,
    charge_factory: bool = True,
):
    """Create the order through the same state and message path for players and admins."""
    if order_type not in ("discussion", "photo", "tournament") or size not in ORDER_SIZES:
        return None, "Некорректный тип или размер заказа."
    await get_user(owner_id)
    topic = (topic or "").strip()
    if order_type != "tournament" and len(topic) < 5:
        return None, "Тема задания должна содержать хотя бы 5 символов."
    if charge_factory and not await farm_is_complete(owner_id):
        return None, "Заказы откроются, когда все 9 клеток будут застроены, а всё оборудование прокачано до 3 уровня."

    cost, bank, stake = ORDER_SIZES[size]
    charged_cost = cost if charge_factory else 0
    now = int(time.time())
    duration = 1800 if order_type == "tournament" else 6 * 3600
    metadata = {"stake": stake, "admin_launch": not charge_factory}

    async with _chat_order_locks[int(chat_id)]:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("BEGIN IMMEDIATE")
            active = await (await db.execute(
                """SELECT 1 FROM factory_orders
                   WHERE chat_id=? AND status IN ('active','voting','tournament') LIMIT 1""",
                (int(chat_id),),
            )).fetchone()
            if active:
                await db.rollback()
                return None, "В этом чате уже идёт заводской заказ."
            if charge_factory:
                spent = await db.execute(
                    """UPDATE farm_players SET coins=coins-?,updated_at=?
                       WHERE user_id=? AND coins>=?""",
                    (cost, now, int(owner_id), cost),
                )
                if spent.rowcount != 1:
                    await db.rollback()
                    return None, f"Не хватает монет завода. Нужно {cost:,}."
            try:
                cur = await db.execute(
                    """INSERT INTO factory_orders
                       (chat_id,owner_id,order_type,size,coin_cost,xp_bank,topic,status,
                        created_at,stage_ends_at,metadata)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        int(chat_id), int(owner_id), order_type, size, charged_cost, bank,
                        topic, "active", now, now + duration, json.dumps(metadata),
                    ),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return None, "В этом чате уже идёт заводской заказ."
            order_id = int(cur.lastrowid)
            await db.commit()
    await _mark_dirty("factory_orders", *(["farm_players"] if charge_factory else []))

    names = {"discussion": "Тема смены", "photo": "Фотосмена", "tournament": "Турнир цехов"}
    conditions = {
        "discussion": "Нужно 5 участников и 10 ответов друг другу. Отвечайте внутри этой ветки.",
        "photo": "Нужно 4 участника. Пришлите по одному фото/видео ответом на это сообщение.",
        "tournament": f"Нужно 4 участника. Взнос каждого — {stake} XP. Регистрация 30 минут.",
    }
    owner_link = f'<a href="tg://user?id={int(owner_id)}">{html.escape(owner_name or "Организатор")}</a>'
    text = (
        f"🏭 <b>{names[order_type]}</b> · заказ #{order_id}\n"
        f"Организатор: {owner_link}\n"
        f"Банк завода: <code>{bank} XP</code>\n"
        + (f"Тема: <b>{html.escape(topic)}</b>\n" if topic else "")
        + f"\n{conditions[order_type]}"
    )
    try:
        sent = await bot.send_message(
            chat_id=int(chat_id),
            text=text,
            reply_markup=_kb_tournament(order_id) if order_type == "tournament" else None,
        )
    except Exception:
        # Never leave a paid but unusable order if Telegram rejects the anchor.
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("BEGIN IMMEDIATE")
            changed = await db.execute(
                """UPDATE factory_orders SET status='cancelled'
                   WHERE id=? AND status='active' AND message_id=0""",
                (order_id,),
            )
            if changed.rowcount == 1 and charged_cost:
                await db.execute(
                    "UPDATE farm_players SET coins=coins+?,updated_at=? WHERE user_id=?",
                    (charged_cost, int(time.time()), int(owner_id)),
                )
            await db.commit()
        await _mark_dirty("factory_orders", *(["farm_players"] if charged_cost else []))
        return None, "Telegram не смог опубликовать событие. Монеты не списаны."

    async with aiosqlite.connect(DB_NAME) as db:
        attached = await db.execute(
            """UPDATE factory_orders SET message_id=?
               WHERE id=? AND status='active'""",
            (sent.message_id, order_id),
        )
        await db.commit()
    if attached.rowcount != 1:
        await _delete_message_id(bot, chat_id, sent.message_id)
        return None, "Событие было остановлено во время запуска."
    await _mark_dirty("factory_orders")
    return await _order(order_id=order_id), ""


async def _delete_message_id(bot, chat_id: int, message_id):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
    except Exception:
        pass


async def _cleanup_order_messages(bot, order, extra_message_ids=None):
    message_ids = {
        int(order["message_id"] or 0),
        int(order["vote_message_id"] or 0),
    }
    try:
        meta = json.loads(order["metadata"] or "{}")
        message_ids.add(int(meta.get("stage_message_id") or 0))
    except Exception:
        pass
    for message_id in extra_message_ids or ():
        message_ids.add(int(message_id or 0))
    for message_id in message_ids:
        if message_id:
            await _delete_message_id(bot, order["chat_id"], message_id)


async def _clear_order_message_refs(order_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute(
            "SELECT metadata FROM factory_orders WHERE id=?", (int(order_id),)
        )).fetchone()
    try:
        meta = json.loads(row[0] or "{}") if row else {}
    except Exception:
        meta = {}
    meta.pop("stage_message_id", None)
    await _set_order(
        order_id,
        message_id=0,
        vote_message_id=0,
        metadata=json.dumps(meta),
    )


async def _send_temp_result(bot, chat_id: int, text: str, delay=60, reply_markup=None):
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    asyncio.create_task(delete_later(sent, delay))
    return sent


async def _send_tournament_stage(bot, order, meta, text, reply_markup):
    sent = await bot.send_message(order["chat_id"], text, reply_markup=reply_markup)
    try:
        meta["stage_message_id"] = sent.message_id
        await _set_order(order["id"], metadata=json.dumps(meta))
    except Exception:
        await _delete_message_id(bot, order["chat_id"], sent.message_id)
        raise
    return sent


async def _send_tournament_stage_or_cancel(bot, order, meta, text, reply_markup):
    try:
        return await _send_tournament_stage(bot, order, meta, text, reply_markup)
    except Exception:
        await _cancel_factory_order_unlocked(
            bot,
            order["id"],
            "техническая ошибка при открытии следующего боя",
        )
        return None


async def get_factory_order_stats(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute('''SELECT COUNT(*),
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
            COALESCE(SUM(CASE WHEN status='completed' THEN distributed_xp ELSE 0 END),0)
            FROM factory_orders WHERE owner_id=?''', (user_id,))).fetchone()
        people = await (await db.execute('''SELECT COUNT(DISTINCT p.user_id) FROM factory_order_participants p
            JOIN factory_orders o ON o.id=p.order_id WHERE o.owner_id=?''', (user_id,))).fetchone()
        return int(row[0]), int(row[1] or 0), int(people[0] or 0), int(row[2] or 0)


@router.message(Command("factory_order"))
async def create_order(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if message.chat.type == "private":
        return await answer_temp(message, "Заводской заказ запускается в общем чате, а не в личке.")
    args = (command.args or "").split(maxsplit=2)
    if len(args) < 2:
        text, kb = build_factory_help_main(message.from_user.id)
        return await answer_temp(message, text, reply_markup=kb, user_id=message.from_user.id)
    order_type = TYPE_ALIASES.get(args[0].lower())
    size = SIZE_ALIASES.get(args[1].lower())
    topic = args[2].strip() if len(args) > 2 else ""
    if not order_type or not size or (order_type != "tournament" and len(topic) < 5):
        return await answer_temp(
            message,
            "Не удалось разобрать команду.\n\n"
            "Пример:\n"
            "<code>/factory_order</code> <code>обсуждение</code> "
            "<code>малый</code> <code>Ваша тема</code>",
            user_id=message.from_user.id,
        )
    _order_row, error = await launch_factory_order(
        message.bot,
        message.chat.id,
        message.from_user.id,
        message.from_user.full_name,
        order_type,
        size,
        topic,
        charge_factory=True,
    )
    if error:
        return await answer_temp(message, f"⚠️ {html.escape(error)}", user_id=message.from_user.id)


async def track_text_message(message):
    order = await _order(chat_id=message.chat.id)
    if not order or order["order_type"] != "discussion" or order["status"] != "active":
        return
    if message.from_user.id == order["owner_id"] or not message.reply_to_message:
        return
    parent_id = message.reply_to_message.message_id
    parent_author = None
    if parent_id == order["message_id"]:
        linked = True
    else:
        async with aiosqlite.connect(DB_NAME) as db:
            row = await (await db.execute("SELECT author_id FROM factory_order_messages WHERE order_id=? AND message_id=?",
                                         (order["id"], parent_id))).fetchone()
        linked = bool(row); parent_author = int(row[0]) if row else None
    if not linked:
        return
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''INSERT OR IGNORE INTO factory_order_participants
            (order_id,user_id,display_name,joined_at) VALUES(?,?,?,?)''',
            (order["id"], message.from_user.id, message.from_user.full_name, now))
        await db.execute('''INSERT OR IGNORE INTO factory_order_messages
            (order_id,message_id,author_id,parent_author_id) VALUES(?,?,?,?)''',
            (order["id"], message.message_id, message.from_user.id, parent_author))
        if parent_author and parent_author != message.from_user.id:
            await db.execute('''UPDATE factory_order_participants SET replies_received=replies_received+1
                WHERE order_id=? AND user_id=?''', (order["id"], parent_author))
        counts = await (await db.execute('''SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN parent_author_id IS NOT NULL AND parent_author_id!=author_id THEN 1 ELSE 0 END),0)
            FROM factory_order_messages WHERE order_id=?''', (order["id"],))).fetchone()
        people = await (await db.execute("SELECT COUNT(*) FROM factory_order_participants WHERE order_id=?",
                                        (order["id"],))).fetchone()
        await db.commit()
    await _mark_dirty("factory_order_participants", "factory_order_messages")
    if int(people[0]) >= 5 and int(counts[1]) >= 10:
        async with _order_locks[order["id"]]:
            current = await _order(order_id=order["id"])
            if current and current["status"] == "active":
                await _finish_discussion(message.bot, current)


async def track_media_message(message):
    order = await _order(chat_id=message.chat.id)
    if not order or order["order_type"] != "photo" or order["status"] != "active":
        return
    if message.from_user.id == order["owner_id"] or not message.reply_to_message or message.reply_to_message.message_id != order["message_id"]:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''INSERT OR IGNORE INTO factory_order_participants
            (order_id,user_id,display_name,submission_message_id,joined_at) VALUES(?,?,?,?,?)''',
            (order["id"], message.from_user.id, message.from_user.full_name, message.message_id, int(time.time())))
        await db.commit()
    await _mark_dirty("factory_order_participants")


async def _pay(order, payouts):
    paid = 0
    level_changes = set()
    month_key = datetime.now().strftime("%Y-%m")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        current = await (await db.execute("SELECT status FROM factory_orders WHERE id=?", (order["id"],))).fetchone()
        if not current or current[0] not in ("active", "voting", "tournament"):
            await db.rollback(); return
        for uid, raw_amount in payouts.items():
            amount = int(raw_amount)
            if amount <= 0:
                continue
            row = await (await db.execute("SELECT xp,level FROM users WHERE user_id=?", (uid,))).fetchone()
            if not row:
                continue
            old_lvl = int(row[1])
            xp, lvl = int(row[0]) + amount, old_lvl
            while lvl < 5 and xp >= LEVEL_CAPS[lvl]:
                xp -= int(LEVEL_CAPS[lvl]); lvl += 1
            await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, lvl, uid))
            if lvl != old_lvl:
                level_changes.add(int(uid))
            await db.execute('''INSERT INTO month_scores(month_key,user_id,xp_earned) VALUES(?,?,?)
                ON CONFLICT(month_key,user_id) DO UPDATE SET xp_earned=xp_earned+excluded.xp_earned''',
                (month_key, uid, amount))
            paid += amount
        await db.execute('''UPDATE factory_orders SET status='completed',distributed_xp=?,metadata=? WHERE id=?''',
                         (paid, json.dumps({"paid": paid}), order["id"]))
        await db.commit()
    await _mark_dirty("users", "month_scores", "factory_orders")
    for user_id in level_changes:
        queue_level_tag_refresh(user_id)


async def _finish_discussion(bot, order):
    participants = await _participants(order["id"])
    eligible = [p for p in participants if p["user_id"] != order["owner_id"] and p["replies_received"] > 0]
    owner_share = order["xp_bank"] * 20 // 100
    pool = order["xp_bank"] - owner_share
    payouts = {order["owner_id"]: owner_share}
    if eligible:
        each = pool // len(eligible)
        for p in eligible: payouts[p["user_id"]] = each
        payouts[order["owner_id"]] += pool - each * len(eligible)
    else:
        payouts[order["owner_id"]] += pool
    await _pay(order, payouts)
    await _cleanup_order_messages(bot, order)
    await _clear_order_message_refs(order["id"])
    await _send_temp_result(
        bot,
        order["chat_id"],
        f"✅ <b>Заказ #{order['id']} выполнен.</b> "
        f"Банк распределён между организатором и {len(eligible)} участниками диалога.",
    )


async def _fail(bot, order, reason):
    level_changes = set()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        current = await (await db.execute("SELECT status FROM factory_orders WHERE id=?", (order["id"],))).fetchone()
        if not current or current[0] not in ("active", "voting", "tournament"):
            await db.rollback(); return
        await db.execute("UPDATE farm_players SET coins=coins+?,updated_at=? WHERE user_id=?",
                         (order["coin_cost"] * 80 // 100, int(time.time()), order["owner_id"]))
        if order["order_type"] == "tournament":
            stake = int(json.loads(order["metadata"] or "{}").get("stake", 0))
            players = await (await db.execute(
                "SELECT user_id FROM factory_order_participants WHERE order_id=?", (order["id"],))).fetchall()
            for (uid,) in players:
                row = await (await db.execute("SELECT xp,level FROM users WHERE user_id=?", (uid,))).fetchone()
                if not row: continue
                old_lvl = int(row[1])
                xp, lvl = int(row[0]) + stake, old_lvl
                while lvl < 5 and xp >= LEVEL_CAPS[lvl]: xp -= int(LEVEL_CAPS[lvl]); lvl += 1
                await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, lvl, uid))
                if lvl != old_lvl:
                    level_changes.add(int(uid))
        await db.execute("UPDATE factory_orders SET status='failed' WHERE id=?", (order["id"],))
        await db.commit()
    await _mark_dirty("farm_players", "users", "factory_orders")
    for user_id in level_changes:
        queue_level_tag_refresh(user_id)
    await _cleanup_order_messages(bot, order)
    await _clear_order_message_refs(order["id"])
    await _send_temp_result(
        bot,
        order["chat_id"],
        f"❌ <b>Заказ #{order['id']} не выполнен:</b> {reason}\n"
        "Возвращено 80% монет завода.",
    )


async def _start_vote(bot, order):
    people = await _participants(order["id"])
    if len(people) < 4:
        return await _fail(bot, order, "не набрано 4 фотоработы")
    rows = [[InlineKeyboardButton(text=f"{i}. {p['display_name']}", callback_data=f"fvote:{order['id']}:{p['user_id']}")]
            for i, p in enumerate(people, 1)]
    sent = await bot.send_message(order["chat_id"], f"🗳 <b>Голосование по заказу #{order['id']}</b>\nОдин голос. За себя голосовать нельзя. Голосование идет 1 час.",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    try:
        await _set_order(
            order["id"],
            status="voting",
            vote_message_id=sent.message_id,
            stage_ends_at=int(time.time()) + 3600,
        )
    except Exception:
        await _delete_message_id(bot, order["chat_id"], sent.message_id)
        raise
    await _delete_message_id(bot, order["chat_id"], order["message_id"])


@router.callback_query(F.data.startswith("fvote:"))
async def vote(callback: types.CallbackQuery):
    _, raw_order, raw_candidate = callback.data.split(":")
    order = await _order(order_id=int(raw_order))
    if not order or order["status"] != "voting": return await callback.answer("Голосование завершено.", show_alert=True)
    candidate = int(raw_candidate)
    if candidate == callback.from_user.id: return await callback.answer("За себя голосовать нельзя.", show_alert=True)
    ids = {p["user_id"] for p in await _participants(order["id"])}
    if candidate not in ids: return await callback.answer("Работа не найдена.", show_alert=True)
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO factory_order_votes(order_id,voter_id,candidate_id) VALUES(?,?,?)",
                             (order["id"], callback.from_user.id, candidate)); await db.commit()
        except aiosqlite.IntegrityError:
            return await callback.answer("Вы уже проголосовали.", show_alert=True)
    await _mark_dirty("factory_order_votes")
    await callback.answer("Голос принят.")


async def _finish_photo(bot, order):
    people = await _participants(order["id"])
    async with aiosqlite.connect(DB_NAME) as db:
        votes = await (await db.execute('''SELECT candidate_id,COUNT(*) c FROM factory_order_votes
            WHERE order_id=? GROUP BY candidate_id ORDER BY c DESC''', (order["id"],))).fetchall()
    max_votes = max((v[1] for v in votes), default=0)
    winners = {v[0] for v in votes if v[1] == max_votes} if max_votes else {people[0]["user_id"]}
    payouts = {order["owner_id"]: order["xp_bank"] * 20 // 100}
    win_pool = order["xp_bank"] * 50 // 100
    rest_pool = order["xp_bank"] - payouts[order["owner_id"]] - win_pool
    for uid in winners: payouts[uid] = payouts.get(uid, 0) + win_pool // len(winners)
    others = [p["user_id"] for p in people if p["user_id"] not in winners]
    if others:
        for uid in others: payouts[uid] = payouts.get(uid, 0) + rest_pool // len(others)
    else: payouts[order["owner_id"]] += rest_pool
    await _pay(order, payouts)
    await _cleanup_order_messages(bot, order)
    await _clear_order_message_refs(order["id"])
    await _send_temp_result(
        bot,
        order["chat_id"],
        f"🏆 <b>Фотосмена #{order['id']} завершена.</b> Победителей: {len(winners)}.",
    )


def _current_tournament_match(meta):
    current = str(meta.get("current_match") or "")
    if current in ("1", "2", "3"):
        return current
    semis = meta.get("semis") or {}
    if "1" not in semis:
        return "1"
    if "2" not in semis:
        return "2"
    return "3"


async def _start_tournament(bot, order):
    people = await _participants(order["id"])
    if len(people) != 4:
        return False, f"Для старта нужно ровно 4 участника. Сейчас: {len(people)}."
    meta = json.loads(order["metadata"] or "{}")
    meta.update({
        "matches": {
            "1": [people[0]["user_id"], people[1]["user_id"]],
            "2": [people[2]["user_id"], people[3]["user_id"]],
        },
        "choices": {},
        "semis": {},
        "current_match": "1",
    })
    await _set_order(
        order["id"],
        status="tournament",
        stage_ends_at=int(time.time()) + 3600,
        metadata=json.dumps(meta),
    )
    await _delete_message_id(bot, order["chat_id"], order["message_id"])
    current = await _order(order_id=order["id"])
    sent = await _send_tournament_stage_or_cancel(
        bot,
        current,
        meta,
        f"🏭 <b>Турнир #{order['id']}: полуфинал 1</b>\n"
        "Оба игрока выбирают скрытую тактику.",
        _kb_tactics(order["id"], 1),
    )
    if not sent:
        return False, "Турнир отменён: Telegram не смог опубликовать бой. Все взносы возвращены."
    return True, "Турнир начат."


async def _resolve_tournament_match(bot, order_id, match_no, winner_pos):
    """Resolve one current match and perform the only valid state transition."""
    order = await _order(order_id=int(order_id))
    if not order or order["status"] != "tournament":
        return False, "Турнир уже завершён."
    meta = json.loads(order["metadata"] or "{}")
    match_no = str(match_no)
    if _current_tournament_match(meta) != match_no:
        return False, "Этот раунд уже завершён."
    pair = (meta.get("matches") or {}).get(match_no)
    if not pair or len(pair) != 2:
        return False, "Пара текущего раунда повреждена."

    await _delete_message_id(bot, order["chat_id"], (meta.get("stage_message_id") or 0))
    if winner_pos is None:
        meta.setdefault("choices", {})[match_no] = {}
        sent = await _send_tournament_stage_or_cancel(
            bot,
            order,
            meta,
            "🤝 Одинаковая тактика — переигровка.",
            _kb_tactics(order["id"], int(match_no)),
        )
        if not sent:
            return False, "Турнир отменён из-за ошибки публикации боя."
        return True, "Назначена переигровка."

    winner = pair[int(winner_pos) - 1]
    if match_no in ("1", "2"):
        meta.setdefault("semis", {})[match_no] = winner
        next_match = "2" if match_no == "1" else "3"
        meta["current_match"] = next_match
        if match_no == "2":
            meta.setdefault("matches", {})["3"] = [meta["semis"]["1"], meta["semis"]["2"]]
        title = (
            f"🏭 <b>Турнир #{order['id']}: полуфинал 2</b>"
            if next_match == "2"
            else f"🏆 <b>Турнир #{order['id']}: финал</b>"
        )
        sent = await _send_tournament_stage_or_cancel(
            bot,
            order,
            meta,
            title,
            _kb_tactics(order["id"], int(next_match)),
        )
        if not sent:
            return False, "Турнир отменён из-за ошибки публикации боя."
        return True, "Открыт следующий бой."

    finalist = pair[1] if winner == pair[0] else pair[0]
    stake = int(meta["stake"])
    total_bank = order["xp_bank"] + stake * 4
    payouts = {
        winner: total_bank * 65 // 100,
        finalist: total_bank * 25 // 100,
        order["owner_id"]: total_bank - (total_bank * 65 // 100) - (total_bank * 25 // 100),
    }
    await _pay(order, payouts)
    await _cleanup_order_messages(bot, order)
    await _clear_order_message_refs(order["id"])
    await _send_temp_result(
        bot,
        order["chat_id"],
        f"🏆 <b>Турнир #{order['id']} завершён.</b> "
        "Победитель получает 65% банка, финалист 25%, организатор 10%.",
    )
    return True, "Турнир завершён."


async def _join_tournament_atomically(order_id, user_id, username, full_name):
    """Take the stake and register the player in one SQLite transaction."""
    await get_user(user_id, username, full_name)
    level_changed = False
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        order = await (await db.execute(
            """SELECT status,order_type,metadata FROM factory_orders
               WHERE id=?""",
            (int(order_id),),
        )).fetchone()
        if not order or order[0] != "active" or order[1] != "tournament":
            await db.rollback()
            return False, "Регистрация закрыта.", 0
        existing = await (await db.execute(
            """SELECT 1 FROM factory_order_participants
               WHERE order_id=? AND user_id=?""",
            (int(order_id), int(user_id)),
        )).fetchone()
        if existing:
            await db.rollback()
            return False, "Вы уже участвуете.", 0
        count = int((await (await db.execute(
            "SELECT COUNT(*) FROM factory_order_participants WHERE order_id=?",
            (int(order_id),),
        )).fetchone())[0])
        if count >= 4:
            await db.rollback()
            return False, "Все места уже заняты.", count
        stake = int(json.loads(order[2] or "{}").get("stake") or 0)
        state = await (await db.execute(
            "SELECT xp,level FROM users WHERE user_id=?",
            (int(user_id),),
        )).fetchone()
        if not state or total_available_xp(state[0], state[1]) < stake:
            await db.rollback()
            return False, "Не хватает XP на взнос.", count
        xp, level = int(state[0]) - stake, int(state[1])
        old_level = level
        while xp < 0 and level > 1:
            level -= 1
            xp += int(LEVEL_CAPS[level])
        if xp < 0:
            await db.rollback()
            return False, "Не хватает XP на взнос.", count
        await db.execute(
            "UPDATE users SET xp=?,level=? WHERE user_id=?",
            (xp, level, int(user_id)),
        )
        await db.execute(
            """INSERT INTO factory_order_participants
               (order_id,user_id,display_name,joined_at) VALUES(?,?,?,?)""",
            (int(order_id), int(user_id), full_name, int(time.time())),
        )
        await db.commit()
        level_changed = level != old_level
        count += 1
    await _mark_dirty("users", "factory_order_participants")
    if level_changed:
        queue_level_tag_refresh(user_id)
    return True, f"Вы в турнире: {count}/4", count


@router.callback_query(F.data.startswith("fjoin:"))
@_serialized_order
async def tournament_join(callback: types.CallbackQuery):
    order = await _order(order_id=int(callback.data.split(":")[1]))
    if not order or order["order_type"] != "tournament" or order["status"] != "active":
        return await callback.answer("Регистрация закрыта.", show_alert=True)
    if callback.from_user.id == order["owner_id"]: return await callback.answer("Организатор не участвует.", show_alert=True)
    joined, result, count = await _join_tournament_atomically(
        order["id"],
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name,
    )
    if not joined:
        return await callback.answer(result, show_alert=True)
    await callback.answer(result)
    if count == 4:
        current = await _order(order_id=order["id"])
        await _start_tournament(callback.bot, current)


def _duel_winner(a, b):
    if a == b: return None
    favored = 1 if (a, b) in (("atk", "trick"), ("def", "atk"), ("trick", "def")) else 2
    return favored if random.random() < .70 else (2 if favored == 1 else 1)


@router.callback_query(F.data.startswith("ftac:"))
@_serialized_order
async def tournament_tactic(callback: types.CallbackQuery):
    _, oid, match_no, choice = callback.data.split(":")
    order = await _order(order_id=int(oid))
    if not order or order["status"] != "tournament": return await callback.answer("Раунд завершен.", show_alert=True)
    meta = json.loads(order["metadata"])
    if _current_tournament_match(meta) != str(match_no):
        return await callback.answer("Этот раунд уже завершён.", show_alert=True)
    pair = meta["matches"].get(match_no)
    if not pair or callback.from_user.id not in pair: return await callback.answer("Вы не играете в этом раунде.", show_alert=True)
    choices = meta.setdefault("choices", {}).setdefault(match_no, {})
    key = str(callback.from_user.id)
    if key in choices: return await callback.answer("Вы уже выбрали.", show_alert=True)
    choices[key] = choice; await _set_order(order["id"], metadata=json.dumps(meta)); await callback.answer("Тактика принята.")
    if len(choices) < 2: return
    winner_pos = _duel_winner(choices[str(pair[0])], choices[str(pair[1])])
    await _resolve_tournament_match(callback.bot, order["id"], match_no, winner_pos)


async def get_factory_order_snapshot(chat_id: int = None, order_id: int = None):
    if order_id is not None:
        order = await _order(order_id=int(order_id))
    elif chat_id is not None:
        order = await _order(chat_id=int(chat_id))
    else:
        raise ValueError("chat_id or order_id is required")
    if not order:
        return None
    people = await _participants(order["id"])
    snapshot = {
        "order": order,
        "participants": len(people),
        "participant_names": {int(p["user_id"]): p["display_name"] for p in people},
        "replies": 0,
        "votes": 0,
        "current_match": None,
        "current_pair": [],
    }
    async with aiosqlite.connect(DB_NAME) as db:
        if order["order_type"] == "discussion":
            row = await (await db.execute(
                """SELECT COUNT(*) FROM factory_order_messages
                   WHERE order_id=? AND parent_author_id IS NOT NULL
                     AND parent_author_id!=author_id""",
                (order["id"],),
            )).fetchone()
            snapshot["replies"] = int(row[0] or 0)
        elif order["order_type"] == "photo":
            row = await (await db.execute(
                "SELECT COUNT(*) FROM factory_order_votes WHERE order_id=?",
                (order["id"],),
            )).fetchone()
            snapshot["votes"] = int(row[0] or 0)
    if order["order_type"] == "tournament" and order["status"] == "tournament":
        meta = json.loads(order["metadata"] or "{}")
        current_match = _current_tournament_match(meta)
        snapshot["current_match"] = current_match
        snapshot["current_pair"] = list((meta.get("matches") or {}).get(current_match) or [])
    return snapshot


async def admin_advance_factory_order(bot, order_id: int):
    """Advance only through valid production transitions; no synthetic participants are created."""
    async with _order_locks[int(order_id)]:
        order = await _order(order_id=int(order_id))
        if not order:
            return False, "Событие уже завершено."
        people = await _participants(order["id"])
        if order["status"] == "active" and order["order_type"] == "discussion":
            async with aiosqlite.connect(DB_NAME) as db:
                row = await (await db.execute(
                    """SELECT COUNT(*) FROM factory_order_messages
                       WHERE order_id=? AND parent_author_id IS NOT NULL
                         AND parent_author_id!=author_id""",
                    (order["id"],),
                )).fetchone()
            replies = int(row[0] or 0)
            if len(people) < 5 or replies < 10:
                return False, f"Пока нельзя завершить: участников {len(people)}/5, ответов друг другу {replies}/10."
            await _finish_discussion(bot, order)
            return True, "Обсуждение завершено и награды выданы."
        if order["status"] == "active" and order["order_type"] == "photo":
            if len(people) < 4:
                return False, f"Для голосования нужно 4 фотоработы. Сейчас: {len(people)}."
            await _start_vote(bot, order)
            return True, "Голосование запущено."
        if order["status"] == "voting" and order["order_type"] == "photo":
            if len(people) < 4:
                return False, "Событие повреждено: в голосовании меньше четырёх работ."
            await _finish_photo(bot, order)
            return True, "Голосование завершено и награды выданы."
        if order["status"] == "active" and order["order_type"] == "tournament":
            return await _start_tournament(bot, order)
        if order["status"] == "tournament":
            meta = json.loads(order["metadata"] or "{}")
            match_no = _current_tournament_match(meta)
            return await _resolve_tournament_match(
                bot,
                order["id"],
                match_no,
                random.randint(1, 2),
            )
        return False, "Для этого состояния нет следующего шага."


async def _cancel_factory_order_unlocked(bot, order_id: int, reason):
    order = await _order(order_id=int(order_id))
    if not order:
        return False, "Событие уже завершено."
    level_changes = set()
    refunded_stakes = 0
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        changed = await db.execute(
            """UPDATE factory_orders SET status='cancelled'
               WHERE id=? AND status IN ('active','voting','tournament')""",
            (order["id"],),
        )
        if changed.rowcount != 1:
            await db.rollback()
            return False, "Событие уже завершено."
        if int(order["coin_cost"] or 0) > 0:
            await db.execute(
                "UPDATE farm_players SET coins=coins+?,updated_at=? WHERE user_id=?",
                (int(order["coin_cost"]), int(time.time()), int(order["owner_id"])),
            )
        if order["order_type"] == "tournament":
            meta = json.loads(order["metadata"] or "{}")
            stake = int(meta.get("stake") or 0)
            players = await (await db.execute(
                "SELECT user_id FROM factory_order_participants WHERE order_id=?",
                (order["id"],),
            )).fetchall()
            for (user_id,) in players:
                state = await (await db.execute(
                    "SELECT xp,level FROM users WHERE user_id=?",
                    (user_id,),
                )).fetchone()
                if not state:
                    continue
                old_level = int(state[1])
                xp, level = int(state[0]) + stake, old_level
                while level < 5 and xp >= LEVEL_CAPS[level]:
                    xp -= int(LEVEL_CAPS[level])
                    level += 1
                await db.execute(
                    "UPDATE users SET xp=?,level=? WHERE user_id=?",
                    (xp, level, user_id),
                )
                refunded_stakes += stake
                if level != old_level:
                    level_changes.add(int(user_id))
        await db.commit()
    await _mark_dirty("factory_orders", "farm_players", "users")
    for user_id in level_changes:
        queue_level_tag_refresh(user_id)
    await _cleanup_order_messages(bot, order)
    await _clear_order_message_refs(order["id"])
    if order["order_type"] == "tournament" and refunded_stakes:
        suffix = " Турнирные взносы возвращены полностью."
    elif int(order["coin_cost"] or 0) > 0:
        suffix = " Монеты организатора возвращены полностью."
    else:
        suffix = ""
    try:
        await _send_temp_result(
            bot,
            order["chat_id"],
            f"⏹ <b>Заказ #{order['id']} остановлен:</b> {html.escape(reason)}.{suffix}",
        )
    except Exception:
        pass
    return True, "Событие остановлено без потери монет и турнирных взносов."


async def admin_cancel_factory_order(bot, order_id: int, reason="остановлено администратором"):
    """Cancel safely: full coin and tournament-stake refund, then remove all anchors."""
    async with _order_locks[int(order_id)]:
        return await _cancel_factory_order_unlocked(bot, order_id, reason)


async def process_factory_deadlines(bot):
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute('''SELECT * FROM factory_orders
            WHERE status IN ('active','voting','tournament') AND stage_ends_at<=?''', (now,))).fetchall()
    for order in rows:
        try:
            async with _order_locks[order["id"]]:
                current = await _order(order_id=order["id"])
                if not current or current["stage_ends_at"] > now:
                    continue
                if current["status"] == "voting": await _finish_photo(bot, current)
                elif current["status"] == "active" and current["order_type"] == "photo": await _start_vote(bot, current)
                elif current["status"] == "active": await _fail(bot, current, "условия не выполнены вовремя")
                else: await _fail(bot, current, "турнир не завершен вовремя")
        except Exception:
            logging.exception("Ошибка обработки дедлайна заводского заказа #%s", order["id"])


async def cleanup_finished_factory_messages(bot):
    """One-time cleanup for messages left behind by earlier bot versions."""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT * FROM factory_orders
               WHERE status IN ('completed','failed')
                 AND (message_id!=0 OR vote_message_id!=0 OR metadata LIKE '%stage_message_id%')"""
        )).fetchall()
    for order in rows:
        await _cleanup_order_messages(bot, order)
        try:
            meta = json.loads(order["metadata"] or "{}")
        except Exception:
            meta = {}
        meta.pop("stage_message_id", None)
        await _set_order(
            order["id"],
            message_id=0,
            vote_message_id=0,
            metadata=json.dumps(meta),
        )


async def _processor(bot):
    try:
        await cleanup_finished_factory_messages(bot)
    except Exception:
        logging.exception("Не удалось очистить старые сообщения заводских заказов")
    while True:
        try:
            await process_factory_deadlines(bot)
        except Exception:
            logging.exception("Ошибка цикла заводских заказов")
        await asyncio.sleep(30)


def start_factory_processor(bot):
    global _processor_task
    if not _processor_task or _processor_task.done(): _processor_task = asyncio.create_task(_processor(bot))
