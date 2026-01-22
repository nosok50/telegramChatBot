# -*- coding: utf-8 -*-
import math
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from database import add_to_list, get_list, manage_warn, remove_from_list, clear_list_data
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

# Модифицированная клавиатура для badwords с поддержкой страниц
def badwords_kb(page=0, total_pages=1):
    kb = []
    
    # Кнопки навигации (только если страниц > 1)
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"show_badwords:{page-1}"))
        else:
            nav_row.append(InlineKeyboardButton(text="⏺", callback_data="ignore"))
            
        nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="ignore"))
        
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"show_badwords:{page+1}"))
        else:
            nav_row.append(InlineKeyboardButton(text="⏺", callback_data="ignore"))
        
        kb.append(nav_row)
    else:
        # Если страница одна, оставляем кнопку "Показать" для обновления
        kb.append([InlineKeyboardButton(text="👁 Показать список", callback_data="show_badwords:0")])

    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="add_badword"),
               InlineKeyboardButton(text="➖ Удалить", callback_data="del_badword")])
    
    # Кнопка очистки всего списка
    kb.append([InlineKeyboardButton(text="🗑 Очистить весь список", callback_data="ask_clear_badwords")])
    
    kb.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="nav_main")])
    
    return InlineKeyboardMarkup(inline_keyboard=kb)

def confirm_clear_kb(section):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, очистить всё", callback_data=f"confirm_clear_{section}")],
        [InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"nav_{section}")]
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

@router.callback_query(F.data == "ignore")
async def ignore_click(clb: CallbackQuery):
    await clb.answer()

# --- ПРОСМОТР СПИСКОВ ---

@router.callback_query(F.data == "show_whitelist")
async def show_wl(clb: CallbackQuery):
    items = await get_list('whitelist')
    text = "📋 <b>Белый список:</b>\n\n" + ("\n".join([f"• <code>{i}</code>" for i in items]) if items else "<i>Список пуст</i>")
    
    # Если список слишком длинный, телеграм не отправит. Обрезаем.
    if len(text) > 4000: text = text[:4000] + "\n..."
    
    await clb.message.edit_text(text, reply_markup=whitelist_kb(), parse_mode="HTML")

# ОБНОВЛЕННАЯ ФУНКЦИЯ ПРОСМОТРА BADWORDS С ПАГИНАЦИЕЙ
@router.callback_query(F.data.startswith("show_badwords"))
async def show_bw(clb: CallbackQuery):
    # Парсим номер страницы из callback_data (format: show_badwords:page_num)
    try:
        page = int(clb.data.split(":")[1])
    except IndexError:
        page = 0

    items = await get_list('badwords')
    
    # Настройки пагинации
    ITEMS_PER_PAGE = 50
    total_items = len(items)
    total_pages = math.ceil(total_items / ITEMS_PER_PAGE)
    
    # Если список пуст
    if total_items == 0:
        text = "🤬 <b>Фильтр слов:</b>\n\n<i>Список пуст</i>"
        await clb.message.edit_text(text, reply_markup=badwords_kb(0, 1), parse_mode="HTML")
        return

    # Корректируем страницу если вышли за пределы
    if page >= total_pages: page = total_pages - 1
    if page < 0: page = 0

    # Срез данных
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    current_items = items[start:end]

    # Формируем текст
    text_items = ", ".join([f"<code>{i}</code>" for i in current_items])
    text = (f"🤬 <b>Фильтр слов</b> (Стр. {page+1}/{total_pages}):\n"
            f"Всего слов: {total_items}\n\n"
            f"{text_items}")
    
    await clb.message.edit_text(text, reply_markup=badwords_kb(page, total_pages), parse_mode="HTML")

# --- ОЧИСТКА СПИСКА ---

@router.callback_query(F.data == "ask_clear_badwords")
async def ask_clear_bw(clb: CallbackQuery):
    await clb.message.edit_text(
        "🗑 <b>Вы уверены, что хотите очистить весь список запрещенных слов?</b>\n"
        "Это действие нельзя отменить.",
        reply_markup=confirm_clear_kb("badwords"),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "confirm_clear_badwords")
async def confirm_clear_bw(clb: CallbackQuery):
    await clear_list_data('badwords')
    await clb.answer("Список полностью очищен!", show_alert=True)
    await nav_bw(clb)

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