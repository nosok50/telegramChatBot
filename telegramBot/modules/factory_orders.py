import asyncio
import html
import json
import random
import time
from collections import defaultdict
from functools import wraps
from datetime import datetime

import aiosqlite
from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import (DB_NAME, farm_spend_coins, farm_is_complete, get_user,
                      total_available_xp, update_xp, _mark_dirty, LEVEL_CAPS)


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


def _serialized_order_creation(handler):
    @wraps(handler)
    async def wrapped(message, *args, **kwargs):
        async with _chat_order_locks[message.chat.id]:
            return await handler(message, *args, **kwargs)
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
@_serialized_order_creation
async def create_order(message: types.Message, command: CommandObject):
    if message.chat.type == "private":
        return await message.answer("Заводской заказ запускается в групповом чате.")
    args = (command.args or "").split(maxsplit=2)
    if len(args) < 2:
        return await message.answer(
            "<b>Заводские заказы</b>\n"
            "<code>/factory_order discussion small тема</code>\n"
            "<code>/factory_order photo medium тема</code>\n"
            "<code>/factory_order tournament large</code>\n\n"
            "Размеры: small / medium / large.")
    order_type = TYPE_ALIASES.get(args[0].lower())
    size = SIZE_ALIASES.get(args[1].lower())
    topic = args[2].strip() if len(args) > 2 else ""
    if not order_type or not size or (order_type != "tournament" and len(topic) < 5):
        return await message.answer("Неверный тип/размер либо тема короче 5 символов.")
    if not await farm_is_complete(message.from_user.id):
        return await message.answer("🔒 Заказы открываются после полной застройки и прокачки всех 9 клеток завода.")
    if await _order(chat_id=message.chat.id):
        return await message.answer("В этом чате уже идет заводской заказ.")
    cost, bank, stake = ORDER_SIZES[size]
    if not await farm_spend_coins(message.from_user.id, cost):
        return await message.answer(f"Не хватает монет завода. Нужно <code>{cost:,}</code>.")
    now = int(time.time())
    duration = 1800 if order_type == "tournament" else 6 * 3600
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute('''INSERT INTO factory_orders
            (chat_id,owner_id,order_type,size,coin_cost,xp_bank,topic,status,created_at,stage_ends_at,metadata)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (message.chat.id, message.from_user.id, order_type, size,
            cost, bank, topic, "active", now, now + duration, json.dumps({"stake": stake})))
        order_id = cur.lastrowid; await db.commit()
    await _mark_dirty("factory_orders")
    names = {"discussion": "Тема смены", "photo": "Фотосмена", "tournament": "Турнир цехов"}
    conditions = {
        "discussion": "Нужно 5 участников и 10 ответов друг другу. Отвечайте внутри этой ветки.",
        "photo": "Нужно 4 участника. Пришлите по одному фото/видео ответом на это сообщение.",
        "tournament": f"Нужно 4 участника. Взнос каждого — {stake} XP. Регистрация 30 минут.",
    }
    text = (f"🏭 <b>{names[order_type]}</b> · заказ #{order_id}\n"
            f"Организатор: {message.from_user.mention_html()}\n"
            f"Банк завода: <code>{bank} XP</code>\n"
            + (f"Тема: <b>{html.escape(topic)}</b>\n" if topic else "") + f"\n{conditions[order_type]}")
    sent = await message.answer(text, reply_markup=_kb_tournament(order_id) if order_type == "tournament" else None)
    await _set_order(order_id, message_id=sent.message_id)


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
            xp, lvl = int(row[0]) + amount, int(row[1])
            while lvl < 5 and xp >= LEVEL_CAPS[lvl]:
                xp -= int(LEVEL_CAPS[lvl]); lvl += 1
            await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, lvl, uid))
            await db.execute('''INSERT INTO month_scores(month_key,user_id,xp_earned) VALUES(?,?,?)
                ON CONFLICT(month_key,user_id) DO UPDATE SET xp_earned=xp_earned+excluded.xp_earned''',
                (month_key, uid, amount))
            paid += amount
        await db.execute('''UPDATE factory_orders SET status='completed',distributed_xp=?,metadata=? WHERE id=?''',
                         (paid, json.dumps({"paid": paid}), order["id"]))
        await db.commit()
    await _mark_dirty("users", "month_scores", "factory_orders")


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
    await bot.send_message(order["chat_id"], f"✅ <b>Заказ #{order['id']} выполнен.</b> Банк распределен между организатором и {len(eligible)} участниками диалога.")


async def _fail(bot, order, reason):
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
                xp, lvl = int(row[0]) + stake, int(row[1])
                while lvl < 5 and xp >= LEVEL_CAPS[lvl]: xp -= int(LEVEL_CAPS[lvl]); lvl += 1
                await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, lvl, uid))
        await db.execute("UPDATE factory_orders SET status='failed' WHERE id=?", (order["id"],))
        await db.commit()
    await _mark_dirty("farm_players", "users", "factory_orders")
    await bot.send_message(order["chat_id"], f"❌ <b>Заказ #{order['id']} не выполнен:</b> {reason}\nВозвращено 80% монет завода.")


async def _start_vote(bot, order):
    people = await _participants(order["id"])
    if len(people) < 4:
        return await _fail(bot, order, "не набрано 4 фотоработы")
    rows = [[InlineKeyboardButton(text=f"{i}. {p['display_name']}", callback_data=f"fvote:{order['id']}:{p['user_id']}")]
            for i, p in enumerate(people, 1)]
    sent = await bot.send_message(order["chat_id"], f"🗳 <b>Голосование по заказу #{order['id']}</b>\nОдин голос. За себя голосовать нельзя. Голосование идет 1 час.",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await _set_order(order["id"], status="voting", vote_message_id=sent.message_id, stage_ends_at=int(time.time()) + 3600)


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
    await bot.send_message(order["chat_id"], f"🏆 <b>Фотосмена #{order['id']} завершена.</b> Победителей: {len(winners)}.")


@router.callback_query(F.data.startswith("fjoin:"))
@_serialized_order
async def tournament_join(callback: types.CallbackQuery):
    order = await _order(order_id=int(callback.data.split(":")[1]))
    if not order or order["order_type"] != "tournament" or order["status"] != "active":
        return await callback.answer("Регистрация закрыта.", show_alert=True)
    if callback.from_user.id == order["owner_id"]: return await callback.answer("Организатор не участвует.", show_alert=True)
    people = await _participants(order["id"])
    if any(p["user_id"] == callback.from_user.id for p in people): return await callback.answer("Вы уже участвуете.")
    meta = json.loads(order["metadata"] or "{}"); stake = int(meta["stake"])
    data = await get_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    if total_available_xp(data[3], data[4]) < stake: return await callback.answer("Не хватает XP на взнос.", show_alert=True)
    await update_xp(callback.from_user.id, -stake)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''INSERT INTO factory_order_participants(order_id,user_id,display_name,joined_at)
            VALUES(?,?,?,?)''', (order["id"], callback.from_user.id, callback.from_user.full_name, int(time.time()))); await db.commit()
    await _mark_dirty("factory_order_participants")
    people = await _participants(order["id"])
    await callback.answer(f"Вы в турнире: {len(people)}/4")
    if len(people) == 4:
        meta.update({"matches": {"1": [people[0]["user_id"], people[1]["user_id"]],
                                  "2": [people[2]["user_id"], people[3]["user_id"]]}, "choices": {}})
        await _set_order(order["id"], status="tournament", stage_ends_at=int(time.time()) + 3600, metadata=json.dumps(meta))
        await callback.bot.send_message(order["chat_id"], f"🏭 <b>Турнир #{order['id']}: полуфинал 1</b>\nОба игрока выбирают скрытую тактику.", reply_markup=_kb_tactics(order["id"], 1))


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
    meta = json.loads(order["metadata"]); pair = meta["matches"].get(match_no)
    if not pair or callback.from_user.id not in pair: return await callback.answer("Вы не играете в этом раунде.", show_alert=True)
    choices = meta.setdefault("choices", {}).setdefault(match_no, {})
    key = str(callback.from_user.id)
    if key in choices: return await callback.answer("Вы уже выбрали.", show_alert=True)
    choices[key] = choice; await _set_order(order["id"], metadata=json.dumps(meta)); await callback.answer("Тактика принята.")
    if len(choices) < 2: return
    winner_pos = _duel_winner(choices[str(pair[0])], choices[str(pair[1])])
    if winner_pos is None:
        meta["choices"][match_no] = {}; await _set_order(order["id"], metadata=json.dumps(meta))
        return await callback.bot.send_message(order["chat_id"], "🤝 Одинаковая тактика — переигровка.", reply_markup=_kb_tactics(order["id"], int(match_no)))
    winner = pair[winner_pos - 1]
    if match_no in ("1", "2"):
        meta.setdefault("semis", {})[match_no] = winner
        if match_no == "1":
            await _set_order(order["id"], metadata=json.dumps(meta))
            return await callback.bot.send_message(order["chat_id"], f"🏭 <b>Турнир #{order['id']}: полуфинал 2</b>", reply_markup=_kb_tactics(order["id"], 2))
        meta["matches"]["3"] = [meta["semis"]["1"], meta["semis"]["2"]]
        await _set_order(order["id"], metadata=json.dumps(meta))
        return await callback.bot.send_message(order["chat_id"], f"🏆 <b>Турнир #{order['id']}: финал</b>", reply_markup=_kb_tactics(order["id"], 3))
    finalist = pair[1] if winner == pair[0] else pair[0]
    stake = int(meta["stake"]); total_bank = order["xp_bank"] + stake * 4
    payouts = {winner: total_bank * 65 // 100, finalist: total_bank * 25 // 100,
               order["owner_id"]: total_bank - (total_bank * 65 // 100) - (total_bank * 25 // 100)}
    await _pay(order, payouts)
    await callback.bot.send_message(order["chat_id"], f"🏆 <b>Турнир #{order['id']} завершен.</b> Победитель получает 65% банка, финалист 25%, организатор 10%.")


async def process_factory_deadlines(bot):
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute('''SELECT * FROM factory_orders
            WHERE status IN ('active','voting','tournament') AND stage_ends_at<=?''', (now,))).fetchall()
    for order in rows:
        async with _order_locks[order["id"]]:
            current = await _order(order_id=order["id"])
            if not current or current["stage_ends_at"] > now:
                continue
            if current["status"] == "voting": await _finish_photo(bot, current)
            elif current["status"] == "active" and current["order_type"] == "photo": await _start_vote(bot, current)
            elif current["status"] == "active": await _fail(bot, current, "условия не выполнены вовремя")
            else: await _fail(bot, current, "турнир не завершен вовремя")


async def _processor(bot):
    while True:
        try: await process_factory_deadlines(bot)
        except Exception: pass
        await asyncio.sleep(30)


def start_factory_processor(bot):
    global _processor_task
    if not _processor_task or _processor_task.done(): _processor_task = asyncio.create_task(_processor(bot))
