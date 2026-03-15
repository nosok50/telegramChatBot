import aiosqlite
import asyncpg
import asyncio
import aiohttp
import logging
import time
import json
import os
import sqlite3
import random
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
FARM_GRID_SIZE = 3
FARM_CELLS_TOTAL = FARM_GRID_SIZE * FARM_GRID_SIZE
FARM_CENTER_IDX = 4
FARM_UNLOCK_COSTS = [500, 500, 2500, 2500, 10000, 10000, 40000, 40000]
FARM_STARTER_COINS = 150
FARM_BASE_CYCLE_SECONDS = 4 * 60 * 60
FARM_BASE_CAP_SECONDS = 4 * 60 * 60

SYNCABLE_TABLES = ["users", "rep_history", "whitelist", "badwords", "warn_reasons", "farm_players", "farm_cells"]
DEFAULT_NEON_DSN = "postgresql://neondb_owner:npg_jPzy6UoZY3pe@ep-patient-tooth-albqdusn-pooler.c-3.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"


def _normalize_neon_dsn(raw_dsn: str) -> str:
    if not raw_dsn:
        return ""
    parts = urlsplit(raw_dsn)
    if not parts.query:
        return raw_dsn
    supported = {"sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl", "sslpassword"}
    query = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k.lower() in supported]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


NEON_DSN = _normalize_neon_dsn(os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL") or DEFAULT_NEON_DSN)
# Optional local cooldown between event-driven sync condition checks.
SYNC_CHECK_INTERVAL_SECONDS = int(os.getenv("DB_SYNC_CHECK_INTERVAL", "0"))
# Minimum time between successful sync runs.
SYNC_MIN_INTERVAL_SECONDS = int(os.getenv("DB_SYNC_MIN_INTERVAL", "18000"))
# Retry delay after failed Neon sync attempts.
SYNC_RETRY_INTERVAL_SECONDS = int(os.getenv("DB_SYNC_RETRY_INTERVAL", "600"))
# Run sync only if enough user activity messages were accumulated.
SYNC_MIN_ACTIVITY_MESSAGES = int(os.getenv("DB_SYNC_MIN_ACTIVITY_MESSAGES", "100"))

_neon_pool = None
_sync_task = None
_startup_sync_done = False
_last_alert_key = None
_last_alert_at = 0.0
_next_sync_attempt_at = 0.0
_sync_wakeup_event = asyncio.Event()
_last_neon_error = ""
_last_sync_check_at = 0.0
_db_import_lock = asyncio.Lock()


def _set_last_neon_error(text: str):
    global _last_neon_error
    _last_neon_error = text or ""

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


async def _inc_activity_counter(amount: int = 1):
    current = await _get_meta_int("sync_activity_count", 0)
    await _set_meta_int("sync_activity_count", current + max(1, int(amount or 1)))


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


async def register_sync_activity(amount: int = 1):
    await _inc_activity_counter(amount)
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
            await _send_db_file_to_owner(session, BOT_TOKEN, user_id, event_key)
        _last_alert_key = event_key
        _last_alert_at = now
    except Exception:
        pass


async def _send_db_file_to_owner(session: aiohttp.ClientSession, token: str, user_id: int, event_key: str):
    if not os.path.exists(DB_NAME):
        return
    try:
        file_size = os.path.getsize(DB_NAME)
    except Exception:
        return
    # Telegram bot API document size limit is much higher than our expected SQLite backups,
    # but keep a hard cap to avoid unexpected huge uploads on free hosting.
    if file_size > 45 * 1024 * 1024:
        return

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    form = aiohttp.FormData()
    form.add_field("chat_id", str(user_id))
    form.add_field("caption", f"[DB] Резервная копия ({event_key})")
    with open(DB_NAME, "rb") as db_file:
        form.add_field(
            "document",
            db_file,
            filename=os.path.basename(DB_NAME),
            content_type="application/octet-stream",
        )
        async with session.post(url, data=form, timeout=30):
            pass


async def _init_neon_pool():
    global _neon_pool
    if _neon_pool or not NEON_DSN:
        return
    try:
        _neon_pool = await asyncpg.create_pool(
            dsn=NEON_DSN,
            min_size=1,
            max_size=3,
            timeout=10,
        )
        _set_last_neon_error("")
    except Exception as e:
        _set_last_neon_error(f"{type(e).__name__}: {e}")
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
            CREATE TABLE IF NOT EXISTS farm_players (
                user_id BIGINT PRIMARY KEY,
                coins BIGINT DEFAULT 0,
                opened_cells TEXT NOT NULL,
                moving_from INTEGER DEFAULT NULL,
                created_at BIGINT DEFAULT 0,
                updated_at BIGINT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS farm_cells (
                user_id BIGINT,
                cell_idx INTEGER,
                module_type TEXT,
                level INTEGER DEFAULT 1,
                spec TEXT DEFAULT '',
                status TEXT DEFAULT 'ok',
                status_since BIGINT DEFAULT 0,
                last_collect_ts BIGINT DEFAULT 0,
                last_event_check_ts BIGINT DEFAULT 0,
                PRIMARY KEY (user_id, cell_idx)
            );
            CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
            CREATE INDEX IF NOT EXISTS idx_users_level_xp ON users (level DESC, xp DESC);
            CREATE INDEX IF NOT EXISTS idx_farm_cells_user ON farm_cells (user_id);
            """
        )
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS xp INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 1")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS warns INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS mod_level INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reputation INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_wipe_date TEXT DEFAULT NULL")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_free_dice_ts BIGINT DEFAULT 0")
        await conn.execute("ALTER TABLE farm_players ADD COLUMN IF NOT EXISTS coins BIGINT DEFAULT 0")
        await conn.execute("ALTER TABLE farm_players ADD COLUMN IF NOT EXISTS opened_cells TEXT DEFAULT '[4]'")
        await conn.execute("ALTER TABLE farm_players ADD COLUMN IF NOT EXISTS moving_from INTEGER DEFAULT NULL")
        await conn.execute("ALTER TABLE farm_players ADD COLUMN IF NOT EXISTS created_at BIGINT DEFAULT 0")
        await conn.execute("ALTER TABLE farm_players ADD COLUMN IF NOT EXISTS updated_at BIGINT DEFAULT 0")
        await conn.execute("ALTER TABLE farm_cells ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 1")
        await conn.execute("ALTER TABLE farm_cells ADD COLUMN IF NOT EXISTS spec TEXT DEFAULT ''")
        await conn.execute("ALTER TABLE farm_cells ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ok'")
        await conn.execute("ALTER TABLE farm_cells ADD COLUMN IF NOT EXISTS status_since BIGINT DEFAULT 0")
        await conn.execute("ALTER TABLE farm_cells ADD COLUMN IF NOT EXISTS last_collect_ts BIGINT DEFAULT 0")
        await conn.execute("ALTER TABLE farm_cells ADD COLUMN IF NOT EXISTS last_event_check_ts BIGINT DEFAULT 0")


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
        elif table == "farm_players":
            c = await db.execute("SELECT user_id, coins, opened_cells, moving_from, created_at, updated_at FROM farm_players")
        elif table == "farm_cells":
            c = await db.execute("SELECT user_id, cell_idx, module_type, level, spec, status, status_since, last_collect_ts, last_event_check_ts FROM farm_cells")
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
            elif table == "farm_players":
                await conn.executemany(
                    "INSERT INTO farm_players (user_id, coins, opened_cells, moving_from, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6)",
                    rows,
                )
            elif table == "farm_cells":
                await conn.executemany(
                    "INSERT INTO farm_cells (user_id, cell_idx, module_type, level, spec, status, status_since, last_collect_ts, last_event_check_ts) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
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
        elif table == "farm_players":
            rows = await conn.fetch("SELECT user_id, coins, opened_cells, moving_from, created_at, updated_at FROM farm_players")
        elif table == "farm_cells":
            rows = await conn.fetch("SELECT user_id, cell_idx, module_type, level, spec, status, status_since, last_collect_ts, last_event_check_ts FROM farm_cells")
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
        elif table == "farm_players" and rows:
            await db.executemany(
                "INSERT INTO farm_players (user_id, coins, opened_cells, moving_from, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        elif table == "farm_cells" and rows:
            await db.executemany(
                "INSERT INTO farm_cells (user_id, cell_idx, module_type, level, spec, status, status_since, last_collect_ts, last_event_check_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        await db.commit()


async def _sync_dirty_tables_once():
    if not _neon_pool:
        return {"ok": False, "synced": [], "failed_table": None, "error": "Neon не подключен"}
    dirty = await _load_dirty_tables()
    if not dirty:
        return {"ok": True, "synced": [], "failed_table": None, "error": ""}
    synced = []
    failed_table = None
    failure_error = ""
    for table in list(dirty):
        try:
            await _replace_neon_table_from_local(table)
            synced.append(table)
        except Exception as e:
            failed_table = table
            failure_error = f"{type(e).__name__}: {e}"
            _set_last_neon_error(f"Ошибка синхронизации таблицы {table}: {failure_error}")
            await _send_db_alert("sync_failed", f"Ошибка синхронизации таблицы {table} в Neon.\n{failure_error}")
            break
    if synced:
        await _clear_dirty(*synced)
        if failed_table is None:
            await _set_meta_int("local_change_count", 0)
            await _set_meta_int("last_sync_at", int(_now_ts()))
            _set_last_neon_error("")
    return {
        "ok": failed_table is None,
        "synced": synced,
        "failed_table": failed_table,
        "error": failure_error,
    }


async def _sync_loop():
    global _next_sync_attempt_at, _last_sync_check_at
    while True:
        # Strict event-driven mode: check sync only after local writes.
        await _sync_wakeup_event.wait()
        _sync_wakeup_event.clear()

        now = _now_ts()
        if SYNC_CHECK_INTERVAL_SECONDS > 0 and _last_sync_check_at and (now - _last_sync_check_at) < SYNC_CHECK_INTERVAL_SECONDS:
            continue
        _last_sync_check_at = now

        dirty = await _load_dirty_tables()
        if not dirty:
            continue

        if now < _next_sync_attempt_at:
            continue

        local_changes = await _get_meta_int("local_change_count", 0)
        activity_messages = await _get_meta_int("sync_activity_count", 0)
        last_sync_at = await _get_meta_int("last_sync_at", 0)
        min_interval_ok = (last_sync_at == 0) or (now - last_sync_at >= SYNC_MIN_INTERVAL_SECONDS)

        if not min_interval_ok:
            continue
        if activity_messages < SYNC_MIN_ACTIVITY_MESSAGES:
            continue

        was_disconnected = _neon_pool is None
        if not _neon_pool:
            await _init_neon_pool()
            if not _neon_pool:
                _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS
                continue
            await _ensure_neon_schema()
            if was_disconnected:
                await _send_db_alert("neon_back", "Neon снова доступен, синхронизация возобновлена.")
        try:
            result = await _sync_dirty_tables_once()
            if result["ok"]:
                await _set_meta_int("sync_activity_count", 0)
            _next_sync_attempt_at = now + (SYNC_MIN_INTERVAL_SECONDS if result["ok"] else SYNC_RETRY_INTERVAL_SECONDS)
        except Exception:
            _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS


async def _startup_bootstrap():
    global _startup_sync_done, _sync_task
    if _startup_sync_done:
        return
    _startup_sync_done = True

    if not _sync_task:
        _sync_task = asyncio.create_task(_sync_loop())

    # By default do not touch Neon on startup to preserve free-tier compute hours.
    # Enable old bootstrap behavior only if explicitly requested.
    if os.getenv("DB_STARTUP_BOOTSTRAP_NEON", "0") != "1":
        return

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
        await _send_db_alert("local_restore", "Локальная БД была пустая, данные подтянуты из Neon.")
    elif local_users > 0 and neon_users == 0:
        await _mark_dirty(*SYNCABLE_TABLES)
        await _sync_dirty_tables_once()
        await _send_db_alert("neon_seed", "Neon был пустой, выполнен первичный экспорт из локальной БД.")

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
            last_wipe_date TEXT DEFAULT NULL,
            last_free_dice_ts INTEGER DEFAULT 0
        )''')
        
        # 2. Таблица истории репутации
        await db.execute('''CREATE TABLE IF NOT EXISTS rep_history (
            from_id INTEGER,
            to_id INTEGER,
            date_str TEXT,
            PRIMARY KEY (from_id, to_id, date_str)
        )''')

        # Миграции для старых баз
        try:
            await db.execute('SELECT full_name FROM users LIMIT 1')
        except Exception:
            print("Миграция: full_name...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN full_name TEXT')
            except: pass

        try:
            await db.execute('SELECT mod_level FROM users LIMIT 1')
        except Exception:
            print("Миграция: mod_level...")
            try:
                await db.execute('ALTER TABLE users ADD COLUMN mod_level INTEGER DEFAULT 0')
            except: pass

        try:
            await db.execute('SELECT reputation FROM users LIMIT 1')
        except Exception:
            print("Миграция: reputation и last_wipe_date...")
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
        await db.execute('''CREATE TABLE IF NOT EXISTS farm_players (
            user_id INTEGER PRIMARY KEY,
            coins INTEGER DEFAULT 0,
            opened_cells TEXT NOT NULL,
            moving_from INTEGER DEFAULT NULL,
            created_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS farm_cells (
            user_id INTEGER NOT NULL,
            cell_idx INTEGER NOT NULL,
            module_type TEXT NOT NULL,
            level INTEGER DEFAULT 1,
            spec TEXT DEFAULT '',
            status TEXT DEFAULT 'ok',
            status_since INTEGER DEFAULT 0,
            last_collect_ts INTEGER DEFAULT 0,
            last_event_check_ts INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, cell_idx)
        )''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_farm_cells_user ON farm_cells (user_id)')
        
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
    """Очищает список полностью."""
    if table not in ['whitelist', 'badwords']: return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(f'DELETE FROM {table}')
        await db.commit()
    await _mark_dirty(table)


async def get_db_sync_status():
    dirty = sorted(list(await _load_dirty_tables()))
    local_changes = await _get_meta_int("local_change_count", 0)
    activity_messages = await _get_meta_int("sync_activity_count", 0)
    last_sync_at = await _get_meta_int("last_sync_at", 0)
    alert_user_id = await _resolve_alert_user_id()

    return {
        "db_path": DB_NAME,
        "neon_configured": bool(NEON_DSN),
        "neon_connected": _neon_pool is not None,
        "sync_loop_running": _sync_task is not None and not _sync_task.done(),
        "dirty_tables": dirty,
        "local_change_count": local_changes,
        "activity_message_count": activity_messages,
        "last_sync_at": last_sync_at,
        "sync_check_interval": SYNC_CHECK_INTERVAL_SECONDS,
        "sync_min_interval": SYNC_MIN_INTERVAL_SECONDS,
        "sync_retry_interval": SYNC_RETRY_INTERVAL_SECONDS,
        "sync_min_activity_messages": SYNC_MIN_ACTIVITY_MESSAGES,
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
        result = await _sync_dirty_tables_once()
        if result["ok"]:
            await _set_meta_int("sync_activity_count", 0)
            _next_sync_attempt_at = _now_ts() + SYNC_MIN_INTERVAL_SECONDS
            if result["synced"]:
                synced_list = ", ".join(result["synced"])
                return {"ok": True, "message": f"Ручная синхронизация выполнена успешно.\nСинхронизировано таблиц: {synced_list}"}
            return {"ok": True, "message": "Изменения уже были синхронизированы, новых таблиц не найдено."}

        _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
        table = result["failed_table"] or "неизвестно"
        details = result["error"] or "причина не зафиксирована"
        return {"ok": False, "message": f"Ошибка ручного sync таблицы {table}.\nДетали: {details}"}
    except Exception as e:
        _set_last_neon_error(f"{type(e).__name__}: {e}")
        _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
        return {"ok": False, "message": f"Ошибка ручного sync: {e}"}


def _validate_sqlite_file_sync(db_file_path: str):
    with sqlite3.connect(db_file_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
        cur.fetchone()
        cur.execute("PRAGMA integrity_check")
        row = cur.fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise ValueError("SQLite integrity_check failed")


def _replace_local_db_sync(src_file_path: str):
    os.makedirs(os.path.dirname(DB_NAME) or ".", exist_ok=True)
    with sqlite3.connect(src_file_path) as src_conn:
        with sqlite3.connect(DB_NAME) as dst_conn:
            src_conn.backup(dst_conn)
            dst_conn.commit()


async def import_local_db_from_file(uploaded_db_path: str):
    """
    Imports SQLite DB from uploaded file into the active local DB.
    Then marks syncable tables dirty and immediately tries one Neon sync.
    """
    async with _db_import_lock:
        await asyncio.to_thread(_validate_sqlite_file_sync, uploaded_db_path)
        await asyncio.to_thread(_replace_local_db_sync, uploaded_db_path)
        await create_tables()
        await _mark_dirty(*SYNCABLE_TABLES)
        sync_result = await trigger_manual_sync()
        return {
            "ok": True,
            "message": "Локальная БД успешно обновлена из файла.",
            "sync": sync_result,
        }


FARM_SPEED_BONUS = {1: 0.15, 2: 0.30, 3: 0.50}
FARM_BATTERY_HOURS = {1: 2, 2: 4, 3: 8}
FARM_STABILIZER_REDUCTION = {1: 0.10, 2: 0.17, 3: 0.20}
FARM_GEN_BUY_COST = 100
FARM_GEN_UPGRADE_COSTS = {2: 400, 3: 2000}
FARM_SUPPORT_COSTS = {
    "speed": {1: 250, 2: 1000, 3: 4000},
    "battery": {1: 300, 2: 1200, 3: 5000},
    "stabilizer": {1: 400, 2: 1500, 3: 6000},
}


def _farm_now() -> int:
    return int(_now_ts())


def _farm_neighbors(cell_idx: int):
    row, col = divmod(cell_idx, FARM_GRID_SIZE)
    neighbors = []
    if row > 0:
        neighbors.append((row - 1) * FARM_GRID_SIZE + col)
    if row < FARM_GRID_SIZE - 1:
        neighbors.append((row + 1) * FARM_GRID_SIZE + col)
    if col > 0:
        neighbors.append(row * FARM_GRID_SIZE + (col - 1))
    if col < FARM_GRID_SIZE - 1:
        neighbors.append(row * FARM_GRID_SIZE + (col + 1))
    return neighbors


def _farm_parse_opened_cells(raw: str):
    try:
        parsed = json.loads(raw or "[]")
        if isinstance(parsed, list):
            items = sorted({int(x) for x in parsed if 0 <= int(x) < FARM_CELLS_TOTAL})
            if FARM_CENTER_IDX not in items:
                items.append(FARM_CENTER_IDX)
            return sorted(items)
    except Exception:
        pass
    return [FARM_CENTER_IDX]


def _farm_opened_cells_payload(opened_cells):
    return json.dumps(sorted({int(x) for x in opened_cells if 0 <= int(x) < FARM_CELLS_TOTAL}))


def _farm_next_unlock_cost(opened_count: int):
    if opened_count >= FARM_CELLS_TOTAL:
        return None
    return FARM_UNLOCK_COSTS[opened_count - 1]


def _farm_generator_output(level: int, spec: str):
    if level <= 1:
        return 50, 2
    if level == 2:
        return 150, 5
    if spec == "xp":
        return 0, 15
    return 600, 0


def _farm_get_support_bonuses(cells_by_idx: dict, cell_idx: int):
    speed = 0.0
    battery_hours = 0
    stabilization_reduction = 0.0
    for n in _farm_neighbors(cell_idx):
        cell = cells_by_idx.get(n)
        if not cell:
            continue
        m_type = cell["module_type"]
        lvl = int(cell["level"] or 1)
        if m_type == "speed":
            speed += FARM_SPEED_BONUS.get(lvl, 0.0)
        elif m_type == "battery":
            battery_hours += FARM_BATTERY_HOURS.get(lvl, 0)
        elif m_type == "stabilizer":
            stabilization_reduction += FARM_STABILIZER_REDUCTION.get(lvl, 0.0)
    speed = min(speed, 0.90)
    stabilization_reduction = min(stabilization_reduction, 0.20)
    return {
        "speed_bonus": speed,
        "battery_hours": battery_hours,
        "overheat_chance_per_hour": max(0.0, 0.20 - stabilization_reduction),
    }


def _farm_cycle_seconds(speed_bonus: float):
    return max(int(FARM_BASE_CYCLE_SECONDS * (1.0 - speed_bonus)), 1800)


def _farm_capacity_seconds(extra_hours: int):
    return FARM_BASE_CAP_SECONDS + int(extra_hours) * 3600


def _farm_process_overheat(cell: dict, support: dict, now_ts: int):
    if cell["module_type"] != "generator":
        return False
    if cell["status"] != "ok":
        return False

    last_collect = int(cell["last_collect_ts"] or now_ts)
    cap_seconds = _farm_capacity_seconds(support["battery_hours"])
    run_end = min(now_ts, last_collect + cap_seconds)
    last_check = int(cell["last_event_check_ts"] or last_collect)
    check_from = max(last_collect, min(last_check, run_end))
    if run_end <= check_from:
        return False

    full_hours = int((run_end - check_from) // 3600)
    if full_hours <= 0:
        return False

    chance = support["overheat_chance_per_hour"]
    for i in range(full_hours):
        if random.random() < chance:
            cell["status"] = "overheat"
            cell["status_since"] = check_from + (i + 1) * 3600
            cell["last_event_check_ts"] = cell["status_since"]
            return True

    cell["last_event_check_ts"] = check_from + full_hours * 3600
    return True


def _farm_pending_for_cell(cell: dict, support: dict, now_ts: int):
    if cell["module_type"] != "generator":
        return {
            "pending_coins": 0,
            "pending_xp": 0,
            "pending_cycles": 0,
            "cycle_seconds": 0,
            "capacity_seconds": 0,
            "stalled": False,
        }

    cycle_seconds = _farm_cycle_seconds(support["speed_bonus"])
    cap_seconds = _farm_capacity_seconds(support["battery_hours"])
    last_collect = int(cell["last_collect_ts"] or now_ts)
    run_end = min(now_ts, last_collect + cap_seconds)
    elapsed = max(0, run_end - last_collect)
    total_cycles = elapsed // cycle_seconds
    if total_cycles <= 0:
        return {
            "pending_coins": 0,
            "pending_xp": 0,
            "pending_cycles": 0,
            "cycle_seconds": cycle_seconds,
            "capacity_seconds": cap_seconds,
            "stalled": now_ts >= (last_collect + cap_seconds),
        }

    base_coins, base_xp = _farm_generator_output(int(cell["level"] or 1), (cell.get("spec") or "").strip())
    if cell["status"] != "overheat":
        pending_coins = int(total_cycles * base_coins)
        pending_xp = int(total_cycles * base_xp)
    else:
        status_since = int(cell.get("status_since") or last_collect)
        normal_end = min(run_end, max(last_collect, status_since))
        normal_cycles = max(0, min(total_cycles, (normal_end - last_collect) // cycle_seconds))
        heat_cycles = total_cycles - normal_cycles
        pending_coins = int(normal_cycles * base_coins + heat_cycles * base_coins * 0.5)
        pending_xp = int(normal_cycles * base_xp + heat_cycles * base_xp * 0.5)

    return {
        "pending_coins": pending_coins,
        "pending_xp": pending_xp,
        "pending_cycles": int(total_cycles),
        "cycle_seconds": cycle_seconds,
        "capacity_seconds": cap_seconds,
        "stalled": now_ts >= (last_collect + cap_seconds),
    }


async def _farm_get_or_create_player(user_id: int):
    now_ts = _farm_now()
    async with aiosqlite.connect(DB_NAME) as db:
        c = await db.execute(
            "SELECT user_id, coins, opened_cells, moving_from, created_at, updated_at FROM farm_players WHERE user_id = ?",
            (user_id,),
        )
        row = await c.fetchone()
        if row:
            return {
                "user_id": int(row[0]),
                "coins": int(row[1] or 0),
                "opened_cells": _farm_parse_opened_cells(row[2]),
                "moving_from": row[3],
                "created_at": int(row[4] or 0),
                "updated_at": int(row[5] or 0),
            }
        opened = [FARM_CENTER_IDX]
        await db.execute(
            "INSERT INTO farm_players (user_id, coins, opened_cells, moving_from, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?)",
            (user_id, FARM_STARTER_COINS, _farm_opened_cells_payload(opened), now_ts, now_ts),
        )
        await db.commit()
    await _mark_dirty("farm_players")
    return {
        "user_id": int(user_id),
        "coins": FARM_STARTER_COINS,
        "opened_cells": [FARM_CENTER_IDX],
        "moving_from": None,
        "created_at": now_ts,
        "updated_at": now_ts,
    }


async def _farm_load_cells(user_id: int):
    cells = {}
    async with aiosqlite.connect(DB_NAME) as db:
        c = await db.execute(
            "SELECT user_id, cell_idx, module_type, level, spec, status, status_since, last_collect_ts, last_event_check_ts FROM farm_cells WHERE user_id = ?",
            (user_id,),
        )
        rows = await c.fetchall()
    for r in rows:
        idx = int(r[1])
        cells[idx] = {
            "user_id": int(r[0]),
            "cell_idx": idx,
            "module_type": r[2],
            "level": int(r[3] or 1),
            "spec": r[4] or "",
            "status": r[5] or "ok",
            "status_since": int(r[6] or 0),
            "last_collect_ts": int(r[7] or 0),
            "last_event_check_ts": int(r[8] or 0),
        }
    return cells


async def farm_get_state(user_id: int):
    player = await _farm_get_or_create_player(user_id)
    cells = await _farm_load_cells(user_id)
    now_ts = _farm_now()

    changed_cells = []
    for idx, cell in cells.items():
        if cell["module_type"] != "generator":
            continue
        support = _farm_get_support_bonuses(cells, idx)
        changed = _farm_process_overheat(cell, support, now_ts)
        if changed:
            changed_cells.append(cell)

    if changed_cells:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.executemany(
                "UPDATE farm_cells SET status = ?, status_since = ?, last_event_check_ts = ? WHERE user_id = ? AND cell_idx = ?",
                [
                    (
                        c["status"],
                        int(c.get("status_since") or 0),
                        int(c.get("last_event_check_ts") or 0),
                        int(c["user_id"]),
                        int(c["cell_idx"]),
                    )
                    for c in changed_cells
                ],
            )
            await db.commit()
        await _mark_dirty("farm_cells")

    opened_set = set(player["opened_cells"])
    moving_from = player["moving_from"]

    cell_views = []
    for idx in range(FARM_CELLS_TOTAL):
        locked = idx not in opened_set
        cell = cells.get(idx)
        if locked:
            cell_views.append({"cell_idx": idx, "state": "locked"})
            continue
        if not cell:
            cell_views.append({"cell_idx": idx, "state": "empty"})
            continue
        support = _farm_get_support_bonuses(cells, idx)
        pending = _farm_pending_for_cell(cell, support, now_ts)
        cell_views.append(
            {
                "cell_idx": idx,
                "state": "module",
                "module_type": cell["module_type"],
                "level": int(cell["level"] or 1),
                "spec": cell.get("spec") or "",
                "status": cell.get("status") or "ok",
                "status_since": int(cell.get("status_since") or 0),
                "last_collect_ts": int(cell.get("last_collect_ts") or 0),
                "pending_coins": int(pending["pending_coins"]),
                "pending_xp": int(pending["pending_xp"]),
                "pending_cycles": int(pending["pending_cycles"]),
                "cycle_seconds": int(pending["cycle_seconds"]),
                "capacity_seconds": int(pending["capacity_seconds"]),
                "stalled": bool(pending["stalled"]),
                "support": support,
            }
        )

    return {
        "ok": True,
        "coins": int(player["coins"]),
        "opened_cells": sorted(list(opened_set)),
        "opened_count": len(opened_set),
        "next_unlock_cost": _farm_next_unlock_cost(len(opened_set)),
        "moving_from": moving_from,
        "cells": cell_views,
        "now_ts": now_ts,
    }


async def farm_adjust_coins(user_id: int, delta: int):
    player = await _farm_get_or_create_player(user_id)
    now_ts = _farm_now()
    new_coins = int(player["coins"]) + int(delta)
    if new_coins < 0:
        new_coins = 0
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE farm_players SET coins = ?, updated_at = ? WHERE user_id = ?",
            (new_coins, now_ts, user_id),
        )
        await db.commit()
    await _mark_dirty("farm_players")
    return new_coins


def _farm_module_sell_refund(module_type: str, level: int, spec: str):
    spent = 0
    if module_type == "generator":
        spent = FARM_GEN_BUY_COST
        if level >= 2:
            spent += FARM_GEN_UPGRADE_COSTS[2]
        if level >= 3:
            spent += FARM_GEN_UPGRADE_COSTS[3]
    else:
        table = FARM_SUPPORT_COSTS.get(module_type, {})
        for lvl in range(1, int(level) + 1):
            spent += int(table.get(lvl, 0))
    return int(spent * 0.5)


async def farm_unlock_cell(user_id: int, cell_idx: int):
    if cell_idx < 0 or cell_idx >= FARM_CELLS_TOTAL:
        return {"ok": False, "message": "Некорректная ячейка."}
    player = await _farm_get_or_create_player(user_id)
    opened = set(player["opened_cells"])
    if cell_idx in opened:
        return {"ok": False, "message": "Ячейка уже открыта."}
    cost = _farm_next_unlock_cost(len(opened))
    if cost is None:
        return {"ok": False, "message": "Все ячейки уже открыты."}
    if int(player["coins"]) < cost:
        return {"ok": False, "message": f"Не хватает монет. Нужно {cost}."}
    opened.add(cell_idx)
    now_ts = _farm_now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE farm_players SET coins = ?, opened_cells = ?, updated_at = ? WHERE user_id = ?",
            (int(player["coins"]) - int(cost), _farm_opened_cells_payload(opened), now_ts, user_id),
        )
        await db.commit()
    await _mark_dirty("farm_players")
    return {"ok": True, "message": f"Ячейка открыта за {cost} монет."}


async def farm_buy_module(user_id: int, cell_idx: int, module_type: str):
    if module_type not in ["generator", "speed", "battery", "stabilizer"]:
        return {"ok": False, "message": "Неизвестный модуль."}
    player = await _farm_get_or_create_player(user_id)
    opened = set(player["opened_cells"])
    if cell_idx not in opened:
        return {"ok": False, "message": "Сначала откройте эту ячейку."}
    cells = await _farm_load_cells(user_id)
    if cell_idx in cells:
        return {"ok": False, "message": "Ячейка уже занята."}

    cost = FARM_GEN_BUY_COST if module_type == "generator" else FARM_SUPPORT_COSTS[module_type][1]
    if int(player["coins"]) < int(cost):
        return {"ok": False, "message": f"Не хватает монет. Нужно {cost}."}

    now_ts = _farm_now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE farm_players SET coins = ?, updated_at = ? WHERE user_id = ?",
            (int(player["coins"]) - int(cost), now_ts, user_id),
        )
        await db.execute(
            "INSERT INTO farm_cells (user_id, cell_idx, module_type, level, spec, status, status_since, last_collect_ts, last_event_check_ts) VALUES (?, ?, ?, 1, '', 'ok', 0, ?, ?)",
            (user_id, cell_idx, module_type, now_ts, now_ts),
        )
        await db.commit()
    await _mark_dirty("farm_players", "farm_cells")
    return {"ok": True, "message": f"Установлен модуль: {module_type}."}


async def farm_set_move_source(user_id: int, cell_idx):
    player = await _farm_get_or_create_player(user_id)
    if cell_idx is not None:
        cells = await _farm_load_cells(user_id)
        if cell_idx not in cells:
            return {"ok": False, "message": "В выбранной ячейке нет модуля."}
    now_ts = _farm_now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE farm_players SET moving_from = ?, updated_at = ? WHERE user_id = ?",
            (cell_idx, now_ts, user_id),
        )
        await db.commit()
    await _mark_dirty("farm_players")
    if cell_idx is None:
        return {"ok": True, "message": "Перемещение отменено."}
    return {"ok": True, "message": "Выберите пустую открытую ячейку для перемещения."}


async def farm_move_module(user_id: int, target_idx: int):
    player = await _farm_get_or_create_player(user_id)
    source_idx = player["moving_from"]
    if source_idx is None:
        return {"ok": False, "message": "Сначала выберите модуль для перемещения."}

    opened = set(player["opened_cells"])
    if target_idx not in opened:
        return {"ok": False, "message": "Целевая ячейка закрыта."}
    cells = await _farm_load_cells(user_id)
    if source_idx not in cells:
        return {"ok": False, "message": "Исходный модуль не найден."}
    if target_idx in cells:
        return {"ok": False, "message": "Целевая ячейка занята."}
    if source_idx == target_idx:
        return {"ok": False, "message": "Это та же самая ячейка."}

    now_ts = _farm_now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE farm_cells SET cell_idx = ? WHERE user_id = ? AND cell_idx = ?",
            (target_idx, user_id, source_idx),
        )
        await db.execute(
            "UPDATE farm_players SET moving_from = NULL, updated_at = ? WHERE user_id = ?",
            (now_ts, user_id),
        )
        await db.commit()
    await _mark_dirty("farm_players", "farm_cells")
    return {"ok": True, "message": "Модуль перемещен."}


async def farm_collect_generator(user_id: int, cell_idx: int):
    state = await farm_get_state(user_id)
    cell = None
    for c in state["cells"]:
        if c["cell_idx"] == cell_idx:
            cell = c
            break
    if not cell or cell.get("state") != "module" or cell.get("module_type") != "generator":
        return {"ok": False, "message": "В ячейке нет генератора."}

    coins_gain = int(cell.get("pending_coins") or 0)
    xp_gain = int(cell.get("pending_xp") or 0)
    if coins_gain <= 0 and xp_gain <= 0:
        return {"ok": False, "message": "Пока нечего собирать."}

    now_ts = _farm_now()
    player = await _farm_get_or_create_player(user_id)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE farm_players SET coins = ?, updated_at = ? WHERE user_id = ?",
            (int(player["coins"]) + coins_gain, now_ts, user_id),
        )
        await db.execute(
            "UPDATE farm_cells SET last_collect_ts = ?, last_event_check_ts = ? WHERE user_id = ? AND cell_idx = ?",
            (now_ts, now_ts, user_id, cell_idx),
        )
        await db.commit()
    if xp_gain > 0:
        await update_xp(user_id, xp_gain)
    await _mark_dirty("farm_players", "farm_cells")
    return {"ok": True, "message": f"Собрано: +{coins_gain} монет, +{xp_gain} XP.", "coins_gain": coins_gain, "xp_gain": xp_gain}


async def farm_upgrade_module(user_id: int, cell_idx: int, spec: str = ""):
    player = await _farm_get_or_create_player(user_id)
    cells = await _farm_load_cells(user_id)
    cell = cells.get(cell_idx)
    if not cell:
        return {"ok": False, "message": "Модуль не найден."}

    m_type = cell["module_type"]
    lvl = int(cell["level"] or 1)
    now_ts = _farm_now()

    if m_type == "generator":
        if lvl == 1:
            need = FARM_GEN_UPGRADE_COSTS[2]
            if player["coins"] < need:
                return {"ok": False, "message": f"Не хватает монет. Нужно {need}."}
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE farm_players SET coins = ?, updated_at = ? WHERE user_id = ?", (int(player["coins"]) - need, now_ts, user_id))
                await db.execute("UPDATE farm_cells SET level = 2 WHERE user_id = ? AND cell_idx = ?", (user_id, cell_idx))
                await db.commit()
            await _mark_dirty("farm_players", "farm_cells")
            return {"ok": True, "message": "Генератор улучшен до уровня 2."}
        if lvl == 2:
            if spec not in ["xp", "coin"]:
                return {"ok": False, "message": "Выберите специализацию: xp или coin."}
            need = FARM_GEN_UPGRADE_COSTS[3]
            if player["coins"] < need:
                return {"ok": False, "message": f"Не хватает монет. Нужно {need}."}
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE farm_players SET coins = ?, updated_at = ? WHERE user_id = ?", (int(player["coins"]) - need, now_ts, user_id))
                await db.execute("UPDATE farm_cells SET level = 3, spec = ? WHERE user_id = ? AND cell_idx = ?", (spec, user_id, cell_idx))
                await db.commit()
            await _mark_dirty("farm_players", "farm_cells")
            title = "Синтезатор Опыта" if spec == "xp" else "Монетный Двор"
            return {"ok": True, "message": f"Генератор эволюционировал в «{title}»."}
        return {"ok": False, "message": "Это максимальный уровень генератора."}

    if m_type not in FARM_SUPPORT_COSTS:
        return {"ok": False, "message": "Модуль нельзя улучшить."}
    if lvl >= 3:
        return {"ok": False, "message": "Это максимальный уровень."}
    target_lvl = lvl + 1
    need = FARM_SUPPORT_COSTS[m_type][target_lvl]
    if int(player["coins"]) < int(need):
        return {"ok": False, "message": f"Не хватает монет. Нужно {need}."}
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE farm_players SET coins = ?, updated_at = ? WHERE user_id = ?", (int(player["coins"]) - int(need), now_ts, user_id))
        await db.execute("UPDATE farm_cells SET level = ? WHERE user_id = ? AND cell_idx = ?", (target_lvl, user_id, cell_idx))
        await db.commit()
    await _mark_dirty("farm_players", "farm_cells")
    return {"ok": True, "message": f"Модуль улучшен до уровня {target_lvl}."}


async def farm_repair_module(user_id: int, cell_idx: int):
    cells = await _farm_load_cells(user_id)
    cell = cells.get(cell_idx)
    if not cell:
        return {"ok": False, "message": "Модуль не найден."}
    if cell["module_type"] != "generator":
        return {"ok": False, "message": "Ремонт нужен только генераторам."}
    if cell.get("status") != "overheat":
        return {"ok": False, "message": "Модуль не требует обслуживания."}
    now_ts = _farm_now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE farm_cells SET status = 'ok', status_since = 0, last_event_check_ts = ? WHERE user_id = ? AND cell_idx = ?",
            (now_ts, user_id, cell_idx),
        )
        await db.commit()
    await _mark_dirty("farm_cells")
    return {"ok": True, "message": "Модуль обслужен, перегрев снят."}


async def farm_sell_module(user_id: int, cell_idx: int):
    player = await _farm_get_or_create_player(user_id)
    cells = await _farm_load_cells(user_id)
    cell = cells.get(cell_idx)
    if not cell:
        return {"ok": False, "message": "Модуль не найден."}
    refund = _farm_module_sell_refund(cell["module_type"], int(cell["level"] or 1), (cell.get("spec") or ""))
    now_ts = _farm_now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM farm_cells WHERE user_id = ? AND cell_idx = ?", (user_id, cell_idx))
        if player.get("moving_from") == cell_idx:
            await db.execute(
                "UPDATE farm_players SET coins = ?, moving_from = NULL, updated_at = ? WHERE user_id = ?",
                (int(player["coins"]) + refund, now_ts, user_id),
            )
        else:
            await db.execute(
                "UPDATE farm_players SET coins = ?, updated_at = ? WHERE user_id = ?",
                (int(player["coins"]) + refund, now_ts, user_id),
            )
        await db.commit()
    await _mark_dirty("farm_players", "farm_cells")
    return {"ok": True, "message": f"Модуль продан. Возврат: {refund} монет.", "refund": refund}

# --- Лидеры и состав персонала ---

async def get_top_users(limit=10):
    """Возвращает топ пользователей по уровню и XP."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('''
            SELECT full_name, level, xp, user_id 
            FROM users 
            ORDER BY level DESC, xp DESC 
            LIMIT ?
        ''', (limit,))
        return await cursor.fetchall()

async def get_user_rank(user_id):
    """Возвращает место, уровень и XP пользователя."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Получаем данные пользователя
        cursor = await db.execute('SELECT level, xp FROM users WHERE user_id = ?', (user_id,))
        user_data = await cursor.fetchone()
        if not user_data:
            return None
        
        u_lvl, u_xp = user_data
        
        # Считаем, сколько людей выше по уровню или по XP на том же уровне
        cursor = await db.execute('''
            SELECT COUNT(*) FROM users 
            WHERE level > ? OR (level = ? AND xp > ?)
        ''', (u_lvl, u_lvl, u_xp))
        count = await cursor.fetchone()
        rank = count[0] + 1
        
        return rank, u_lvl, u_xp

async def get_all_staff():
    """Возвращает всех сотрудников (mod_level > 0), отсортированных по рангу."""
    async with aiosqlite.connect(DB_NAME) as db:
        # user_id нужен для ссылки на профиль в интерфейсе
        cursor = await db.execute('''
            SELECT full_name, mod_level, username, user_id
            FROM users 
            WHERE mod_level > 0 
            ORDER BY mod_level DESC
        ''', ())
        return await cursor.fetchall()

