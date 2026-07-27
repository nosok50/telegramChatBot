import html
import time

import aiosqlite
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import OWNER_ID
from database import DB_NAME
from modules.factory_orders import (
    ORDER_SIZES,
    admin_advance_factory_order,
    admin_cancel_factory_order,
    get_factory_order_snapshot,
    launch_factory_order,
)
from utils import answer_temp, delete_later, touch_temp_message


router = Router()

TYPE_NAMES = {
    "discussion": "💬 Обсуждение",
    "photo": "📸 Фото",
    "tournament": "⚔️ Турнир",
}
SIZE_NAMES = {
    "small": "Малый",
    "medium": "Средний",
    "large": "Большой",
}
STATUS_NAMES = {
    "active": "сбор участников",
    "voting": "голосование",
    "tournament": "турнирные бои",
}
CHAT_SOURCE_TABLES = (
    "chat_activity_state",
    "chat_level_tags",
    "factory_orders",
    "active_duels",
    "recent_user_messages",
)


class FactoryAdminStates(StatesGroup):
    waiting_topic = State()


async def _owner_callback(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Эти кнопки доступны только владельцу.", show_alert=True)
        return False
    await touch_temp_message(callback.message)
    return True


async def _edit_panel(message, text, reply_markup):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _main_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="nav_main")],
    ])


async def get_known_factory_chats(bot):
    """Return groups seen by the bot and still accessible to it."""
    chat_ids = set()
    async with aiosqlite.connect(DB_NAME) as db:
        existing = {
            row[0] for row in await (await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).fetchall()
        }
        for table in CHAT_SOURCE_TABLES:
            if table not in existing:
                continue
            columns = {
                row[1] for row in await (await db.execute(
                    f'PRAGMA table_info("{table}")'
                )).fetchall()
            }
            if "chat_id" not in columns:
                continue
            rows = await (await db.execute(
                f'SELECT DISTINCT chat_id FROM "{table}" WHERE chat_id < 0'
            )).fetchall()
            chat_ids.update(int(row[0]) for row in rows)

    result = []
    for chat_id in sorted(chat_ids):
        try:
            chat = await bot.get_chat(chat_id)
            title = chat.title or chat.full_name or str(chat_id)
            result.append((chat_id, title))
        except Exception:
            continue
    return sorted(result, key=lambda item: item[1].lower())


async def build_factory_chat_picker(bot):
    chats = await get_known_factory_chats(bot)
    if not chats:
        text = (
            "🏭 <b>Управление событиями цеха</b>\n\n"
            "Бот пока не нашёл доступных групп. Группа появится здесь после любого нового сообщения в ней."
        )
        return text, _main_back_kb()
    rows = [
        [InlineKeyboardButton(
            text=f"💬 {title[:45]}",
            callback_data=f"facadm_chat:{chat_id}",
        )]
        for chat_id, title in chats[:30]
    ]
    rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="nav_factory_events")])
    rows.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="nav_main")])
    return (
        "🏭 <b>Управление событиями цеха</b>\n\n"
        "Выберите группу, в которой нужно запустить событие или управлять текущим:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _launch_menu(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💬 Обсуждение",
                callback_data=f"facadm_type:{chat_id}:discussion",
            ),
            InlineKeyboardButton(
                text="📸 Фото",
                callback_data=f"facadm_type:{chat_id}:photo",
            ),
        ],
        [InlineKeyboardButton(
            text="⚔️ Турнир",
            callback_data=f"facadm_type:{chat_id}:tournament",
        )],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"facadm_chat:{chat_id}")],
        [InlineKeyboardButton(text="🔙 К списку групп", callback_data="nav_factory_events")],
    ])


def _active_kb(order):
    order_id = int(order["id"])
    chat_id = int(order["chat_id"])
    if order["status"] == "active" and order["order_type"] == "discussion":
        action = "✅ Проверить и завершить"
    elif order["status"] == "active" and order["order_type"] == "photo":
        action = "🗳 Запустить голосование"
    elif order["status"] == "voting":
        action = "🏁 Завершить голосование"
    elif order["status"] == "active" and order["order_type"] == "tournament":
        action = "▶️ Запустить турнир"
    else:
        action = "🎲 Завершить текущий бой"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=action, callback_data=f"facadm_next_ask:{order_id}")],
        [InlineKeyboardButton(text="⏹ Остановить событие", callback_data=f"facadm_stop_ask:{order_id}")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"facadm_chat:{chat_id}")],
        [InlineKeyboardButton(text="🔙 К списку групп", callback_data="nav_factory_events")],
    ])


async def build_factory_admin_view(chat_id: int, chat_title=None):
    snapshot = await get_factory_order_snapshot(chat_id=chat_id)
    group_line = html.escape(chat_title or str(chat_id))
    if not snapshot:
        return (
            "🏭 <b>Управление событиями цеха</b>\n"
            f"<b>Группа:</b> {group_line}\n\n"
            "Сейчас в этой группе нет активного события.\n"
            "Админский запуск не списывает монеты завода, но награды и правила участия остаются настоящими.",
            _launch_menu(chat_id),
        )

    order = snapshot["order"]
    remaining = max(0, int(order["stage_ends_at"] or 0) - int(time.time()))
    minutes = (remaining + 59) // 60
    lines = [
        "🏭 <b>Управление событиями цеха</b>",
        f"<b>Группа:</b> {group_line}",
        "",
        f"<b>Заказ:</b> #{order['id']} · {TYPE_NAMES[order['order_type']]}",
        f"<b>Этап:</b> {STATUS_NAMES.get(order['status'], order['status'])}",
        f"<b>Размер:</b> {SIZE_NAMES[order['size']]} · банк <code>{order['xp_bank']} XP</code>",
        f"<b>До автоматического завершения этапа:</b> {minutes} мин.",
    ]
    if order["topic"]:
        lines.append(f"<b>Тема:</b> {html.escape(order['topic'])}")
    if order["order_type"] == "discussion":
        lines.append(
            f"\n<b>Прогресс:</b> участников {snapshot['participants']}/5 · "
            f"ответов друг другу {snapshot['replies']}/10"
        )
    elif order["order_type"] == "photo":
        lines.append(f"\n<b>Работы:</b> {snapshot['participants']}/4")
        if order["status"] == "voting":
            lines.append(f"<b>Голосов:</b> {snapshot['votes']}")
    elif order["status"] == "active":
        lines.append(f"\n<b>Участники турнира:</b> {snapshot['participants']}/4")
    else:
        pair_names = [
            html.escape(snapshot["participant_names"].get(int(user_id), str(user_id)))
            for user_id in snapshot["current_pair"]
        ]
        lines.append(f"\n<b>Текущий бой:</b> {snapshot['current_match']}/3")
        if pair_names:
            lines.append(f"<b>Пара:</b> {' против '.join(pair_names)}")
    return "\n".join(lines), _active_kb(order)


async def _render_chat_panel(message, bot, chat_id):
    try:
        chat = await bot.get_chat(chat_id)
        title = chat.title or chat.full_name or str(chat_id)
    except Exception:
        title = str(chat_id)
    await _edit_panel(message, *(await build_factory_admin_view(chat_id, title)))


@router.callback_query(F.data == "nav_factory_events")
async def nav_factory_events(callback: CallbackQuery, state: FSMContext):
    if not await _owner_callback(callback):
        return
    await state.clear()
    if callback.message.chat.type != "private":
        await _edit_panel(
            callback.message,
            "🏭 <b>Управление цехом доступно в личных сообщениях с ботом.</b>",
            _main_back_kb(),
        )
    else:
        await _edit_panel(callback.message, *(await build_factory_chat_picker(callback.bot)))
    await callback.answer()


@router.callback_query(F.data.startswith("facadm_chat:"))
async def select_factory_chat(callback: CallbackQuery):
    if not await _owner_callback(callback):
        return
    chat_id = int(callback.data.split(":")[1])
    await _render_chat_panel(callback.message, callback.bot, chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("facadm_type:"))
async def choose_factory_type(callback: CallbackQuery):
    if not await _owner_callback(callback):
        return
    _, raw_chat_id, order_type = callback.data.split(":")
    chat_id = int(raw_chat_id)
    if order_type not in TYPE_NAMES:
        return await callback.answer("Неизвестный тип.", show_alert=True)
    rows = []
    for size, (_cost, bank, _stake) in ORDER_SIZES.items():
        rows.append([
            InlineKeyboardButton(
                text=f"{SIZE_NAMES[size]} · {bank} XP",
                callback_data=f"facadm_size:{chat_id}:{order_type}:{size}",
            )
        ])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"facadm_chat:{chat_id}")])
    await _edit_panel(
        callback.message,
        f"{TYPE_NAMES[order_type]} <b>— выберите размер</b>\n\n"
        "Размер определяет банк XP. Монеты при админском запуске не списываются.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("facadm_size:"))
async def choose_factory_size(callback: CallbackQuery, state: FSMContext):
    if not await _owner_callback(callback):
        return
    _, raw_chat_id, order_type, size = callback.data.split(":")
    chat_id = int(raw_chat_id)
    if order_type not in TYPE_NAMES or size not in ORDER_SIZES:
        return await callback.answer("Некорректные параметры.", show_alert=True)
    if await get_factory_order_snapshot(chat_id=chat_id):
        await callback.answer("В группе уже запущено событие.", show_alert=True)
        return await _render_chat_panel(callback.message, callback.bot, chat_id)
    if order_type == "tournament":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Запустить турнир",
                callback_data=f"facadm_launch:{chat_id}:tournament:{size}",
            )],
            [InlineKeyboardButton(
                text="🔙 Назад",
                callback_data=f"facadm_type:{chat_id}:tournament",
            )],
        ])
        await _edit_panel(
            callback.message,
            f"⚔️ <b>Запустить турнир?</b>\n\n"
            f"Размер: {SIZE_NAMES[size]}\n"
            f"Банк завода: <code>{ORDER_SIZES[size][1]} XP</code>\n"
            f"Взнос каждого участника: <code>{ORDER_SIZES[size][2]} XP</code>",
            kb,
        )
    else:
        await state.set_state(FactoryAdminStates.waiting_topic)
        await state.update_data(
            target_chat_id=chat_id,
            order_type=order_type,
            size=size,
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
        )
        await _edit_panel(
            callback.message,
            f"{TYPE_NAMES[order_type]} · <b>{SIZE_NAMES[size]}</b>\n\n"
            "Напишите следующим сообщением тему задания.\n"
            "Минимум 5 символов. Сообщение с темой бот удалит.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"facadm_chat:{chat_id}")],
            ]),
        )
    await callback.answer()


async def _launch_from_panel(bot, target_chat_id, owner_name, order_type, size, topic):
    return await launch_factory_order(
        bot,
        target_chat_id,
        OWNER_ID,
        owner_name,
        order_type,
        size,
        topic,
        charge_factory=False,
    )


@router.callback_query(F.data.startswith("facadm_launch:"))
async def launch_factory_from_callback(callback: CallbackQuery):
    if not await _owner_callback(callback):
        return
    _, raw_chat_id, order_type, size = callback.data.split(":")
    target_chat_id = int(raw_chat_id)
    order, error = await _launch_from_panel(
        callback.bot,
        target_chat_id,
        callback.from_user.full_name,
        order_type,
        size,
        "",
    )
    if error:
        await callback.answer(error, show_alert=True)
    else:
        await callback.answer(f"Заказ #{order['id']} запущен.")
    await _render_chat_panel(callback.message, callback.bot, target_chat_id)


@router.message(FactoryAdminStates.waiting_topic)
async def receive_factory_topic(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    await delete_later(message, 0)
    data = await state.get_data()
    topic = (message.text or message.caption or "").strip()
    if len(topic) < 5:
        return await answer_temp(
            message,
            "Тема слишком короткая. Напишите минимум 5 символов.",
            user_id=OWNER_ID,
        )
    target_chat_id = int(data["target_chat_id"])
    order, error = await _launch_from_panel(
        message.bot,
        target_chat_id,
        message.from_user.full_name,
        data["order_type"],
        data["size"],
        topic,
    )
    if error:
        await answer_temp(message, f"⚠️ {html.escape(error)}", user_id=OWNER_ID)
    await state.clear()
    try:
        chat = await message.bot.get_chat(target_chat_id)
        title = chat.title or chat.full_name or str(target_chat_id)
    except Exception:
        title = str(target_chat_id)
    text, kb = await build_factory_admin_view(target_chat_id, title)
    try:
        await message.bot.edit_message_text(
            chat_id=int(data["panel_chat_id"]),
            message_id=int(data["panel_message_id"]),
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        await answer_temp(message, text, reply_markup=kb, user_id=OWNER_ID)


@router.callback_query(F.data.startswith("facadm_next_ask:"))
async def ask_next_factory_step(callback: CallbackQuery):
    if not await _owner_callback(callback):
        return
    order_id = int(callback.data.split(":")[1])
    snapshot = await get_factory_order_snapshot(order_id=order_id)
    if not snapshot:
        await callback.answer("Событие уже завершено.", show_alert=True)
        return await _edit_panel(callback.message, *(await build_factory_chat_picker(callback.bot)))
    chat_id = int(snapshot["order"]["chat_id"])
    await _edit_panel(
        callback.message,
        "▶️ <b>Запустить следующий шаг?</b>\n\n"
        "Бот проверит обязательные условия этапа. Если участников или работ недостаточно, состояние события не изменится.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнить", callback_data=f"facadm_next_do:{order_id}")],
            [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"facadm_chat:{chat_id}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("facadm_next_do:"))
async def do_next_factory_step(callback: CallbackQuery):
    if not await _owner_callback(callback):
        return
    order_id = int(callback.data.split(":")[1])
    snapshot = await get_factory_order_snapshot(order_id=order_id)
    if not snapshot:
        await callback.answer("Событие уже завершено.", show_alert=True)
        return await _edit_panel(callback.message, *(await build_factory_chat_picker(callback.bot)))
    chat_id = int(snapshot["order"]["chat_id"])
    ok, result = await admin_advance_factory_order(callback.bot, order_id)
    await callback.answer(result, show_alert=not ok)
    await _render_chat_panel(callback.message, callback.bot, chat_id)


@router.callback_query(F.data.startswith("facadm_stop_ask:"))
async def ask_stop_factory(callback: CallbackQuery):
    if not await _owner_callback(callback):
        return
    order_id = int(callback.data.split(":")[1])
    snapshot = await get_factory_order_snapshot(order_id=order_id)
    if not snapshot:
        await callback.answer("Событие уже завершено.", show_alert=True)
        return await _edit_panel(callback.message, *(await build_factory_chat_picker(callback.bot)))
    chat_id = int(snapshot["order"]["chat_id"])
    await _edit_panel(
        callback.message,
        "⏹ <b>Остановить событие?</b>\n\n"
        "Событие закроется, его сообщения будут удалены. Монеты организатора и турнирные взносы вернутся полностью.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏹ Да, остановить", callback_data=f"facadm_stop_do:{order_id}")],
            [InlineKeyboardButton(text="🔙 Нет", callback_data=f"facadm_chat:{chat_id}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("facadm_stop_do:"))
async def do_stop_factory(callback: CallbackQuery):
    if not await _owner_callback(callback):
        return
    order_id = int(callback.data.split(":")[1])
    snapshot = await get_factory_order_snapshot(order_id=order_id)
    if not snapshot:
        await callback.answer("Событие уже завершено.", show_alert=True)
        return await _edit_panel(callback.message, *(await build_factory_chat_picker(callback.bot)))
    chat_id = int(snapshot["order"]["chat_id"])
    ok, result = await admin_cancel_factory_order(callback.bot, order_id)
    await callback.answer(result, show_alert=not ok)
    await _render_chat_panel(callback.message, callback.bot, chat_id)
