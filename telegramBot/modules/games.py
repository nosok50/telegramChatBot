# -*- coding: utf-8 -*-
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from database import (
    get_user,
    update_xp,
    get_id_by_username,
    LEVEL_CAPS,
    farm_get_state,
    farm_unlock_cell,
    farm_buy_module,
    farm_set_move_source,
    farm_move_module,
    farm_collect_generator,
    farm_upgrade_module,
    farm_repair_module,
    farm_sell_module,
    FARM_BASE_CAP_SECONDS,
    FARM_BASE_CYCLE_SECONDS,
    FARM_GEN_BUY_COST,
    FARM_GEN_UPGRADE_COSTS,
    FARM_BATTERY_HOURS,
    FARM_SPEED_BONUS,
    FARM_STABILIZER_REDUCTION,
    FARM_SUPPORT_COSTS,
    total_available_xp, save_active_duel, load_active_duel, delete_active_duel,
    duel_pair_count_today,
    farm_is_complete,
    expire_stale_duels,
    escrow_active_duel,
    settle_active_duel,
)
from utils import (
    delete_later,
    answer_temp,
    touch_temp_message,
    get_user_link,
    ensure_sticky_message,
)
import asyncio
import html
import random
import time
from collections import defaultdict
from functools import wraps
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
_duel_processor_task = None
_duel_locks = defaultdict(asyncio.Lock)


def _serialized_duel(handler):
    @wraps(handler)
    async def wrapped(callback, *args, **kwargs):
        async with _duel_locks[callback.message.chat.id]:
            return await handler(callback, *args, **kwargs)
    return wrapped

GAME_BETS_BY_LEVEL = {
    1: (50,),
    2: (50, 100, 500),
    3: (100, 500, 2000),
    4: (500, 2000, 5000),
    5: (1000, 5000, 10000),
}

GAME_UI = {
    "dice": {
        "emoji": "🎲",
        "name": "Кости",
        "desc": "Бросаете кубик и играете на его результате.",
        "hint": "4-6 — победа x2, 1-3 — проигрыш.",
    },
    "slots": {
        "emoji": "🎰",
        "name": "Рулетка",
        "desc": "Крутите барабаны и ловите выигрышную комбинацию.",
        "hint": "777 — x10, сильная комбинация — x3, иначе проигрыш.",
    },
    "basket": {
        "emoji": "🏀",
        "name": "Баскет",
        "desc": "Бросаете мяч и получаете награду за точный бросок.",
        "hint": "Попадание — x2, чистый бросок — x3, промах — проигрыш.",
    },
}


def fmt_num(num: int) -> str:
    return "{:,}".format(num).replace(",", " ")


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds <= 0:
        return "сейчас"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if secs and not parts:
        parts.append(f"{secs}с")
    return " ".join(parts[:2])


def _farm_neighbors(cell_idx: int):
    row, col = divmod(cell_idx, 3)
    neighbors = []
    if row > 0:
        neighbors.append((row - 1) * 3 + col)
    if row < 2:
        neighbors.append((row + 1) * 3 + col)
    if col > 0:
        neighbors.append(row * 3 + (col - 1))
    if col < 2:
        neighbors.append(row * 3 + (col + 1))
    return neighbors


def _farm_generator_output(level: int, spec: str):
    if level <= 1:
        return 50, 2
    if level == 2:
        return 150, 5
    if spec == "xp":
        return 0, 15
    return 600, 0


def _farm_module_icon(m_type: str, lvl: int = 1, spec: str = "") -> str:
    if m_type == "generator":
        if lvl >= 3 and spec == "xp":
            return "🧪"
        if lvl >= 3 and spec == "coin":
            return "🏭"
        return "⚙️"
    if m_type == "speed":
        return "⚡"
    if m_type == "battery":
        return "🔋"
    if m_type == "stabilizer":
        return "🛡"
    return "⬜"


def _farm_focus_grid(selected_idx: int, effect_cells=None, effect_icon: str = "⬜", selected_icon: str = "🟩"):
    effect_cells = set(effect_cells or [])
    rows = []
    for row in range(3):
        row_cells = []
        for col in range(3):
            idx = row * 3 + col
            if idx == selected_idx:
                row_cells.append(selected_icon)
            elif idx in effect_cells:
                row_cells.append(effect_icon)
            else:
                row_cells.append("⬜")
        rows.append("".join(row_cells))
    return "\n".join(rows)


def _farm_format_resources(coins: int, xp: int) -> str:
    parts = []
    if coins > 0:
        parts.append(f"{fmt_num(coins)} 💰")
    if xp > 0:
        parts.append(f"{fmt_num(xp)} XP")
    return ", ".join(parts) if parts else "ничего"


def _farm_storage_max(level: int, spec: str, cycle_seconds: int, capacity_seconds: int):
    base_coins, base_xp = _farm_generator_output(level, spec)
    max_cycles = max(1, int(capacity_seconds // max(1, cycle_seconds)))
    return {
        "coins": int(base_coins * max_cycles),
        "xp": int(base_xp * max_cycles),
        "cycles": max_cycles,
    }


def _farm_storage_lines(current_coins: int, current_xp: int, max_coins: int, max_xp: int):
    lines = []
    if max_coins > 0:
        lines.append(f"💰 Монеты: <code>{fmt_num(current_coins)}/{fmt_num(max_coins)}</code>")
    if max_xp > 0:
        lines.append(f"⭐ XP: <code>{fmt_num(current_xp)}/{fmt_num(max_xp)}</code>")
    return lines


def _farm_generator_status(cell: dict, state: dict) -> str:
    if cell.get("stalled"):
        if cell.get("status") == "overheat":
            return "⚠️ Статус: перегрев, сбор доступен сейчас."
        return "⏸ Статус: сбор доступен сейчас."

    if int(cell.get("pending_cycles") or 0) > 0:
        if cell.get("status") == "overheat":
            return "⚠️ Статус: перегрев, сбор доступен сейчас."
        return "✅ Статус: сбор доступен сейчас."

    last_collect_ts = int(cell.get("last_collect_ts") or state.get("now_ts") or 0)
    elapsed = max(0, int(state.get("now_ts") or 0) - last_collect_ts)
    cycle_seconds = max(1, int(cell.get("cycle_seconds") or FARM_BASE_CYCLE_SECONDS))
    next_ready_in = cycle_seconds - (elapsed % cycle_seconds)
    if next_ready_in == cycle_seconds and elapsed > 0:
        next_ready_in = 0

    if cell.get("status") == "overheat":
        return f"⚠️ Статус: перегрев, сбор доступен через {fmt_duration(next_ready_in)}."
    return f"⏳ Статус: сбор доступен через {fmt_duration(next_ready_in)}."


def _farm_support_live_generators(state: dict, cell_idx: int):
    cells_by_idx = {int(cell["cell_idx"]): cell for cell in state["cells"] if cell["state"] == "module"}
    return [
        idx for idx in _farm_neighbors(cell_idx)
        if cells_by_idx.get(idx) and cells_by_idx[idx].get("module_type") == "generator"
    ]


def _farm_support_grid(cell_idx: int, module_type: str):
    effect_icon = {
        "speed": "⚡",
        "battery": "🔋",
        "stabilizer": "🛡",
    }.get(module_type, "✨")
    return _farm_focus_grid(cell_idx, effect_cells=_farm_neighbors(cell_idx), effect_icon=effect_icon, selected_icon="🟨")


def _farm_support_effect_lines(module_type: str, level: int):
    if module_type == "speed":
        bonus = int(FARM_SPEED_BONUS[level] * 100)
        cycle_seconds = int(FARM_BASE_CYCLE_SECONDS * (1.0 - FARM_SPEED_BONUS[level]))
        return [
            f"Эффект: <b>+{bonus}% к скорости</b> соседних генераторов.",
            f"Цикл генератора рядом: <code>{fmt_duration(FARM_BASE_CYCLE_SECONDS)} → {fmt_duration(cycle_seconds)}</code>",
        ]
    if module_type == "battery":
        hours = int(FARM_BATTERY_HOURS[level])
        total_capacity = FARM_BASE_CAP_SECONDS + hours * 3600
        return [
            f"Эффект: <b>+{hours}ч к хранилищу</b> соседних генераторов.",
            f"Хранилище генератора рядом: <code>{fmt_duration(FARM_BASE_CAP_SECONDS)} → {fmt_duration(total_capacity)}</code>",
        ]
    reduction = int(FARM_STABILIZER_REDUCTION[level] * 100)
    final_chance = max(0, 20 - reduction)
    return [
        f"Эффект: <b>снижает перегрев</b> соседних генераторов на {reduction}%.",
        f"Шанс перегрева рядом: <code>20% → {final_chance}% в час</code>",
    ]


def _farm_level_scale(level: int, max_level: int = 3) -> str:
    level = max(1, min(int(level or 1), max_level))
    return "".join("🟩" if idx < level else "⬜" for idx in range(max_level))


def _farm_append_section(lines: list[str], section):
    section = [item for item in section if item is not None and item != ""]
    if not section:
        return
    if lines and lines[-1] != "":
        lines.append("")
    lines.extend(section)


async def _farm_header_lines(owner_id: int, coins: int):
    user_row = await get_user(owner_id)
    display_name = (user_row[2] if user_row and len(user_row) > 2 else "") or "Игрок"
    user_link = get_user_link(owner_id, display_name)
    return [
        f"🏭 <b>Управление цехом</b> • {user_link}",
        f"💰 Монеты: <code>{fmt_num(coins)}</code>",
        "",
    ]


async def is_admin_or_owner(user_id: int, chat: types.Chat, sender_chat: types.Chat = None) -> bool:
    if user_id == OWNER_ID:
        return True
    if user_id in [ANON_BOT_ID, 777000]:
        return True
    if sender_chat and sender_chat.id == chat.id:
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
            [InlineKeyboardButton(text="🏭 Управление цехом", callback_data=f"farm_open:{owner_id}")],
            [InlineKeyboardButton(text="👤 В профиль", callback_data="nav_profile")],
        ]
    )


def build_games_text(owner_id: int, player_name: str, level: int, xp: int) -> str:
    user_link = get_user_link(owner_id, player_name)
    return (
        f"🕹️ <b>Игровая зона</b> • {user_link}\n"
        "\n"
        f"📊 Уровень: <b>{level}</b>\n"
        f"💳 Баланс: <code>{fmt_num(xp)} XP</code>\n\n"
        "Выберите автомат:"
    )


def build_game_panel_text(owner_id: int, player_name: str, title: str, xp: int, desc: str, hint: str) -> str:
    user_link = get_user_link(owner_id, player_name)
    return (
        f"🕹️ <b>Игровая зона</b> • {user_link}\n"
        f"{title}\n"
        f"💳 Баланс: <code>{fmt_num(xp)} XP</code>\n\n"
        f"{desc}\n"
        f"{hint}\n\n"
        "Выберите ставку:"
    )


async def ensure_games_menu_hint(message: types.Message):
    await ensure_sticky_message(
        message,
        scope="games_menu_hint",
        text="🎮 <b>Открыть меню игр</b> — <code>/games</code>",
        min_age_seconds=10 * 60 * 60,
        min_normal_messages=50,
        parse_mode="HTML",
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


FARM_MODULE_NAMES = {
    "generator": "Генератор",
    "speed": "Разгонный блок",
    "battery": "Аккумулятор",
    "stabilizer": "Стабилизатор",
}

FARM_MODULE_LEVEL_REQS = {
    "generator": 1,
    "speed": 2,
    "battery": 3,
    "stabilizer": 4,
}


def _farm_get_cell(state: dict, cell_idx: int):
    for cell in state["cells"]:
        if int(cell["cell_idx"]) == int(cell_idx):
            return cell
    return None


def _farm_can_access_module(module_type: str, user_level: int) -> bool:
    return int(user_level or 1) >= FARM_MODULE_LEVEL_REQS.get(module_type, 1)


def _farm_field_button_label(cell: dict):
    if cell["state"] == "locked":
        return "🔒"
    if cell["state"] == "empty":
        return "➕"
    m_type = cell.get("module_type")
    lvl = int(cell.get("level") or 1)
    if m_type == "generator":
        spec = cell.get("spec") or ""
        if lvl >= 3 and spec == "xp":
            base = "🧪"
        elif lvl >= 3 and spec == "coin":
            base = "🏭"
        else:
            base = "⚙️"
        if int(cell.get("pending_coins") or 0) > 0 or int(cell.get("pending_xp") or 0) > 0:
            base = "✅" + base
    elif m_type == "speed":
        base = "⚡"
    elif m_type == "battery":
        base = "🔋"
    else:
        base = "🛡"
    if cell.get("status") == "overheat":
        base = "🔥" + base
    return f"{base}{lvl}"


async def _farm_render_main(owner_id: int):
    state = await farm_get_state(owner_id)
    lines = await _farm_header_lines(owner_id, state["coins"])
    lines.append("Выберите ячейку:")
    if state.get("moving_from") is not None:
        lines.append(f"📦 Режим перемещения: выберите пустую ячейку для модуля #{int(state['moving_from']) + 1}")
        lines.append("")

    kb_rows = []
    for row in range(3):
        row_btns = []
        for col in range(3):
            idx = row * 3 + col
            cell = _farm_get_cell(state, idx)
            row_btns.append(InlineKeyboardButton(text=_farm_field_button_label(cell), callback_data=f"farm_cell:{owner_id}:{idx}"))
        kb_rows.append(row_btns)

    if state.get("moving_from") is not None:
        kb_rows.append([InlineKeyboardButton(text="❌ Отменить перемещение", callback_data=f"farm_move_cancel:{owner_id}")])
    kb_rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"farm_open:{owner_id}")])
    if await farm_is_complete(owner_id):
        kb_rows.append([InlineKeyboardButton(text="📦 Заводские заказы", callback_data=f"farm_orders:{owner_id}")])
    kb_rows.append([InlineKeyboardButton(text="🎮 Назад в игры", callback_data=f"back_to_games:{owner_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows), state


@router.callback_query(F.data.startswith("farm_orders:"))
async def farm_orders_info(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        return await callback.answer("Это не ваш завод.", show_alert=True)
    await callback.answer()
    await callback.message.answer(
        "📦 <b>Заводские заказы</b>\n\n"
        "Запускаются в групповом чате:\n"
        "<code>/factory_order discussion small тема</code>\n"
        "<code>/factory_order photo medium тема</code>\n"
        "<code>/factory_order tournament large</code>\n\n"
        "Small: 50 000 монет → банк 500 XP\n"
        "Medium: 200 000 → 2 000 XP\n"
        "Large: 500 000 → 5 000 XP")


async def _farm_render_locked_cell(owner_id: int, state: dict, cell_idx: int):
    next_cost = state.get("next_unlock_cost")
    lines = await _farm_header_lines(owner_id, state["coins"])
    lines.extend([
        _farm_focus_grid(cell_idx),
        "",
        "🔒 Ячейка закрыта.",
    ])
    kb_rows = []
    if next_cost is None:
        lines.append("Все ячейки уже открыты.")
    else:
        lines.append(f"Открытие: <code>{fmt_num(next_cost)} 💰</code>")
        kb_rows.append([InlineKeyboardButton(text=f"🔓 Открыть за {fmt_num(next_cost)} 💰", callback_data=f"farm_unlock:{owner_id}:{cell_idx}")])
    kb_rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"farm_open:{owner_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _farm_render_empty_cell(owner_id: int, state: dict, cell_idx: int):
    lines = await _farm_header_lines(owner_id, state["coins"])
    lines.extend([
        _farm_focus_grid(cell_idx),
        "",
        "▫️ Пустая ячейка.",
    ])
    kb_rows = []
    if state.get("moving_from") is not None:
        kb_rows.append([InlineKeyboardButton(text="📥 Переместить сюда", callback_data=f"farm_move_here:{owner_id}:{cell_idx}")])
    kb_rows.append([InlineKeyboardButton(text="🛒 Открыть магазин", callback_data=f"farm_shop:{owner_id}:{cell_idx}")])
    kb_rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"farm_open:{owner_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _farm_render_module_cell(owner_id: int, state: dict, cell_idx: int, cell: dict):
    m_type = cell.get("module_type")
    lvl = int(cell.get("level") or 1)
    pending_coins = int(cell.get("pending_coins") or 0)
    pending_xp = int(cell.get("pending_xp") or 0)
    lines = await _farm_header_lines(owner_id, state["coins"])

    if m_type == "generator":
        production = _farm_format_resources(*_farm_generator_output(lvl, cell.get("spec") or ""))
        storage = _farm_storage_max(lvl, cell.get("spec") or "", int(cell.get("cycle_seconds") or 0), int(cell.get("capacity_seconds") or 0))
        lines.extend([
            _farm_focus_grid(cell_idx, selected_icon=_farm_module_icon(m_type, lvl, cell.get("spec") or "")),
            "",
            f"{_farm_module_icon(m_type, lvl, cell.get('spec') or '')} <b>{FARM_MODULE_NAMES.get(m_type, m_type)}</b>",
            f"Уровень: { _farm_level_scale(lvl) }",
        ])
        _farm_append_section(lines, [
            f"Производит: <b>{production}</b> за <code>{fmt_duration(cell.get('cycle_seconds') or 0)}</code>",
        ])
        _farm_append_section(lines, ["Хранилище:"] + _farm_storage_lines(pending_coins, pending_xp, storage["coins"], storage["xp"]))

        speed_bonus = int((cell.get("support") or {}).get("speed_bonus", 0) * 100)
        battery_hours = int((cell.get("support") or {}).get("battery_hours", 0))
        overheat_chance = int(round((cell.get("support") or {}).get("overheat_chance_per_hour", 0.20) * 100))
        bonus_bits = []
        if speed_bonus > 0:
            bonus_bits.append(f"⚡ скорость +{speed_bonus}%")
        if battery_hours > 0:
            bonus_bits.append(f"🔋 хранилище +{battery_hours}ч")
        if overheat_chance < 20:
            bonus_bits.append(f"🛡 перегрев {overheat_chance}%/ч")
        if bonus_bits:
            _farm_append_section(lines, [f"Соседние бонусы: {' • '.join(bonus_bits)}"])
        _farm_append_section(lines, [_farm_generator_status(cell, state)])
    else:
        affected_generators = _farm_support_live_generators(state, cell_idx)
        lines.extend([
            _farm_support_grid(cell_idx, m_type),
            "",
            f"{_farm_module_icon(m_type, lvl)} <b>{FARM_MODULE_NAMES.get(m_type, m_type)}</b>",
            f"Уровень: { _farm_level_scale(lvl) }",
        ])
        _farm_append_section(lines, _farm_support_effect_lines(m_type, lvl))
        _farm_append_section(lines, [f"🟨 — модуль, {_farm_module_icon(m_type, lvl)} — клетки под эффектом"])
        if affected_generators:
            cells_text = ", ".join(f"#{idx + 1}" for idx in affected_generators)
            _farm_append_section(lines, [f"Сейчас усиливает генераторы в клетках: <code>{cells_text}</code>"])
        else:
            _farm_append_section(lines, ["Сейчас рядом нет генераторов, на которые действует модуль."])

    kb_rows = []
    if m_type == "generator" and (pending_coins > 0 or pending_xp > 0):
        kb_rows.append([InlineKeyboardButton(text="📥 Собрать", callback_data=f"farm_collect:{owner_id}:{cell_idx}")])
    if lvl < 3:
        kb_rows.append([InlineKeyboardButton(text="⬆️ Улучшение", callback_data=f"farm_upgrade_menu:{owner_id}:{cell_idx}")])
    kb_rows.append(
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"farm_sell:{owner_id}:{cell_idx}"),
            InlineKeyboardButton(text="↕️ Переместить", callback_data=f"farm_move_from:{owner_id}:{cell_idx}"),
        ]
    )
    if m_type == "generator" and cell.get("status") == "overheat":
        kb_rows.append([InlineKeyboardButton(text="🧰 Обслужить", callback_data=f"farm_repair:{owner_id}:{cell_idx}")])
    kb_rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"farm_open:{owner_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _farm_render_cell_menu(owner_id: int, cell_idx: int):
    state = await farm_get_state(owner_id)
    cell = _farm_get_cell(state, cell_idx)
    if cell["state"] == "locked":
        return await _farm_render_locked_cell(owner_id, state, cell_idx)
    if cell["state"] == "empty":
        return await _farm_render_empty_cell(owner_id, state, cell_idx)
    return await _farm_render_module_cell(owner_id, state, cell_idx, cell)


async def _farm_render_shop_menu(owner_id: int, cell_idx: int):
    state = await farm_get_state(owner_id)
    user_row = await get_user(owner_id)
    user_level = int(user_row[4] or 1) if user_row else 1
    lines = await _farm_header_lines(owner_id, state["coins"])
    lines.extend([
        _farm_focus_grid(cell_idx),
        "",
        "🛒 <b>Магазин цеха</b>",
    ])
    module_descriptions = {
        "generator": "⚙️ Генератор — производит монеты и XP.",
        "speed": "⚡ Разгонный блок — ускоряет соседние генераторы.",
        "battery": "🔋 Аккумулятор — расширяет хранилище соседних генераторов.",
        "stabilizer": "🛡 Стабилизатор — снижает перегрев соседних генераторов.",
    }
    for module_type in ("generator", "speed", "battery", "stabilizer"):
        if _farm_can_access_module(module_type, user_level):
            lines.append(module_descriptions[module_type])
        else:
            lines.append(f"{FARM_MODULE_NAMES[module_type]} — 🔒 Ур.{FARM_MODULE_LEVEL_REQS[module_type]}")
    lines.append("")
    lines.append("Выберите модуль:")
    kb_rows = [
        [InlineKeyboardButton(text="⚙️ Генератор", callback_data=f"farm_shop_section:{owner_id}:{cell_idx}:generator")],
        [
            InlineKeyboardButton(
                text="⚡ Разгонный блок" if _farm_can_access_module("speed", user_level) else f"🔒 Ур.{FARM_MODULE_LEVEL_REQS['speed']}",
                callback_data=f"farm_shop_section:{owner_id}:{cell_idx}:speed" if _farm_can_access_module("speed", user_level) else f"farm_shop_locked:{FARM_MODULE_LEVEL_REQS['speed']}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔋 Аккумулятор" if _farm_can_access_module("battery", user_level) else f"🔒 Ур.{FARM_MODULE_LEVEL_REQS['battery']}",
                callback_data=f"farm_shop_section:{owner_id}:{cell_idx}:battery" if _farm_can_access_module("battery", user_level) else f"farm_shop_locked:{FARM_MODULE_LEVEL_REQS['battery']}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🛡 Стабилизатор" if _farm_can_access_module("stabilizer", user_level) else f"🔒 Ур.{FARM_MODULE_LEVEL_REQS['stabilizer']}",
                callback_data=f"farm_shop_section:{owner_id}:{cell_idx}:stabilizer" if _farm_can_access_module("stabilizer", user_level) else f"farm_shop_locked:{FARM_MODULE_LEVEL_REQS['stabilizer']}",
            )
        ],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"farm_cell:{owner_id}:{cell_idx}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _farm_render_shop_section(owner_id: int, cell_idx: int, module_type: str):
    state = await farm_get_state(owner_id)
    user_row = await get_user(owner_id)
    user_level = int(user_row[4] or 1) if user_row else 1
    lines = await _farm_header_lines(owner_id, state["coins"])
    if not _farm_can_access_module(module_type, user_level):
        lines.extend([
            _farm_focus_grid(cell_idx),
            "",
            f"🔒 <b>{FARM_MODULE_NAMES[module_type]}</b>",
            f"Доступно с <b>{FARM_MODULE_LEVEL_REQS[module_type]}</b> уровня.",
        ])
        kb_rows = [[InlineKeyboardButton(text="↩️ Назад", callback_data=f"farm_shop:{owner_id}:{cell_idx}")]]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)
    shop_descriptions = {
        "generator": "Автоматически производит ресурсы.",
        "speed": "Ускоряет соседние генераторы.",
        "battery": "Увеличивает хранилище соседних генераторов.",
        "stabilizer": "Снижает шанс перегрева соседних генераторов.",
    }
    if module_type == "generator":
        lines.extend([
            _farm_focus_grid(cell_idx, selected_icon="⚙️"),
            "",
            "⚙️ <b>Генератор</b>",
            shop_descriptions["generator"],
            f"Цена: <code>{FARM_GEN_BUY_COST} 💰</code>",
        ])
        for title, stats in [
            ("1️⃣ Уровень", f"<code>{_farm_format_resources(50, 2)}</code> за <code>{fmt_duration(FARM_BASE_CYCLE_SECONDS)}</code>"),
            ("2️⃣ Уровень", f"<code>{_farm_format_resources(150, 5)}</code> за <code>{fmt_duration(FARM_BASE_CYCLE_SECONDS)}</code>"),
            ("3️⃣ Уровень", f"🧪 <code>{_farm_format_resources(0, 15)}</code> или 🏭 <code>{_farm_format_resources(600, 0)}</code> за <code>{fmt_duration(FARM_BASE_CYCLE_SECONDS)}</code>"),
        ]:
            _farm_append_section(lines, [title, stats])
        _farm_append_section(lines, [f"Хранилище без бонусов: <code>{fmt_duration(FARM_BASE_CAP_SECONDS)}</code>"])
        btn = InlineKeyboardButton(text=f"Купить за {FARM_GEN_BUY_COST} 💰", callback_data=f"farm_buy:{owner_id}:{cell_idx}:generator")
    else:
        lines.extend([
            _farm_support_grid(cell_idx, module_type),
            "",
            f"{_farm_module_icon(module_type)} <b>{FARM_MODULE_NAMES[module_type]}</b>",
            shop_descriptions[module_type],
            f"Цена: <code>{FARM_SUPPORT_COSTS[module_type][1]} 💰</code>",
        ])
        for lvl in (1, 2, 3):
            level_block = [f"{lvl}️⃣ Уровень"] + _farm_support_effect_lines(module_type, lvl)
            if lvl >= 2:
                level_block.append(f"Улучшение до этого уровня: <code>{FARM_SUPPORT_COSTS[module_type][lvl]} 💰</code>")
            _farm_append_section(lines, level_block)
        _farm_append_section(lines, [f"🟨 — модуль, {_farm_module_icon(module_type)} — клетки под эффектом"])
        btn = InlineKeyboardButton(
            text=f"Купить за {FARM_SUPPORT_COSTS[module_type][1]} 💰",
            callback_data=f"farm_buy:{owner_id}:{cell_idx}:{module_type}",
        )

    kb_rows = [
        [btn],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"farm_shop:{owner_id}:{cell_idx}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _farm_render_upgrade_menu(owner_id: int, cell_idx: int):
    state = await farm_get_state(owner_id)
    cell = _farm_get_cell(state, cell_idx)
    if not cell or cell["state"] != "module":
        return await _farm_render_cell_menu(owner_id, cell_idx)
    m_type = cell.get("module_type")
    lvl = int(cell.get("level") or 1)
    lines = await _farm_header_lines(owner_id, state["coins"])
    grid = _farm_focus_grid(cell_idx)
    if m_type != "generator":
        grid = _farm_support_grid(cell_idx, m_type)
    lines.extend([
        grid,
        "",
        f"⬆️ Улучшение: <b>{FARM_MODULE_NAMES.get(m_type, m_type)}</b>",
        f"Уровень: { _farm_level_scale(lvl) }",
    ])
    kb_rows = []
    if m_type == "generator":
        if lvl == 1:
            _farm_append_section(lines, [
                f"После улучшения: <code>{_farm_format_resources(150, 5)}</code> за <code>{fmt_duration(FARM_BASE_CYCLE_SECONDS)}</code>",
                f"Цена: <code>{FARM_GEN_UPGRADE_COSTS[2]} 💰</code>",
            ])
            kb_rows.append([InlineKeyboardButton(text=f"Улучшить до ур.2 за {FARM_GEN_UPGRADE_COSTS[2]} 💰", callback_data=f"farm_upgrade:{owner_id}:{cell_idx}")])
        elif lvl == 2:
            _farm_append_section(lines, [
                f"Выберите ветку 3️⃣ уровня.",
                f"Цена: <code>{FARM_GEN_UPGRADE_COSTS[3]} 💰</code>",
                f"🧪 Синтезатор Опыта: <code>{_farm_format_resources(0, 15)}</code> за <code>{fmt_duration(FARM_BASE_CYCLE_SECONDS)}</code>",
                f"🏭 Монетный Двор: <code>{_farm_format_resources(600, 0)}</code> за <code>{fmt_duration(FARM_BASE_CYCLE_SECONDS)}</code>",
            ])
            kb_rows.append(
                [
                    InlineKeyboardButton(text=f"🧪 В XP ({FARM_GEN_UPGRADE_COSTS[3]} 💰)", callback_data=f"farm_upgrade_spec:{owner_id}:{cell_idx}:xp"),
                    InlineKeyboardButton(text=f"🏭 В 💰 ({FARM_GEN_UPGRADE_COSTS[3]} 💰)", callback_data=f"farm_upgrade_spec:{owner_id}:{cell_idx}:coin"),
                ]
            )
        else:
            _farm_append_section(lines, ["Максимальный уровень."])
    else:
        if lvl >= 3:
            _farm_append_section(lines, ["Максимальный уровень."])
        else:
            next_lvl = lvl + 1
            cost = FARM_SUPPORT_COSTS[m_type][next_lvl]
            _farm_append_section(lines, _farm_support_effect_lines(m_type, next_lvl))
            _farm_append_section(lines, [
                f"🟨 — модуль, {_farm_module_icon(m_type, next_lvl)} — клетки под эффектом",
                f"Цена улучшения: <code>{cost} 💰</code>",
            ])
            kb_rows.append([InlineKeyboardButton(text=f"Улучшить до ур.{next_lvl} за {cost} 💰", callback_data=f"farm_upgrade:{owner_id}:{cell_idx}")])

    kb_rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"farm_cell:{owner_id}:{cell_idx}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _farm_edit_message(message: types.Message, text: str, kb: InlineKeyboardMarkup):
    await touch_temp_message(message)
    try:
        if message.photo:
            await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await touch_temp_message(message)


async def _farm_notify(callback: types.CallbackQuery, text: str):
    user_link = get_user_link(callback.from_user.id, callback.from_user.full_name or "Игрок")
    await answer_temp(
        callback.message,
        text=f"{user_link}, {text}",
        parse_mode="HTML",
        reply=True,
        key=f"farm_notice:{callback.from_user.id}",
    )


async def _farm_check_owner(callback: types.CallbackQuery, owner_id: int):
    if callback.from_user.id != owner_id and not (
        owner_id == ANON_BOT_ID and await is_admin_or_owner(callback.from_user.id, callback.message.chat)
    ):
        await callback.answer("Это не ваш цех.", show_alert=True)
        return False
    return True


@router.message(Command("factory"))
async def cmd_farm(message: types.Message):
    await delete_later(message, 0)
    await ensure_games_menu_hint(message)
    text, kb, _ = await _farm_render_main(message.from_user.id)
    await answer_temp(message, text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("farm_open:"))
async def farm_open(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if not await _farm_check_owner(callback, owner_id):
        return
    text, kb, _ = await _farm_render_main(owner_id)
    await _farm_edit_message(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("farm_cell:"))
async def farm_cell(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    text, kb = await _farm_render_cell_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("farm_shop:"))
async def farm_shop(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    text, kb = await _farm_render_shop_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("farm_shop_section:"))
async def farm_shop_section(callback: types.CallbackQuery):
    _, owner_id, cell_idx, module_type = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    text, kb = await _farm_render_shop_section(owner_id, cell_idx, module_type)
    await _farm_edit_message(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("farm_shop_locked:"))
async def farm_shop_locked(callback: types.CallbackQuery):
    need_level = int(callback.data.split(":")[1])
    await callback.answer(f"🔒 Модуль откроется с {need_level} уровня.", show_alert=True)


@router.callback_query(F.data.startswith("farm_upgrade_menu:"))
async def farm_upgrade_menu(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    text, kb = await _farm_render_upgrade_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("farm_unlock:"))
async def farm_unlock(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_unlock_cell(owner_id, cell_idx)
    text, kb = await _farm_render_cell_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_buy:"))
async def farm_buy(callback: types.CallbackQuery):
    _, owner_id, cell_idx, module_type = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    user_row = await get_user(owner_id)
    user_level = int(user_row[4] or 1) if user_row else 1
    if not _farm_can_access_module(module_type, user_level):
        need_level = FARM_MODULE_LEVEL_REQS.get(module_type, 1)
        await callback.answer(f"🔒 Модуль откроется с {need_level} уровня.", show_alert=True)
        return
    res = await farm_buy_module(owner_id, cell_idx, module_type)
    text, kb = await _farm_render_cell_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_collect:"))
async def farm_collect(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_collect_generator(owner_id, cell_idx)
    text, kb = await _farm_render_cell_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_upgrade_spec:"))
async def farm_upgrade_spec(callback: types.CallbackQuery):
    _, owner_id, cell_idx, spec = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_upgrade_module(owner_id, cell_idx, spec=spec)
    text, kb = await _farm_render_upgrade_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_upgrade:"))
async def farm_upgrade(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_upgrade_module(owner_id, cell_idx)
    text, kb = await _farm_render_upgrade_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_repair:"))
async def farm_repair(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_repair_module(owner_id, cell_idx)
    text, kb = await _farm_render_cell_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_sell:"))
async def farm_sell(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_sell_module(owner_id, cell_idx)
    text, kb = await _farm_render_cell_menu(owner_id, cell_idx)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_move_from:"))
async def farm_move_from(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_set_move_source(owner_id, cell_idx)
    text, kb, _ = await _farm_render_main(owner_id)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_move_here:"))
async def farm_move_here(callback: types.CallbackQuery):
    _, owner_id, cell_idx = callback.data.split(":")
    owner_id = int(owner_id)
    cell_idx = int(cell_idx)
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_move_module(owner_id, cell_idx)
    text, kb, _ = await _farm_render_main(owner_id)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer("Готово" if res["ok"] else "Ошибка")


@router.callback_query(F.data.startswith("farm_move_cancel:"))
async def farm_move_cancel(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if not await _farm_check_owner(callback, owner_id):
        return
    res = await farm_set_move_source(owner_id, None)
    text, kb, _ = await _farm_render_main(owner_id)
    await _farm_edit_message(callback.message, text, kb)
    await _farm_notify(callback, res["message"])
    await callback.answer()


@router.message(Command("games"))
async def cmd_games(message: types.Message):
    await delete_later(message, 0)
    await ensure_games_menu_hint(message)

    user_data = await get_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    if not user_data:
        return

    xp, level = user_data[3], user_data[4]
    uid = message.from_user.id
    is_adm = await is_admin_or_owner(uid, message.chat, message.sender_chat)

    text = build_games_text(uid, message.from_user.full_name, level, xp)
    kb = build_games_keyboard(level, is_adm, uid)
    await answer_temp(message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("locked_game"))
async def locked_game_alert(callback: types.CallbackQuery):
    await touch_temp_message(callback.message)
    req = callback.data.split(":")[1]
    await callback.answer(f"🔒 Эта игра доступна с {req} уровня.", show_alert=True)


@router.callback_query(F.data.startswith("game_menu_"))
async def game_bet_menu(callback: types.CallbackQuery):
    await touch_temp_message(callback.message)
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

    conf = GAME_UI.get(
        game_name,
        {"emoji": "🎮", "name": "Игра", "desc": "Сделайте ставку.", "hint": "Результат зависит от удачи."},
    )

    user_data = await get_user(owner_id)
    current_xp = user_data[3] if user_data else 0
    level = user_data[4] if user_data else 1
    bet_buttons = [InlineKeyboardButton(text=f"{bet} XP", callback_data=f"play_{game_name}:{bet}:{owner_id}")
                   for bet in GAME_BETS_BY_LEVEL.get(level, GAME_BETS_BY_LEVEL[1])]
    kb = InlineKeyboardMarkup(inline_keyboard=[bet_buttons, [InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_games:{owner_id}")]])
    display_name = "Group Anonymous Bot" if owner_id == ANON_BOT_ID else ((user_data[2] if user_data else "") or "Игрок")
    text = build_game_panel_text(
        owner_id,
        display_name,
        f"{conf['emoji']} <b>{conf['name']}</b>",
        current_xp,
        conf["desc"],
        conf["hint"],
    )

    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("play_"))
async def play_game_logic(callback: types.CallbackQuery):
    await touch_temp_message(callback.message)
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
    if bet not in GAME_BETS_BY_LEVEL.get(user_data[4], ()):
        return await callback.answer("Эта ставка недоступна на вашем уровне.", show_alert=True)
    if not can_afford(user_data[3], user_data[4], bet):
        return await callback.answer(f"Не хватает XP. У вас {fmt_num(user_data[3])} XP.", show_alert=True)

    try:
        await callback.message.delete()
    except Exception:
        pass

    old_lvl_start, new_lvl_start, _ = await update_xp(player_id, -bet)

    emoji_map = {"dice": "🎲", "basket": "🏀", "slots": "🎰"}
    dice_emoji = emoji_map.get(game, "🎲")
    try:
        dice_msg = await callback.message.answer_dice(emoji=dice_emoji)
    except TelegramBadRequest:
        await update_xp(player_id, bet)
        return await callback.answer("Не удалось запустить игру. Попробуйте еще раз.", show_alert=True)
    val = dice_msg.dice.value
    animation_seconds = 4 if game != "slots" else 2
    await asyncio.sleep(animation_seconds)

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
            res_text = "🏀 <b>По дужке.</b> Мяч ударился о кольцо и вылетел."
        elif val == 4:
            win_mult = 2
            res_text = "🏀 <b>Попадание!</b> Мяч зашел в кольцо."
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
            [InlineKeyboardButton(text="🔄 Сыграть еще", callback_data=f"game_menu_{game}:{owner_id}")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data=f"back_to_games:{owner_id}")],
        ]
    )
    await answer_temp(callback.message, res_text, reply_markup=kb, user_id=callback.from_user.id)
    await delete_later(dice_msg, animation_seconds)


@router.callback_query(F.data.startswith("game_info_duel"))
async def duel_info_menu(callback: types.CallbackQuery):
    await touch_temp_message(callback.message)
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
    user_data = await get_user(owner_id)
    display_name = "Group Anonymous Bot" if owner_id == ANON_BOT_ID else ((user_data[2] if user_data else "") or "Игрок")
    user_link = get_user_link(owner_id, display_name)
    text = (
        f"🕹️ <b>Игровая зона</b> • {user_link}\n"
        "🔫 <b>Дуэль</b>\n\n"
        "Игра между двумя участниками на XP.\n\n"
        "<b>Как работает раунд:</b>\n"
        "• ⚔️ Атака побеждает 🛡 Оборону\n"
        "• 🛡 Оборона побеждает ⚡ Хитрость\n"
        "• ⚡ Хитрость побеждает ⚔️ Атаку\n"
        "• Сильная тактика дает 70% на победу, слабая сохраняет 30%\n"
        "• Одинаковый выбор = ничья и возврат ставок\n\n"
        "Ставка — не более 5% XP более бедного игрока и не более 10 000 XP.\n"
        "Комиссия одной пары за день: 10%, 25%, 50%, затем 75%. С новым соперником снова 10%.\n\n"
        "<b>Как начать:</b>\n"
        "1. Вызов: <code>/duel @username [ставка]</code>\n"
        "2. Соперник принимает дуэль\n"
        "3. Оба выбирают тактику\n"
        "4. При принятии ставки уходят в банк; победитель получает банк за вычетом комиссии"
    )
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("back_to_games"))
async def back_to_games(callback: types.CallbackQuery):
    await touch_temp_message(callback.message)
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
    display_name = "Group Anonymous Bot" if owner_id == ANON_BOT_ID else ((user_data[2] if user_data else "") or "Игрок")
    is_adm = await is_admin_or_owner(callback.from_user.id, callback.message.chat)

    text = build_games_text(owner_id, display_name, level, xp)
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

    target_data = await get_user(target_id)
    if not target_data:
        return await answer_temp(message, "❌ У соперника еще нет профиля.", reply=True)
    max_bet = min(10000, int(min(total_available_xp(init_data[3], init_data[4]),
                                 total_available_xp(target_data[3], target_data[4])) * 0.05))
    if max_bet < 10:
        return await answer_temp(message, "❌ У одного из игроков пока недостаточно XP для дуэли.", reply=True)
    if bet > max_bet:
        return await answer_temp(message, f"⚠️ Максимальная ставка для этой пары: <code>{fmt_num(max_bet)} XP</code>.", reply=True)
    if await load_active_duel(message.chat.id):
        return await answer_temp(message, "⚠️ В этом чате уже идет дуэль.", reply=True)

    duel = {
        "chat_id": message.chat.id,
        "initiator": initiator.id,
        "target": target_id,
        "bet": bet,
        "initiator_name": html.escape(initiator.full_name or "Игрок"),
        "target_name": html.escape(str(target_name or "Игрок")),
        "state": "waiting_accept",
        "p1_choice": None,
        "p2_choice": None,
        "escrowed": False,
        "created_at": int(time.time()),
    }

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⚔️ Принять вызов", callback_data="duel_accept")]]
    )
    text = (
        "🥊 <b>Вызов на дуэль</b>\n"
        f"🔴 <b>{duel['initiator_name']}</b> VS 🔵 <b>{duel['target_name']}</b>\n"
        f"💰 Банк: <code>{fmt_num(bet * 2)} XP</code>\n\n"
        f"Ждем подтверждения от {duel['target_name']}."
    )
    sent = await answer_temp(message, text, reply_markup=kb)
    if sent:
        duel["message_id"] = sent.message_id
    active_duels[message.chat.id] = duel
    await save_active_duel(duel)


@router.callback_query(F.data == "duel_accept")
@_serialized_duel
async def duel_accept(callback: types.CallbackQuery):
    await touch_temp_message(callback.message)
    chat_id = callback.message.chat.id
    duel = active_duels.get(chat_id) or await load_active_duel(chat_id)
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

    init_data = await get_user(duel["initiator"])
    if not init_data or not can_afford(init_data[3], init_data[4], duel["bet"]):
        return await callback.answer("У инициатора больше нет XP на эту ставку.", show_alert=True)
    max_bet = min(10000, int(min(total_available_xp(init_data[3], init_data[4]),
                                 total_available_xp(target_data[3], target_data[4])) * 0.05))
    if duel["bet"] > max_bet:
        return await callback.answer(f"Лимит пары снизился до {max_bet} XP. Создайте новый вызов.", show_alert=True)

    if not await escrow_active_duel(chat_id):
        return await callback.answer("Дуэль уже принята либо баланс изменился.", show_alert=True)
    duel["state"] = "fighting"
    duel["escrowed"] = True
    active_duels[chat_id] = duel
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ Атака", callback_data="tactics_atk")],
            [InlineKeyboardButton(text="🛡 Оборона", callback_data="tactics_def")],
            [InlineKeyboardButton(text="⚡ Хитрость", callback_data="tactics_trick")],
        ]
    )
    text = (
        "🔥 <b>Дуэль началась</b>\n"
        f"🔴 {duel['initiator_name']} VS 🔵 {duel['target_name']}\n\n"
        "Выберите тактику. Ваш выбор скрыт до конца раунда."
    )
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tactics_"))
@_serialized_duel
async def duel_tactics(callback: types.CallbackQuery):
    await touch_temp_message(callback.message)
    chat_id = callback.message.chat.id
    duel = active_duels.get(chat_id) or await load_active_duel(chat_id)
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
    choice_name = {"atk": "Атака ⚔️", "def": "Оборона 🛡", "trick": "Хитрость ⚡"}.get(choice, "Неизвестно")

    if role == "p1":
        if duel["p1_choice"]:
            return await callback.answer("Вы уже выбрали.", show_alert=True)
        duel["p1_choice"] = choice
    else:
        if duel["p2_choice"]:
            return await callback.answer("Вы уже выбрали.", show_alert=True)
        duel["p2_choice"] = choice
    active_duels[chat_id] = duel
    await save_active_duel(duel)
    await callback.answer(f"Выбрано: {choice_name}")

    if duel["p1_choice"] and duel["p2_choice"]:
        await resolve_duel(callback.message, duel)
    else:
        p1_status = "✅ Готов" if duel["p1_choice"] else "⏳ Думает..."
        p2_status = "✅ Готов" if duel["p2_choice"] else "⏳ Думает..."
        text = (
            "🔥 <b>Дуэль идет</b>\n"
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
    tactical_winner = None
    if p1 == p2:
        winner = None
    elif p1 == "atk":
        tactical_winner = 1 if p2 == "trick" else 2
    elif p1 == "def":
        tactical_winner = 1 if p2 == "atk" else 2
    elif p1 == "trick":
        tactical_winner = 1 if p2 == "def" else 2
    if tactical_winner:
        winner = tactical_winner if random.random() < 0.70 else (2 if tactical_winner == 1 else 1)

    t_map = {"atk": "Атака ⚔️", "def": "Оборона 🛡", "trick": "Хитрость ⚡"}
    res_text = (
        "🏁 <b>Результат дуэли</b>\n"
        f"🔴 {duel['initiator_name']}: <b>{t_map[p1]}</b>\n"
        f"🔵 {duel['target_name']}: <b>{t_map[p2]}</b>\n\n"
    )

    if winner is None:
        await settle_active_duel(duel)
        res_text += "🤝 <b>Ничья.</b> Ставки возвращены."
    else:
        w_id = duel["initiator"] if winner == 1 else duel["target"]
        l_id = duel["target"] if winner == 1 else duel["initiator"]
        w_name = duel["initiator_name"] if winner == 1 else duel["target_name"]
        l_name = duel["target_name"] if winner == 1 else duel["initiator_name"]

        completed_today = await duel_pair_count_today(duel["initiator"], duel["target"])
        commission = (10, 25, 50, 75)[min(completed_today, 3)]
        bank = duel["bet"] * 2
        payout = bank * (100 - commission) // 100
        await settle_active_duel(duel, w_id, commission)

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
            f"💰 <b>{w_name}</b> получает банк: <code>{fmt_num(payout)} XP</code>\n"
            f"💀 <b>{l_name}</b> теряет ставку: <code>{fmt_num(duel['bet'])} XP</code>\n"
            f"🏦 Комиссия пары: <code>{commission}%</code>\n"
        )

    active_duels.pop(message.chat.id, None)
    sent_msg = await message.edit_text(res_text, reply_markup=None)
    await delete_later(sent_msg, 60)


async def _duel_processor(bot):
    while True:
        try:
            expired = await expire_stale_duels(900)
            for chat_id, escrowed in expired:
                active_duels.pop(chat_id, None)
                text = "⌛ Дуэль отменена по тайм-ауту."
                if escrowed:
                    text += " Обе ставки возвращены."
                await bot.send_message(chat_id, text)
        except Exception:
            pass
        await asyncio.sleep(30)


def start_duel_processor(bot):
    global _duel_processor_task
    if not _duel_processor_task or _duel_processor_task.done():
        _duel_processor_task = asyncio.create_task(_duel_processor(bot))
