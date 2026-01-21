# -*- coding: utf-8 -*-
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from database import add_to_list, get_list, manage_warn, remove_from_list
from config import OWNER_ID
from utils import answer_temp, delete_later

router = Router()

class AdminStates(StatesGroup):
    waiting_add_wl = State()
    waiting_del_wl = State()
    waiting_add_bw = State()
    waiting_del_bw = State()

# --- КЛАВИАТУРЫ ---

def main_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Белый список", callback_data="nav_whitelist"),
         InlineKeyboardButton(text="🤬 Фильтр слов", callback_data="nav_badwords")],
        [InlineKeyboardButton(text="❌ Закрыть панель", callback_data="close_admin")]
    ])

def whitelist_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Показать список", callback_data="show_whitelist")],
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_whitelist"),
         InlineKeyboardButton(text="➖ Удалить", callback_data="del_whitelist")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="nav_main")]
    ])

def badwords_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Показать список", callback_data="show_badwords")],
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_badword"),
         InlineKeyboardButton(text="➖ Удалить", callback_data="del_badword")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="nav_main")]
    ])

def cancel_kb(section):
    # section: wl или bw (для возврата в нужное меню)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"nav_{section}")]
    ])

# --- ГЛАВНОЕ МЕНЮ ---

@router.message(Command("admin"))
async def open_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    await delete_later(message, 0)
    
    # Используем message.answer, так как answer_temp не поддерживает reply_markup в вашей версии utils
    await message.answer(
        "⚙️ <b>Панель управления ботом</b>\n"
        "Выберите раздел настроек:", 
        reply_markup=main_admin_kb(),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "close_admin")
async def close_menu(clb: CallbackQuery, state: FSMContext):
    await state.clear()
    await clb.message.delete()

@router.callback_query(F.data == "nav_main")
async def nav_main(clb: CallbackQuery, state: FSMContext):
    await state.clear()
    await clb.message.edit_text(
        "⚙️ <b>Панель управления ботом</b>\n"
        "Выберите раздел настроек:", 
        reply_markup=main_admin_kb(),
        parse_mode="HTML"
    )

# --- НАВИГАЦИЯ ---

@router.callback_query(F.data == "nav_whitelist")
async def nav_wl(clb: CallbackQuery):
    await clb.message.edit_text(
        "📋 <b>Настройки Белого списка</b>\n"
        "Разрешенные ссылки и юзернеймы:",
        reply_markup=whitelist_kb(),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "nav_badwords")
async def nav_bw(clb: CallbackQuery):
    await clb.message.edit_text(
        "🤬 <b>Настройки Фильтра слов</b>\n"
        "Запрещенные слова и выражения:",
        reply_markup=badwords_kb(),
        parse_mode="HTML"
    )

# --- ПРОСМОТР СПИСКОВ ---

@router.callback_query(F.data == "show_whitelist")
async def show_wl(clb: CallbackQuery):
    items = await get_list('whitelist')
    text = "📋 <b>Белый список:</b>\n\n" + ("\n".join([f"• <code>{i}</code>" for i in items]) if items else "<i>Список пуст</i>")
    
    # Если список слишком длинный, телеграм не отправит. Обрезаем.
    if len(text) > 4000: text = text[:4000] + "\n..."
    
    await clb.message.edit_text(text, reply_markup=whitelist_kb(), parse_mode="HTML")

@router.callback_query(F.data == "show_badwords")
async def show_bw(clb: CallbackQuery):
    items = await get_list('badwords')
    text = "🤬 <b>Фильтр слов:</b>\n\n" + (", ".join([f"<code>{i}</code>" for i in items]) if items else "<i>Список пуст</i>")
    
    if len(text) > 4000: text = text[:4000] + "\n..."
    
    await clb.message.edit_text(text, reply_markup=badwords_kb(), parse_mode="HTML")

# --- ДОБАВЛЕНИЕ ---

@router.callback_query(F.data == "add_whitelist")
async def start_add_wl(clb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_add_wl)
    await clb.message.edit_text(
        "➕ <b>Добавление в Белый список</b>\n"
        "Введите ссылки или юзернеймы <b>через запятую</b>.\n"
        "<i>Пример: youtube.com, @admin, google.com</i>", 
        reply_markup=cancel_kb("whitelist"),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "add_badword")
async def start_add_bw(clb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_add_bw)
    await clb.message.edit_text(
        "➕ <b>Добавление запрещенных слов</b>\n"
        "Введите слова или фразы <b>через запятую</b>.\n"
        "<i>Пример: слово1, плохая фраза, слово3</i>", 
        reply_markup=cancel_kb("badwords"),
        parse_mode="HTML"
    )

@router.message(AdminStates.waiting_add_wl)
async def process_add_wl(message: types.Message, state: FSMContext):
    await delete_later(message, 0)
    items = [i.strip() for i in message.text.split(',') if i.strip()]
    
    count = 0
    for item in items:
        if await add_to_list('whitelist', item):
            count += 1
            
    await message.answer(
        f"✅ <b>Добавлено {count} записей</b> в Белый список.", 
        reply_markup=whitelist_kb(),
        parse_mode="HTML"
    )
    await state.clear()

@router.message(AdminStates.waiting_add_bw)
async def process_add_bw(message: types.Message, state: FSMContext):
    await delete_later(message, 0)
    items = [i.strip() for i in message.text.split(',') if i.strip()]
    
    count = 0
    for item in items:
        if await add_to_list('badwords', item):
            count += 1
            
    await message.answer(
        f"✅ <b>Добавлено {count} слов</b> в Фильтр.", 
        reply_markup=badwords_kb(),
        parse_mode="HTML"
    )
    await state.clear()

# --- УДАЛЕНИЕ ---

@router.callback_query(F.data == "del_whitelist")
async def start_del_wl(clb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_del_wl)
    await clb.message.edit_text(
        "➖ <b>Удаление из Белого списка</b>\n"
        "Введите точные значения <b>через запятую</b> для удаления.", 
        reply_markup=cancel_kb("whitelist"),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "del_badword")
async def start_del_bw(clb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_del_bw)
    await clb.message.edit_text(
        "➖ <b>Удаление запрещенных слов</b>\n"
        "Введите слова <b>через запятую</b> для удаления.", 
        reply_markup=cancel_kb("badwords"),
        parse_mode="HTML"
    )

@router.message(AdminStates.waiting_del_wl)
async def process_del_wl(message: types.Message, state: FSMContext):
    await delete_later(message, 0)
    items = [i.strip() for i in message.text.split(',') if i.strip()]
    
    for item in items:
        await remove_from_list('whitelist', item)
            
    await message.answer(
        f"✅ <b>Обработано удаление {len(items)} записей</b> из Белого списка.", 
        reply_markup=whitelist_kb(),
        parse_mode="HTML"
    )
    await state.clear()

@router.message(AdminStates.waiting_del_bw)
async def process_del_bw(message: types.Message, state: FSMContext):
    await delete_later(message, 0)
    items = [i.strip() for i in message.text.split(',') if i.strip()]
    
    for item in items:
        await remove_from_list('badwords', item)
            
    await message.answer(
        f"✅ <b>Обработано удаление {len(items)} слов</b> из Фильтра.", 
        reply_markup=badwords_kb(),
        parse_mode="HTML"
    )
    await state.clear()

# --- СБРОС ВАРНОВ ---

@router.message(Command("reset_warns"))
async def admin_reset(message: types.Message):
    await delete_later(message, 0)
    if message.from_user.id != OWNER_ID: return
    
    if not message.reply_to_message:
        return await answer_temp(message, "⚠️ Ответьте на сообщение пользователя.")
    
    target_name = message.reply_to_message.from_user.full_name
    await manage_warn(message.reply_to_message.from_user.id, "reset")
    await answer_temp(message, f"✅ Предупреждения для <b>{target_name}</b> полностью сброшены.")