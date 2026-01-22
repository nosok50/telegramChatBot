import aiosqlite
import logging
import time
import json
from datetime import datetime

DB_NAME = 'bot_database.db'

# Конфигурация уровней (XP Cap для каждого уровня)
LEVEL_CAPS = {
    1: 500,
    2: 2000,
    3: 8000,
    4: 25000,
    5: float('inf')
}

async def create_tables():
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Основная таблица пользователей
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            warns INTEGER DEFAULT 0,
            mod_level INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            last_wipe_date TEXT DEFAULT NULL
        )''')
        
        # 2. Таблица истории репутации
        await db.execute('''CREATE TABLE IF NOT EXISTS rep_history (
            from_id INTEGER,
            to_id INTEGER,
            date_str TEXT,
            PRIMARY KEY (from_id, to_id, date_str)
        )''')

        # МИГРАЦИИ (Для старых баз)
        try:
            await db.execute('SELECT full_name FROM users LIMIT 1')
        except Exception:
            print("⚠️ Миграция: full_name...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN full_name TEXT')
            except: pass

        try:
            await db.execute('SELECT mod_level FROM users LIMIT 1')
        except Exception:
            print("⚠️ Миграция: mod_level...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN mod_level INTEGER DEFAULT 0')
            except: pass

        try:
            await db.execute('SELECT reputation FROM users LIMIT 1')
        except Exception:
            print("⚠️ Миграция: reputation и last_wipe_date...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN reputation INTEGER DEFAULT 0')
                await db.execute('ALTER TABLE users ADD COLUMN last_wipe_date TEXT DEFAULT NULL')
            except: pass

        # Индексы и остальные таблицы
        await db.execute('CREATE INDEX IF NOT EXISTS idx_username ON users (username)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_level_xp ON users (level DESC, xp DESC)') 
        await db.execute('CREATE TABLE IF NOT EXISTS whitelist (item TEXT PRIMARY KEY)')
        await db.execute('CREATE TABLE IF NOT EXISTS badwords (word TEXT PRIMARY KEY)')
        await db.execute('''CREATE TABLE IF NOT EXISTS warn_reasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            user_id INTEGER, 
            reason TEXT
        )''')
        
        await db.commit()

async def get_user(user_id, username=None, full_name=None):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cursor = await db.execute('''
                SELECT user_id, username, full_name, xp, level, warns, mod_level, reputation 
                FROM users WHERE user_id = ?
            ''', (user_id,))
            row = await cursor.fetchone()
        except Exception as e:
            print(f"Ошибка БД: {e}")
            row = None
        
        clean_username = username.lstrip('@').lower() if username else None

        if not row:
            if not username: username = "Unknown"
            if not full_name: full_name = "User"
            await db.execute('''
                INSERT INTO users (user_id, username, full_name, xp, level, warns, mod_level, reputation) 
                VALUES (?, ?, ?, 0, 1, 0, 0, 0)
            ''', (user_id, clean_username, full_name))
            await db.commit()
            return (user_id, clean_username, full_name, 0, 1, 0, 0, 0)
        else:
            if username or full_name:
                await db.execute('UPDATE users SET username = ?, full_name = ? WHERE user_id = ?', 
                                 (clean_username, full_name, user_id))
                await db.commit()
            return row

async def get_id_by_username(username: str):
    clean_username = username.lstrip('@').lower()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('SELECT user_id FROM users WHERE username = ?', (clean_username,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def update_xp(user_id, xp_amount):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('SELECT xp, level FROM users WHERE user_id = ?', (user_id,))
        row = await cursor.fetchone()
        if not row: return (0, 0, 0)
        
        current_xp, current_lvl = row
        old_lvl = current_lvl
        
        new_xp = current_xp + xp_amount
        
        while new_xp < 0:
            if current_lvl > 1:
                current_lvl -= 1 
                prev_cap = LEVEL_CAPS.get(current_lvl, 500)
                new_xp = prev_cap + new_xp 
            else:
                new_xp = 0
                break
        
        cap = LEVEL_CAPS.get(current_lvl, float('inf'))
        while new_xp >= cap and current_lvl < 5:
            new_xp -= cap 
            current_lvl += 1
            cap = LEVEL_CAPS.get(current_lvl, float('inf'))
            
        await db.execute('UPDATE users SET xp = ?, level = ? WHERE user_id = ?', (new_xp, current_lvl, user_id))
        await db.commit()
        return (old_lvl, current_lvl, xp_amount)

async def give_reputation(from_user_id, to_user_id):
    if from_user_id == to_user_id:
        return "self_rep"

    today = datetime.now().strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('SELECT 1 FROM rep_history WHERE from_id = ? AND to_id = ? AND date_str = ?', 
                                  (from_user_id, to_user_id, today))
        if await cursor.fetchone():
            return "daily_limit_user" 
        
        cursor = await db.execute('SELECT count(*) FROM rep_history WHERE from_id = ? AND date_str = ?',
                                  (from_user_id, today))
        count = await cursor.fetchone()
        if count and count[0] >= 3:
            return "daily_limit_total"

        await db.execute('INSERT INTO rep_history (from_id, to_id, date_str) VALUES (?, ?, ?)',
                         (from_user_id, to_user_id, today))
        await db.execute('UPDATE users SET reputation = reputation + 1 WHERE user_id = ?', (to_user_id,))
        await db.commit()
        
        return "success"

async def check_wipe_cooldown(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('SELECT last_wipe_date FROM users WHERE user_id = ?', (user_id,))
        row = await cursor.fetchone()
        last_date = row[0] if row else None
        
        if last_date == today:
            return False
        
        await db.execute('UPDATE users SET last_wipe_date = ? WHERE user_id = ?', (today, user_id))
        await db.commit()
        return True

async def set_moderator_level(user_id: int, level: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('UPDATE users SET mod_level = ? WHERE user_id = ?', (level, user_id))
        await db.commit()

async def get_user_stats_full(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('''
            SELECT user_id, username, full_name, xp, level, warns, mod_level, reputation
            FROM users WHERE user_id = ?
        ''', (user_id,))
        row = await cursor.fetchone()
        return row

async def manage_warn(user_id: int, action: str = "add", reason: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        if action == "reset":
            await db.execute('UPDATE users SET warns = 0 WHERE user_id = ?', (user_id,))
            await db.execute('DELETE FROM warn_reasons WHERE user_id = ?', (user_id,))
            new_warns = 0
        elif action == "remove":
            await db.execute('UPDATE users SET warns = MAX(0, warns - 1) WHERE user_id = ?', (user_id,))
            await db.execute('DELETE FROM warn_reasons WHERE id = (SELECT MAX(id) FROM warn_reasons WHERE user_id = ?)', (user_id,))
            cursor = await db.execute('SELECT warns FROM users WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            new_warns = row[0] if row else 0
        else: 
            await db.execute('UPDATE users SET warns = warns + 1 WHERE user_id = ?', (user_id,))
            if reason:
                await db.execute('INSERT INTO warn_reasons (user_id, reason) VALUES (?, ?)', (user_id, reason))
            cursor = await db.execute('SELECT warns FROM users WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            new_warns = row[0] if row else 0
        await db.commit()
        return new_warns

async def get_warn_reasons(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cursor = await db.execute('SELECT reason FROM warn_reasons WHERE user_id = ?', (user_id,))
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
        except:
            return []

async def add_to_list(table, item):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            # table name should be validated or hardcoded in logic to prevent injection, 
            # but per instructions keeping logic simple for existing functions
            await db.execute(f'INSERT INTO {table} VALUES (?)', (item.lower(),))
            await db.commit()
            return True
        except:
            return False

async def remove_from_list(table, item):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f'DELETE FROM {table} WHERE item = ?', (item.lower(),))
        await db.commit()

async def get_list(table):
    async with aiosqlite.connect(DB_NAME) as db:
        field = 'item' if table == 'whitelist' else 'word'
        cursor = await db.execute(f'SELECT {field} FROM {table}')
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

async def clear_list_data(table):
    """Очищает список полностью"""
    if table not in ['whitelist', 'badwords']: return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f'DELETE FROM {table}')
        await db.commit()

# --- НОВЫЕ ФУНКЦИИ ДЛЯ ЛИДЕРОВ И СТАФФА ---

async def get_top_users(limit=10):
    """Возвращает топ пользователей по уровню и XP"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('''
            SELECT full_name, level, xp, user_id 
            FROM users 
            ORDER BY level DESC, xp DESC 
            LIMIT ?
        ''', (limit,))
        return await cursor.fetchall()

async def get_user_rank(user_id):
    """Возвращает место, уровень и XP пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
        # Получаем данные пользователя
        cursor = await db.execute('SELECT level, xp FROM users WHERE user_id = ?', (user_id,))
        user_data = await cursor.fetchone()
        if not user_data:
            return None
        
        u_lvl, u_xp = user_data
        
        # Считаем сколько людей выше (Level больше ИЛИ (Level такой же И XP больше))
        cursor = await db.execute('''
            SELECT COUNT(*) FROM users 
            WHERE level > ? OR (level = ? AND xp > ?)
        ''', (u_lvl, u_lvl, u_xp))
        count = await cursor.fetchone()
        rank = count[0] + 1 # +1 потому что если 0 людей выше, мы 1-е
        
        return rank, u_lvl, u_xp

async def get_all_staff():
    """Возвращает всех сотрудников (mod_level > 0), отсортированных по рангу"""
    async with aiosqlite.connect(DB_NAME) as db:
        # ДОБАВЛЕНО: user_id в выборку
        cursor = await db.execute('''
            SELECT full_name, mod_level, username, user_id
            FROM users 
            WHERE mod_level > 0 
            ORDER BY mod_level DESC
        ''', ())
        return await cursor.fetchall()