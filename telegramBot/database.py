import aiosqlite
import asyncpg
import asyncio
import aiohttp
import logging
import time
import json
import os
from datetime import datetime

DEFAULT_DB_FILENAME = "bot_database.db"


def _resolve_db_path() -> str:
    """
    Resolves SQLite path with Render-friendly priority:
    1) DB_PATH (file path or directory)
    2) RENDER_DISK_PATH (directory from Render persistent disk mount)
    3) local project file
    """
    db_path_env = os.getenv("DB_PATH")
    if db_path_env:
        if db_path_env.lower().endswith(".db"):
            return db_path_env
        return os.path.join(db_path_env, DEFAULT_DB_FILENAME)

    render_disk_path = os.getenv("RENDER_DISK_PATH")
    if render_disk_path:
        return os.path.join(render_disk_path, DEFAULT_DB_FILENAME)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, DEFAULT_DB_FILENAME)


DB_NAME = _resolve_db_path()
db_dir = os.path.dirname(DB_NAME)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

# РљРѕРЅС„РёРіСѓСЂР°С†РёСЏ СѓСЂРѕРІРЅРµР№ (XP Cap РґР»СЏ РєР°Р¶РґРѕРіРѕ СѓСЂРѕРІРЅСЏ)
LEVEL_CAPS = {
    1: 500,
    2: 2000,
    3: 8000,
    4: 25000,
    5: float('inf')
}
FREE_DICE_COOLDOWN_SECONDS = 12 * 60 * 60

SYNCABLE_TABLES = ["users", "rep_history", "whitelist", "badwords", "warn_reasons"]
DEFAULT_NEON_DSN = "postgresql://neondb_owner:npg_jPzy6UoZY3pe@ep-patient-tooth-albqdusn-pooler.c-3.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
NEON_DSN = os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL") or DEFAULT_NEON_DSN
# How often local loop checks whether sync should run (no Neon calls without dirty data).
SYNC_CHECK_INTERVAL_SECONDS = int(os.getenv("DB_SYNC_CHECK_INTERVAL", "120"))
# Minimum time between successful sync runs.
SYNC_MIN_INTERVAL_SECONDS = int(os.getenv("DB_SYNC_MIN_INTERVAL", "18000"))
# Retry delay after failed Neon sync attempts.
SYNC_RETRY_INTERVAL_SECONDS = int(os.getenv("DB_SYNC_RETRY_INTERVAL", "600"))
# Run sync only if enough local changes were accumulated (unless max interval exceeded).
SYNC_MIN_LOCAL_CHANGES = int(os.getenv("DB_SYNC_MIN_LOCAL_CHANGES", "120"))
# Force sync at least once per max interval while there are dirty changes.
SYNC_MAX_DIRTY_AGE_SECONDS = int(os.getenv("DB_SYNC_MAX_DIRTY_AGE", "604800"))

_neon_pool = None
_sync_task = None
_startup_sync_done = False
_last_alert_key = None
_last_alert_at = 0.0
_next_sync_attempt_at = 0.0
_sync_wakeup_event = asyncio.Event()
_last_neon_error = ""
_last_sync_check_at = 0.0

def _now_ts() -> float:
    return time.time()


async def _load_dirty_tables() -> set:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT value FROM sync_meta WHERE key = 'dirty_tables'")
        row = await cursor.fetchone()
        if not row or not row[0]:
            return set()
        try:
            data = json.loads(row[0])
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()


async def _save_dirty_tables(tables: set):
    payload = json.dumps(sorted(list(tables)))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('dirty_tables', ?)",
            (payload,),
        )
        await db.commit()


async def _get_meta_value(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT value FROM sync_meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else default


async def _set_meta_value(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def _get_meta_int(key: str, default: int = 0) -> int:
    value = await _get_meta_value(key, str(default))
    try:
        return int(value)
    except Exception:
        return default


async def _set_meta_int(key: str, value: int):
    await _set_meta_value(key, str(int(value)))


async def _inc_change_counter():
    current = await _get_meta_int("local_change_count", 0)
    await _set_meta_int("local_change_count", current + 1)


async def _mark_dirty(*tables: str):
    dirty = await _load_dirty_tables()
    changed = False
    for t in tables:
        if t in SYNCABLE_TABLES:
            if t not in dirty:
                changed = True
            dirty.add(t)
    if dirty and changed:
        await _save_dirty_tables(dirty)
    await _inc_change_counter()
    _sync_wakeup_event.set()


async def _clear_dirty(*tables: str):
    dirty = await _load_dirty_tables()
    for t in tables:
        dirty.discard(t)
    await _save_dirty_tables(dirty)


async def _resolve_alert_user_id() -> int:
    try:
        from config import OWNER_ID
        return int(OWNER_ID)
    except Exception:
        return 0


async def _send_db_alert(event_key: str, text: str):
    global _last_alert_key, _last_alert_at
    now = _now_ts()
    # Anti-spam: same event key no more than once per 5 minutes.
    if _last_alert_key == event_key and now - _last_alert_at < 300:
        return

    try:
        from config import BOT_TOKEN
    except Exception:
        return

    if not BOT_TOKEN:
        return

    user_id = await _resolve_alert_user_id()
    if not user_id:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": user_id, "text": f"[DB] {text}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10):
                pass
        _last_alert_key = event_key
        _last_alert_at = now
    except Exception:
        pass


async def _init_neon_pool():
    global _neon_pool, _last_neon_error
    if _neon_pool or not NEON_DSN:
        return
    try:
        _neon_pool = await asyncpg.create_pool(
            dsn=NEON_DSN,
            min_size=1,
            max_size=3,
            timeout=10,
        )
        _last_neon_error = ""
    except Exception as e:
        _last_neon_error = f"{type(e).__name__}: {e}"
        _neon_pool = None
        await _send_db_alert("neon_down", "Neon недоступен, работаем в локальном режиме.")


async def _ensure_neon_schema():
    if not _neon_pool:
        return
    async with _neon_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                warns INTEGER DEFAULT 0,
                mod_level INTEGER DEFAULT 0,
                reputation INTEGER DEFAULT 0,
                last_wipe_date TEXT DEFAULT NULL,
                last_free_dice_ts BIGINT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS rep_history (
                from_id BIGINT,
                to_id BIGINT,
                date_str TEXT,
                PRIMARY KEY (from_id, to_id, date_str)
            );
            CREATE TABLE IF NOT EXISTS whitelist (item TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS badwords (word TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS warn_reasons (
                id BIGINT PRIMARY KEY,
                user_id BIGINT,
                reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
            CREATE INDEX IF NOT EXISTS idx_users_level_xp ON users (level DESC, xp DESC);
            """
        )


async def _count_local_users() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def _count_neon_users() -> int:
    if not _neon_pool:
        return 0
    async with _neon_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) AS c FROM users")
        return int(row["c"] if row else 0)


async def _fetch_local_table(table: str):
    async with aiosqlite.connect(DB_NAME) as db:
        if table == "users":
            c = await db.execute("SELECT user_id, username, full_name, xp, level, warns, mod_level, reputation, last_wipe_date, last_free_dice_ts FROM users")
        elif table == "rep_history":
            c = await db.execute("SELECT from_id, to_id, date_str FROM rep_history")
        elif table == "whitelist":
            c = await db.execute("SELECT item FROM whitelist")
        elif table == "badwords":
            c = await db.execute("SELECT word FROM badwords")
        elif table == "warn_reasons":
            c = await db.execute("SELECT id, user_id, reason FROM warn_reasons")
        else:
            return []
        return await c.fetchall()


async def _replace_neon_table_from_local(table: str):
    if not _neon_pool:
        return
    rows = await _fetch_local_table(table)
    async with _neon_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"TRUNCATE TABLE {table}")
            if not rows:
                return
            if table == "users":
                await conn.executemany(
                    "INSERT INTO users (user_id, username, full_name, xp, level, warns, mod_level, reputation, last_wipe_date, last_free_dice_ts) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    rows,
                )
            elif table == "rep_history":
                await conn.executemany(
                    "INSERT INTO rep_history (from_id, to_id, date_str) VALUES ($1,$2,$3)",
                    rows,
                )
            elif table == "whitelist":
                await conn.executemany("INSERT INTO whitelist (item) VALUES ($1)", rows)
            elif table == "badwords":
                await conn.executemany("INSERT INTO badwords (word) VALUES ($1)", rows)
            elif table == "warn_reasons":
                await conn.executemany(
                    "INSERT INTO warn_reasons (id, user_id, reason) VALUES ($1,$2,$3)",
                    rows,
                )


async def _fetch_neon_table(table: str):
    if not _neon_pool:
        return []
    async with _neon_pool.acquire() as conn:
        if table == "users":
            rows = await conn.fetch("SELECT user_id, username, full_name, xp, level, warns, mod_level, reputation, last_wipe_date, last_free_dice_ts FROM users")
        elif table == "rep_history":
            rows = await conn.fetch("SELECT from_id, to_id, date_str FROM rep_history")
        elif table == "whitelist":
            rows = await conn.fetch("SELECT item FROM whitelist")
        elif table == "badwords":
            rows = await conn.fetch("SELECT word FROM badwords")
        elif table == "warn_reasons":
            rows = await conn.fetch("SELECT id, user_id, reason FROM warn_reasons ORDER BY id")
        else:
            rows = []
        return [tuple(r.values()) for r in rows]


async def _replace_local_table_from_neon(table: str):
    rows = await _fetch_neon_table(table)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f"DELETE FROM {table}")
        if table == "users" and rows:
            await db.executemany(
                "INSERT INTO users (user_id, username, full_name, xp, level, warns, mod_level, reputation, last_wipe_date, last_free_dice_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        elif table == "rep_history" and rows:
            await db.executemany(
                "INSERT INTO rep_history (from_id, to_id, date_str) VALUES (?, ?, ?)",
                rows,
            )
        elif table == "whitelist" and rows:
            await db.executemany("INSERT INTO whitelist (item) VALUES (?)", rows)
        elif table == "badwords" and rows:
            await db.executemany("INSERT INTO badwords (word) VALUES (?)", rows)
        elif table == "warn_reasons" and rows:
            await db.executemany(
                "INSERT INTO warn_reasons (id, user_id, reason) VALUES (?, ?, ?)",
                rows,
            )
        await db.commit()


async def _sync_dirty_tables_once():
    if not _neon_pool:
        return
    dirty = await _load_dirty_tables()
    if not dirty:
        return
    synced = []
    for table in list(dirty):
        try:
            await _replace_neon_table_from_local(table)
            synced.append(table)
        except Exception:
            await _send_db_alert("sync_failed", f"РћС€РёР±РєР° СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёРё С‚Р°Р±Р»РёС†С‹ {table} РІ Neon.")
            break
    if synced:
        await _clear_dirty(*synced)
        await _set_meta_int("local_change_count", 0)
        await _set_meta_int("last_sync_at", int(_now_ts()))


async def _sync_loop():
    global _next_sync_attempt_at, _last_sync_check_at
    while True:
        # Event-driven mode: no periodic checks while idle/dirty.
        await _sync_wakeup_event.wait()
        _sync_wakeup_event.clear()

        now = _now_ts()
        # Throttle local sync condition checks.
        if _last_sync_check_at and (now - _last_sync_check_at) < SYNC_CHECK_INTERVAL_SECONDS:
            continue
        _last_sync_check_at = now

        dirty = await _load_dirty_tables()
        if not dirty:
            continue

        if now < _next_sync_attempt_at:
            continue

        local_changes = await _get_meta_int("local_change_count", 0)
        last_sync_at = await _get_meta_int("last_sync_at", 0)
        dirty_age_ok = (last_sync_at == 0) or (now - last_sync_at >= SYNC_MAX_DIRTY_AGE_SECONDS)
        enough_changes = local_changes >= SYNC_MIN_LOCAL_CHANGES
        min_interval_ok = (last_sync_at == 0) or (now - last_sync_at >= SYNC_MIN_INTERVAL_SECONDS)

        if not min_interval_ok:
            continue
        if not (enough_changes or dirty_age_ok):
            continue

        was_disconnected = _neon_pool is None
        if not _neon_pool:
            await _init_neon_pool()
            if not _neon_pool:
                _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS
                continue
            await _ensure_neon_schema()
            if was_disconnected:
                await _send_db_alert("neon_back", "Neon СЃРЅРѕРІР° РґРѕСЃС‚СѓРїРµРЅ, СЃРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ РІРѕР·РѕР±РЅРѕРІР»РµРЅР°.")
        try:
            await _sync_dirty_tables_once()
            _next_sync_attempt_at = now + SYNC_MIN_INTERVAL_SECONDS
        except Exception:
            _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS


async def _startup_bootstrap():
    global _startup_sync_done, _sync_task
    if _startup_sync_done:
        return
    _startup_sync_done = True

    await _init_neon_pool()
    if not _neon_pool:
        return
    await _ensure_neon_schema()

    local_users = await _count_local_users()
    neon_users = await _count_neon_users()

    if local_users == 0 and neon_users > 0:
        for t in SYNCABLE_TABLES:
            await _replace_local_table_from_neon(t)
        await _clear_dirty(*SYNCABLE_TABLES)
        await _send_db_alert("local_restore", "Р›РѕРєР°Р»СЊРЅР°СЏ Р‘Р” Р±С‹Р»Р° РїСѓСЃС‚Р°СЏ, РґР°РЅРЅС‹Рµ РїРѕРґС‚СЏРЅСѓС‚С‹ РёР· Neon.")
    elif local_users > 0 and neon_users == 0:
        await _mark_dirty(*SYNCABLE_TABLES)
        await _sync_dirty_tables_once()
        await _send_db_alert("neon_seed", "Neon Р±С‹Р» РїСѓСЃС‚РѕР№, РІС‹РїРѕР»РЅРµРЅ РїРµСЂРІРёС‡РЅС‹Р№ СЌРєСЃРїРѕСЂС‚ РёР· Р»РѕРєР°Р»СЊРЅРѕР№ Р‘Р”.")

    if not _sync_task:
        _sync_task = asyncio.create_task(_sync_loop())

async def create_tables():
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. РћСЃРЅРѕРІРЅР°СЏ С‚Р°Р±Р»РёС†Р° РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            warns INTEGER DEFAULT 0,
            mod_level INTEGER DEFAULT 0,
            reputation INTEGER DEFAULT 0,
            last_wipe_date TEXT DEFAULT NULL,
            last_free_dice_ts INTEGER DEFAULT 0
        )''')
        
        # 2. РўР°Р±Р»РёС†Р° РёСЃС‚РѕСЂРёРё СЂРµРїСѓС‚Р°С†РёРё
        await db.execute('''CREATE TABLE IF NOT EXISTS rep_history (
            from_id INTEGER,
            to_id INTEGER,
            date_str TEXT,
            PRIMARY KEY (from_id, to_id, date_str)
        )''')

        # РњРР“Р РђР¦РР (Р”Р»СЏ СЃС‚Р°СЂС‹С… Р±Р°Р·)
        try:
            await db.execute('SELECT full_name FROM users LIMIT 1')
        except Exception:
            print("вљ пёЏ РњРёРіСЂР°С†РёСЏ: full_name...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN full_name TEXT')
            except: pass

        try:
            await db.execute('SELECT mod_level FROM users LIMIT 1')
        except Exception:
            print("вљ пёЏ РњРёРіСЂР°С†РёСЏ: mod_level...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN mod_level INTEGER DEFAULT 0')
            except: pass

        try:
            await db.execute('SELECT reputation FROM users LIMIT 1')
        except Exception:
            print("вљ пёЏ РњРёРіСЂР°С†РёСЏ: reputation Рё last_wipe_date...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN reputation INTEGER DEFAULT 0')
                await db.execute('ALTER TABLE users ADD COLUMN last_wipe_date TEXT DEFAULT NULL')
            except: pass

        try:
            await db.execute('SELECT last_free_dice_ts FROM users LIMIT 1')
        except Exception:
            try:
                await db.execute('ALTER TABLE users ADD COLUMN last_free_dice_ts INTEGER DEFAULT 0')
            except:
                pass

        # РРЅРґРµРєСЃС‹ Рё РѕСЃС‚Р°Р»СЊРЅС‹Рµ С‚Р°Р±Р»РёС†С‹
        await db.execute('CREATE INDEX IF NOT EXISTS idx_username ON users (username)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_level_xp ON users (level DESC, xp DESC)') 
        await db.execute('CREATE TABLE IF NOT EXISTS whitelist (item TEXT PRIMARY KEY)')
        await db.execute('CREATE TABLE IF NOT EXISTS badwords (word TEXT PRIMARY KEY)')
        await db.execute('''CREATE TABLE IF NOT EXISTS warn_reasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            user_id INTEGER, 
            reason TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        
        await db.commit()
    await _startup_bootstrap()

async def get_user(user_id, username=None, full_name=None):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cursor = await db.execute('''
                SELECT user_id, username, full_name, xp, level, warns, mod_level, reputation, last_free_dice_ts
                FROM users WHERE user_id = ?
            ''', (user_id,))
            row = await cursor.fetchone()
        except Exception as e:
            print(f"РћС€РёР±РєР° Р‘Р”: {e}")
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
            await _mark_dirty("users")
            return (user_id, clean_username, full_name, 0, 1, 0, 0, 0, 0)
        else:
            if username or full_name:
                await db.execute('UPDATE users SET username = ?, full_name = ? WHERE user_id = ?', 
                                 (clean_username, full_name, user_id))
                await db.commit()
                await _mark_dirty("users")
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
        await _mark_dirty("users")
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
        await _mark_dirty("rep_history", "users")
        
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
        await _mark_dirty("users")
        return True


async def get_free_dice_remaining(user_id: int) -> int:
    await get_user(user_id)
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('SELECT last_free_dice_ts FROM users WHERE user_id = ?', (user_id,))
        row = await cursor.fetchone()
        last_ts = int(row[0]) if row and row[0] else 0

    next_ts = last_ts + FREE_DICE_COOLDOWN_SECONDS
    return max(0, next_ts - now)


async def claim_free_dice(user_id: int) -> bool:
    remaining = await get_free_dice_remaining(user_id)
    if remaining > 0:
        return False

    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('UPDATE users SET last_free_dice_ts = ? WHERE user_id = ?', (now, user_id))
        await db.commit()
    await _mark_dirty("users")
    return True


async def reset_free_dice_cooldown(user_id: int) -> bool:
    await get_user(user_id)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('UPDATE users SET last_free_dice_ts = 0 WHERE user_id = ?', (user_id,))
        await db.commit()
    await _mark_dirty("users")
    return True

async def set_moderator_level(user_id: int, level: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('UPDATE users SET mod_level = ? WHERE user_id = ?', (level, user_id))
        await db.commit()
    await _mark_dirty("users")

async def get_user_stats_full(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('''
            SELECT user_id, username, full_name, xp, level, warns, mod_level, reputation, last_free_dice_ts
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
        await _mark_dirty("users", "warn_reasons")
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
            await _mark_dirty(table)
            return True
        except:
            return False

async def remove_from_list(table, item):
    async with aiosqlite.connect(DB_NAME) as db:
        # РСЃРїСЂР°РІР»РµРЅРѕ: РІС‹Р±РёСЂР°РµРј РїСЂР°РІРёР»СЊРЅРѕРµ РёРјСЏ СЃС‚РѕР»Р±С†Р° РІ Р·Р°РІРёСЃРёРјРѕСЃС‚Рё РѕС‚ С‚Р°Р±Р»РёС†С‹
        field = 'item' if table == 'whitelist' else 'word'
        await db.execute(f'DELETE FROM {table} WHERE {field} = ?', (item.lower(),))
        await db.commit()
    await _mark_dirty(table)

async def get_list(table):
    async with aiosqlite.connect(DB_NAME) as db:
        field = 'item' if table == 'whitelist' else 'word'
        cursor = await db.execute(f'SELECT {field} FROM {table}')
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

async def clear_list_data(table):
    """РћС‡РёС‰Р°РµС‚ СЃРїРёСЃРѕРє РїРѕР»РЅРѕСЃС‚СЊСЋ"""
    if table not in ['whitelist', 'badwords']: return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f'DELETE FROM {table}')
        await db.commit()
    await _mark_dirty(table)


async def get_db_sync_status():
    dirty = sorted(list(await _load_dirty_tables()))
    local_changes = await _get_meta_int("local_change_count", 0)
    last_sync_at = await _get_meta_int("last_sync_at", 0)
    alert_user_id = await _resolve_alert_user_id()

    return {
        "db_path": DB_NAME,
        "neon_configured": bool(NEON_DSN),
        "neon_connected": _neon_pool is not None,
        "dirty_tables": dirty,
        "local_change_count": local_changes,
        "last_sync_at": last_sync_at,
        "sync_check_interval": SYNC_CHECK_INTERVAL_SECONDS,
        "sync_min_interval": SYNC_MIN_INTERVAL_SECONDS,
        "sync_retry_interval": SYNC_RETRY_INTERVAL_SECONDS,
        "sync_min_local_changes": SYNC_MIN_LOCAL_CHANGES,
        "sync_max_dirty_age": SYNC_MAX_DIRTY_AGE_SECONDS,
        "alert_user_id": alert_user_id,
        "next_sync_attempt_at": int(_next_sync_attempt_at),
        "last_neon_error": _last_neon_error or "",
    }


async def trigger_manual_sync():
    """
    Force one sync attempt now (ignores schedule thresholds).
    Returns dict with result status for UI.
    """
    global _next_sync_attempt_at
    dirty = await _load_dirty_tables()
    if not dirty:
        return {"ok": True, "message": "Синхронизация не нужна: изменений нет."}

    await _init_neon_pool()
    if not _neon_pool:
        _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
        details = _last_neon_error or "причина не зафиксирована"
        return {"ok": False, "message": f"Neon недоступен, ручной sync не выполнен.\nДетали: {details}"}

    try:
        await _ensure_neon_schema()
        await _sync_dirty_tables_once()
        _next_sync_attempt_at = _now_ts() + SYNC_MIN_INTERVAL_SECONDS
        return {"ok": True, "message": "Ручная синхронизация выполнена успешно."}
    except Exception as e:
        _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
        return {"ok": False, "message": f"Ошибка ручного sync: {e}"}

# --- РќРћР’Р«Р• Р¤РЈРќРљР¦РР Р”Р›РЇ Р›РР”Р•Р РћР’ Р РЎРўРђР¤Р¤Рђ ---

async def get_top_users(limit=10):
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ С‚РѕРї РїРѕР»СЊР·РѕРІР°С‚РµР»РµР№ РїРѕ СѓСЂРѕРІРЅСЋ Рё XP"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('''
            SELECT full_name, level, xp, user_id 
            FROM users 
            ORDER BY level DESC, xp DESC 
            LIMIT ?
        ''', (limit,))
        return await cursor.fetchall()

async def get_user_rank(user_id):
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ РјРµСЃС‚Рѕ, СѓСЂРѕРІРµРЅСЊ Рё XP РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ"""
    async with aiosqlite.connect(DB_NAME) as db:
        # РџРѕР»СѓС‡Р°РµРј РґР°РЅРЅС‹Рµ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ
        cursor = await db.execute('SELECT level, xp FROM users WHERE user_id = ?', (user_id,))
        user_data = await cursor.fetchone()
        if not user_data:
            return None
        
        u_lvl, u_xp = user_data
        
        # РЎС‡РёС‚Р°РµРј СЃРєРѕР»СЊРєРѕ Р»СЋРґРµР№ РІС‹С€Рµ (Level Р±РѕР»СЊС€Рµ РР›Р (Level С‚Р°РєРѕР№ Р¶Рµ Р XP Р±РѕР»СЊС€Рµ))
        cursor = await db.execute('''
            SELECT COUNT(*) FROM users 
            WHERE level > ? OR (level = ? AND xp > ?)
        ''', (u_lvl, u_lvl, u_xp))
        count = await cursor.fetchone()
        rank = count[0] + 1 # +1 РїРѕС‚РѕРјСѓ С‡С‚Рѕ РµСЃР»Рё 0 Р»СЋРґРµР№ РІС‹С€Рµ, РјС‹ 1-Рµ
        
        return rank, u_lvl, u_xp

async def get_all_staff():
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ РІСЃРµС… СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ (mod_level > 0), РѕС‚СЃРѕСЂС‚РёСЂРѕРІР°РЅРЅС‹С… РїРѕ СЂР°РЅРіСѓ"""
    async with aiosqlite.connect(DB_NAME) as db:
        # Р”РћР‘РђР’Р›Р•РќРћ: user_id РІ РІС‹Р±РѕСЂРєСѓ
        cursor = await db.execute('''
            SELECT full_name, mod_level, username, user_id
            FROM users 
            WHERE mod_level > 0 
            ORDER BY mod_level DESC
        ''', ())
        return await cursor.fetchall()

