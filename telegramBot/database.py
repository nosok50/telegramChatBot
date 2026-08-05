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
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
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
    1: 10000,
    2: 20000,
    3: 70000,
    4: 200000,
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

SYNCABLE_TABLES = [
    "users", "rep_history", "whitelist", "badwords", "warn_reasons",
    "farm_players", "farm_cells", "month_scores", "month_results",
    "duel_history", "active_duels", "factory_orders",
    "factory_order_participants", "factory_order_messages", "factory_order_votes",
    "chat_level_tags", "data_migrations",
]

MONTH_PRIZES = (10000, 5000, 2500)
_month_finalize_lock = asyncio.Lock()
_month_processor_task = None
EXTRA_SYNC_COLUMNS = {
    "month_scores": ("month_key", "user_id", "xp_earned"),
    "month_results": ("month_key", "winner_id", "second_id", "third_id", "finalized_at"),
    "duel_history": ("id", "chat_id", "player1_id", "player2_id", "bet", "result", "commission_rate", "created_at", "resolved_at"),
    "active_duels": ("chat_id", "message_id", "player1_id", "player2_id", "player1_name", "player2_name", "bet", "state", "player1_choice", "player2_choice", "escrowed", "created_at"),
    "factory_orders": ("id", "chat_id", "owner_id", "order_type", "size", "coin_cost", "xp_bank", "topic", "status", "message_id", "vote_message_id", "created_at", "stage_ends_at", "metadata", "distributed_xp"),
    "factory_order_participants": ("order_id", "user_id", "display_name", "submission_message_id", "replies_received", "joined_at"),
    "factory_order_messages": ("order_id", "message_id", "author_id", "parent_author_id"),
    "factory_order_votes": ("order_id", "voter_id", "candidate_id"),
    "chat_level_tags": ("chat_id", "user_id", "applied_level", "retry_after", "last_error"),
    "data_migrations": ("migration_key", "applied_at"),
}

LEGACY_LEVEL_CAPS = {1: 500, 2: 2000, 3: 8000, 4: 25000}
LEGACY_LEVEL_FIVE_TOTAL = sum(LEGACY_LEVEL_CAPS.values())
LEGACY_LEVEL_FIVE_MIGRATION = "legacy_level_five_to_2026_caps_v1"


def _normalize_neon_dsn(raw_dsn: str) -> str:
    if not raw_dsn:
        return ""
    parts = urlsplit(raw_dsn)
    if not parts.query:
        return raw_dsn
    supported = {"sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl", "sslpassword"}
    query = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k.lower() in supported]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


NEON_DSN = _normalize_neon_dsn(os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL") or "")
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
_neon_pool_lock = asyncio.Lock()
_sync_execution_lock = asyncio.Lock()
_retry_wakeup_task = None
_bootstrap_complete = False
_neon_had_failure = False


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


def _create_sqlite_snapshot() -> str:
    """Create a consistent SQLite copy that can safely be sent while the bot writes."""
    snapshot_dir = os.path.dirname(os.path.abspath(DB_NAME)) or "."
    fd, snapshot_path = tempfile.mkstemp(prefix="bot_backup_", suffix=".db", dir=snapshot_dir)
    os.close(fd)
    try:
        with closing(sqlite3.connect(DB_NAME, timeout=30)) as source:
            with closing(sqlite3.connect(snapshot_path, timeout=30)) as target:
                source.backup(target)
                target.commit()
        return snapshot_path
    except Exception:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
        raise


async def _send_db_alert(event_key: str, text: str, attach_backup: bool = True):
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
            async with session.post(url, json=payload, timeout=10) as response:
                if response.status >= 400:
                    details = (await response.text())[:300]
                    raise RuntimeError(f"Telegram sendMessage HTTP {response.status}: {details}")
            if attach_backup:
                await _send_db_file_to_owner(session, BOT_TOKEN, user_id, event_key)
        _last_alert_key = event_key
        _last_alert_at = now
    except Exception as e:
        logging.error("DB alert delivery failed (%s): %s: %s", event_key, type(e).__name__, e)


async def _send_db_file_to_owner(session: aiohttp.ClientSession, token: str, user_id: int, event_key: str):
    if not os.path.exists(DB_NAME):
        return
    snapshot_path = None
    try:
        snapshot_path = await asyncio.to_thread(_create_sqlite_snapshot)
        file_size = os.path.getsize(snapshot_path)
        # Keep a hard cap to avoid unexpected huge uploads on free hosting.
        if file_size > 45 * 1024 * 1024:
            raise RuntimeError(f"SQLite backup is too large: {file_size} bytes")

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(user_id))
        form.add_field("caption", f"[DB] Резервная копия ({event_key})")
        db_file = open(snapshot_path, "rb")
        form.add_field(
            "document",
            db_file,
            filename=os.path.basename(DB_NAME),
            content_type="application/octet-stream",
        )
        try:
            async with session.post(url, data=form, timeout=30) as response:
                if response.status >= 400:
                    details = (await response.text())[:300]
                    raise RuntimeError(f"Telegram sendDocument HTTP {response.status}: {details}")
        finally:
            db_file.close()
    finally:
        if snapshot_path:
            try:
                os.remove(snapshot_path)
            except OSError:
                pass


async def _reset_neon_pool():
    global _neon_pool
    async with _neon_pool_lock:
        pool = _neon_pool
        _neon_pool = None
    if not pool:
        return
    try:
        await asyncio.wait_for(pool.close(), timeout=5)
    except Exception:
        try:
            pool.terminate()
        except Exception:
            pass


async def _init_neon_pool() -> bool:
    global _neon_pool, _neon_had_failure
    if not NEON_DSN:
        error = "DATABASE_URL или NEON_DATABASE_URL не задана"
        _neon_had_failure = True
        _set_last_neon_error(error)
        await _send_db_alert(
            "neon_not_configured",
            f"Neon не настроен: {error}. Синхронизация невозможна, пока строка подключения не добавлена в окружение.",
        )
        return False

    error = None
    async with _neon_pool_lock:
        if _neon_pool:
            return True
        try:
            _neon_pool = await asyncpg.create_pool(
                dsn=NEON_DSN,
                min_size=1,
                max_size=3,
                timeout=15,
                command_timeout=30,
            )
            _set_last_neon_error("")
            return True
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            _set_last_neon_error(error)
            _neon_had_failure = True
            _neon_pool = None

    await _send_db_alert(
        "neon_down",
        f"Не удалось подключиться к Neon. Бот продолжает работать локально.\nОшибка: {error}",
    )
    return False


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
            CREATE TABLE IF NOT EXISTS month_scores (month_key TEXT, user_id BIGINT, xp_earned BIGINT DEFAULT 0, PRIMARY KEY(month_key,user_id));
            CREATE TABLE IF NOT EXISTS month_results (month_key TEXT PRIMARY KEY, winner_id BIGINT, second_id BIGINT, third_id BIGINT, finalized_at BIGINT);
            CREATE TABLE IF NOT EXISTS duel_history (id BIGINT PRIMARY KEY, chat_id BIGINT, player1_id BIGINT, player2_id BIGINT, bet BIGINT, result TEXT, commission_rate INTEGER, created_at BIGINT, resolved_at BIGINT);
            CREATE TABLE IF NOT EXISTS active_duels (chat_id BIGINT PRIMARY KEY, message_id BIGINT, player1_id BIGINT, player2_id BIGINT, player1_name TEXT, player2_name TEXT, bet BIGINT, state TEXT, player1_choice TEXT, player2_choice TEXT, escrowed INTEGER, created_at BIGINT);
            CREATE TABLE IF NOT EXISTS factory_orders (id BIGINT PRIMARY KEY, chat_id BIGINT, owner_id BIGINT, order_type TEXT, size TEXT, coin_cost BIGINT, xp_bank BIGINT, topic TEXT, status TEXT, message_id BIGINT, vote_message_id BIGINT, created_at BIGINT, stage_ends_at BIGINT, metadata TEXT, distributed_xp BIGINT DEFAULT 0);
            CREATE TABLE IF NOT EXISTS factory_order_participants (order_id BIGINT, user_id BIGINT, display_name TEXT, submission_message_id BIGINT, replies_received INTEGER, joined_at BIGINT, PRIMARY KEY(order_id,user_id));
            CREATE TABLE IF NOT EXISTS factory_order_messages (order_id BIGINT, message_id BIGINT, author_id BIGINT, parent_author_id BIGINT, PRIMARY KEY(order_id,message_id));
            CREATE TABLE IF NOT EXISTS factory_order_votes (order_id BIGINT, voter_id BIGINT, candidate_id BIGINT, PRIMARY KEY(order_id,voter_id));
            CREATE TABLE IF NOT EXISTS chat_level_tags (
                chat_id BIGINT,
                user_id BIGINT,
                applied_level INTEGER DEFAULT 0,
                retry_after BIGINT DEFAULT 0,
                last_error TEXT DEFAULT '',
                PRIMARY KEY(chat_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS data_migrations (
                migration_key TEXT PRIMARY KEY,
                applied_at BIGINT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
            CREATE INDEX IF NOT EXISTS idx_users_level_xp ON users (level DESC, xp DESC);
            CREATE INDEX IF NOT EXISTS idx_farm_cells_user ON farm_cells (user_id);
            CREATE INDEX IF NOT EXISTS idx_chat_level_tags_user ON chat_level_tags (user_id);
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
        await conn.execute("ALTER TABLE factory_orders ADD COLUMN IF NOT EXISTS distributed_xp BIGINT DEFAULT 0")


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
        elif table in EXTRA_SYNC_COLUMNS:
            c = await db.execute(f"SELECT {','.join(EXTRA_SYNC_COLUMNS[table])} FROM {table}")
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
            elif table in EXTRA_SYNC_COLUMNS:
                cols = EXTRA_SYNC_COLUMNS[table]
                placeholders = ",".join(f"${i}" for i in range(1, len(cols) + 1))
                await conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", rows)


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
        elif table in EXTRA_SYNC_COLUMNS:
            rows = await conn.fetch(f"SELECT {','.join(EXTRA_SYNC_COLUMNS[table])} FROM {table}")
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
        elif table in EXTRA_SYNC_COLUMNS and rows:
            cols = EXTRA_SYNC_COLUMNS[table]
            placeholders = ",".join("?" for _ in cols)
            await db.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", rows)
        await db.commit()


async def _sync_dirty_tables_once():
    global _neon_had_failure
    if not _neon_pool:
        return {"ok": False, "synced": [], "failed_table": None, "error": "Neon не подключен"}
    dirty = await _load_dirty_tables()
    if not dirty:
        return {"ok": True, "synced": [], "failed_table": None, "error": ""}
    synced = []
    failed_table = None
    failure_error = ""
    for table in sorted(dirty):
        try:
            await _replace_neon_table_from_local(table)
            synced.append(table)
        except Exception as e:
            failed_table = table
            failure_error = f"{type(e).__name__}: {e}"
            _neon_had_failure = True
            _set_last_neon_error(f"Ошибка синхронизации таблицы {table}: {failure_error}")
            await _reset_neon_pool()
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


async def _connect_neon_ready() -> bool:
    global _neon_had_failure
    if not await _init_neon_pool():
        return False
    try:
        await _ensure_neon_schema()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        _neon_had_failure = True
        _set_last_neon_error(f"Ошибка подготовки схемы Neon: {error}")
        await _reset_neon_pool()
        await _send_db_alert(
            "neon_schema_failed",
            f"Подключение к Neon открылось, но подготовка схемы завершилась ошибкой.\nОшибка: {error}",
        )
        return False
    return True


async def _wake_sync_after(delay_seconds: float):
    global _retry_wakeup_task
    try:
        await asyncio.sleep(max(1, delay_seconds))
        _sync_wakeup_event.set()
    finally:
        _retry_wakeup_task = None


def _schedule_retry_wakeup(delay_seconds: float = None):
    global _retry_wakeup_task
    delay = SYNC_RETRY_INTERVAL_SECONDS if delay_seconds is None else delay_seconds
    if _retry_wakeup_task and not _retry_wakeup_task.done():
        return
    _retry_wakeup_task = asyncio.create_task(_wake_sync_after(delay))


async def _bootstrap_from_neon_if_needed() -> bool:
    """
    Treat Neon as the authoritative copy once per process start.

    This protects a newly created or repository-bundled SQLite file on Render
    from overwriting a newer Neon backup after an instance restart.
    """
    global _bootstrap_complete, _neon_had_failure
    if _bootstrap_complete:
        return True
    if not NEON_DSN or os.getenv("DB_STARTUP_BOOTSTRAP_NEON", "1") != "1":
        _bootstrap_complete = True
        return True
    if not await _connect_neon_ready():
        return False

    recovered_after_failure = _neon_had_failure
    try:
        local_users = await _count_local_users()
        neon_users = await _count_neon_users()

        if neon_users > 0:
            for table in SYNCABLE_TABLES:
                await _replace_local_table_from_neon(table)
            await _clear_dirty(*SYNCABLE_TABLES)
            await _set_meta_int("local_change_count", 0)
            await _set_meta_int("sync_activity_count", 0)
            await _set_meta_int("last_sync_at", int(_now_ts()))
            _bootstrap_complete = True
            _neon_had_failure = False
            _set_last_neon_error("")
            await _send_db_alert(
                "local_restore",
                f"Локальная БД восстановлена из Neon при запуске. Пользователей в backup: {neon_users}.",
            )
            return True

        if local_users > 0:
            await _mark_dirty(*SYNCABLE_TABLES)
            result = await _sync_dirty_tables_once()
            if not result["ok"]:
                return False
            await _set_meta_int("sync_activity_count", 0)
            _bootstrap_complete = True
            _neon_had_failure = False
            _set_last_neon_error("")
            await _send_db_alert(
                "neon_seed",
                f"Neon был пустой и заполнен из локальной БД при запуске. Пользователей: {local_users}.",
            )
            return True

        _bootstrap_complete = True
        _neon_had_failure = False
        _set_last_neon_error("")
        if recovered_after_failure:
            await _send_db_alert("neon_back", "Соединение с Neon восстановлено.", attach_backup=False)
        return True
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        _neon_had_failure = True
        _set_last_neon_error(f"Ошибка startup-восстановления Neon: {error}")
        await _reset_neon_pool()
        await _send_db_alert(
            "neon_bootstrap_failed",
            f"Не удалось восстановить локальную БД из Neon при запуске.\nОшибка: {error}",
        )
        return False


async def _sync_loop():
    global _next_sync_attempt_at, _last_sync_check_at, _neon_had_failure
    while True:
        # Strict event-driven mode: check sync only after local writes.
        await _sync_wakeup_event.wait()
        _sync_wakeup_event.clear()

        now = _now_ts()
        if not _bootstrap_complete:
            async with _sync_execution_lock:
                bootstrapped = await _bootstrap_from_neon_if_needed()
            if not bootstrapped:
                _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS
                _schedule_retry_wakeup()
                continue
            # Neon is authoritative. Only now is it safe to migrate restored data.
            await migrate_legacy_level_fives()

        if SYNC_CHECK_INTERVAL_SECONDS > 0 and _last_sync_check_at and (now - _last_sync_check_at) < SYNC_CHECK_INTERVAL_SECONDS:
            continue
        _last_sync_check_at = now

        dirty = await _load_dirty_tables()
        if not dirty:
            continue

        if now < _next_sync_attempt_at:
            _schedule_retry_wakeup(_next_sync_attempt_at - now)
            continue

        activity_messages = await _get_meta_int("sync_activity_count", 0)
        last_sync_at = await _get_meta_int("last_sync_at", 0)
        min_interval_ok = (last_sync_at == 0) or (now - last_sync_at >= SYNC_MIN_INTERVAL_SECONDS)

        if not min_interval_ok:
            continue
        if activity_messages < SYNC_MIN_ACTIVITY_MESSAGES:
            continue

        try:
            async with _sync_execution_lock:
                if not await _connect_neon_ready():
                    _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS
                    _schedule_retry_wakeup()
                    continue

                result = await _sync_dirty_tables_once()
                if result["ok"]:
                    _neon_had_failure = False
                    await _set_meta_int("sync_activity_count", 0)
                    _next_sync_attempt_at = now + SYNC_MIN_INTERVAL_SECONDS
                    if result["synced"]:
                        synced_list = ", ".join(result["synced"])
                        await _send_db_alert(
                            "sync_success",
                            f"Neon успешно синхронизирован. Таблицы: {synced_list}.",
                        )
                else:
                    _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS
                    _schedule_retry_wakeup()
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            _set_last_neon_error(f"Необработанная ошибка sync-цикла: {error}")
            await _reset_neon_pool()
            await _send_db_alert(
                "sync_loop_failed",
                f"Необработанная ошибка синхронизации Neon.\nОшибка: {error}",
            )
            _next_sync_attempt_at = now + SYNC_RETRY_INTERVAL_SECONDS
            _schedule_retry_wakeup()


async def _startup_bootstrap():
    global _startup_sync_done, _sync_task, _next_sync_attempt_at, _bootstrap_complete
    if _startup_sync_done:
        return
    _startup_sync_done = True

    if not _sync_task:
        _sync_task = asyncio.create_task(_sync_loop())

    if not NEON_DSN or os.getenv("DB_STARTUP_BOOTSTRAP_NEON", "1") != "1":
        _bootstrap_complete = True
        return

    async with _sync_execution_lock:
        bootstrapped = await _bootstrap_from_neon_if_needed()
    if not bootstrapped:
        _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
        _schedule_retry_wakeup()


def _level_state_from_total_xp(total_xp: int):
    total = max(0, int(total_xp))
    if total >= 300000:
        return 5, total - 300000
    if total >= 100000:
        return 4, total - 100000
    if total >= 30000:
        return 3, total - 30000
    if total >= 10000:
        return 2, total - 10000
    return 1, total


def _queue_level_tag_refresh(user_id: int):
    try:
        from level_tags import queue_level_tag_refresh
        queue_level_tag_refresh(int(user_id))
    except Exception:
        pass


async def migrate_legacy_level_fives():
    """
    One-time conversion of grandfathered level-5 users.

    The old model required 35,500 XP to enter level 5. We reconstruct that
    earned total, then express it using the current cumulative thresholds.
    """
    changed_users = []
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        applied = await (await db.execute(
            "SELECT 1 FROM data_migrations WHERE migration_key = ?",
            (LEGACY_LEVEL_FIVE_MIGRATION,),
        )).fetchone()
        if applied:
            await db.rollback()
            return 0

        rows = await (await db.execute(
            "SELECT user_id, xp FROM users WHERE level = 5"
        )).fetchall()
        for user_id, residual_xp in rows:
            total_xp = LEGACY_LEVEL_FIVE_TOTAL + max(0, int(residual_xp or 0))
            new_level, new_xp = _level_state_from_total_xp(total_xp)
            await db.execute(
                "UPDATE users SET level = ?, xp = ? WHERE user_id = ?",
                (new_level, new_xp, user_id),
            )
            changed_users.append(int(user_id))

        await db.execute(
            "INSERT INTO data_migrations(migration_key, applied_at) VALUES (?, ?)",
            (LEGACY_LEVEL_FIVE_MIGRATION, int(time.time())),
        )
        await db.commit()

    await _mark_dirty("data_migrations", *(["users"] if changed_users else []))
    for user_id in changed_users:
        _queue_level_tag_refresh(user_id)
    if changed_users:
        logging.info("Пересчитаны старые уровни 5: %s пользователей", len(changed_users))
    return len(changed_users)


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
        await db.execute('''CREATE TABLE IF NOT EXISTS recent_user_messages (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            deleted_at INTEGER,
            PRIMARY KEY (chat_id, message_id)
        )''')
        await db.execute('''CREATE INDEX IF NOT EXISTS idx_recent_user_messages_lookup
            ON recent_user_messages (chat_id, user_id, created_at DESC)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS chat_user_activity (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            first_message_at INTEGER NOT NULL,
            last_message_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )''')
        await db.execute('''INSERT OR IGNORE INTO chat_user_activity
            (chat_id, user_id, message_count, first_message_at, last_message_at)
            SELECT chat_id, user_id, COUNT(*), MIN(created_at), MAX(created_at)
            FROM recent_user_messages
            GROUP BY chat_id, user_id''')
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

        # Долгоживущая прогрессия и социальные события. Все таблицы добавляются
        # отдельно: старые users/farm_* не переписываются и накопления не теряются.
        await db.execute('''CREATE TABLE IF NOT EXISTS month_scores (
            month_key TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            xp_earned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (month_key, user_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS month_results (
            month_key TEXT PRIMARY KEY,
            winner_id INTEGER,
            second_id INTEGER,
            third_id INTEGER,
            finalized_at INTEGER NOT NULL
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS duel_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            player1_id INTEGER NOT NULL,
            player2_id INTEGER NOT NULL,
            bet INTEGER NOT NULL,
            result TEXT NOT NULL,
            commission_rate INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            resolved_at INTEGER NOT NULL
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS active_duels (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER DEFAULT 0,
            player1_id INTEGER NOT NULL,
            player2_id INTEGER NOT NULL,
            player1_name TEXT NOT NULL,
            player2_name TEXT NOT NULL,
            bet INTEGER NOT NULL,
            state TEXT NOT NULL,
            player1_choice TEXT,
            player2_choice TEXT,
            escrowed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS factory_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            order_type TEXT NOT NULL,
            size TEXT NOT NULL,
            coin_cost INTEGER NOT NULL,
            xp_bank INTEGER NOT NULL,
            topic TEXT DEFAULT '',
            status TEXT NOT NULL,
            message_id INTEGER DEFAULT 0,
            vote_message_id INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            stage_ends_at INTEGER NOT NULL,
            metadata TEXT DEFAULT '{}'
            ,distributed_xp INTEGER DEFAULT 0
        )''')
        try:
            await db.execute("ALTER TABLE factory_orders ADD COLUMN distributed_xp INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.execute('''CREATE TABLE IF NOT EXISTS factory_order_participants (
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            submission_message_id INTEGER DEFAULT 0,
            replies_received INTEGER DEFAULT 0,
            joined_at INTEGER NOT NULL,
            PRIMARY KEY (order_id, user_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS factory_order_messages (
            order_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            parent_author_id INTEGER,
            PRIMARY KEY (order_id, message_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS factory_order_votes (
            order_id INTEGER NOT NULL,
            voter_id INTEGER NOT NULL,
            candidate_id INTEGER NOT NULL,
            PRIMARY KEY (order_id, voter_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS chat_level_tags (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            applied_level INTEGER DEFAULT 0,
            retry_after INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            PRIMARY KEY (chat_id, user_id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS data_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at INTEGER NOT NULL
        )''')
        await db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_factory_one_active_chat ON factory_orders(chat_id) WHERE status IN (\'active\', \'voting\', \'tournament\')')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_duel_pair_day ON duel_history(player1_id, player2_id, resolved_at)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_month_score_rank ON month_scores(month_key, xp_earned DESC)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_chat_level_tags_user ON chat_level_tags(user_id)')
        
        await db.commit()
    await _startup_bootstrap()
    # If Neon was unavailable, the retry loop will run this migration after the
    # authoritative copy has been restored instead of touching a stale local DB.
    if _bootstrap_complete:
        await migrate_legacy_level_fives()
    await finalize_closed_months()
    await refund_interrupted_duels()

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


async def track_recent_message(chat_id: int, user_id: int, message_id: int, created_at: int = None):
    """Remember a message and return its lifetime number for this user in this chat."""
    timestamp = int(created_at or time.time())
    cutoff = timestamp - (48 * 60 * 60)
    async with aiosqlite.connect(DB_NAME) as db:
        insert_cursor = await db.execute(
            '''INSERT OR IGNORE INTO recent_user_messages
               (chat_id, user_id, message_id, created_at, deleted_at)
               VALUES (?, ?, ?, ?, NULL)''',
            (chat_id, user_id, message_id, timestamp),
        )
        if insert_cursor.rowcount:
            await db.execute(
                '''INSERT INTO chat_user_activity
                   (chat_id, user_id, message_count, first_message_at, last_message_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET
                       message_count = message_count + 1,
                       last_message_at = excluded.last_message_at''',
                (chat_id, user_id, timestamp, timestamp),
            )
        await db.execute('DELETE FROM recent_user_messages WHERE created_at < ?', (cutoff,))
        cursor = await db.execute(
            'SELECT message_count FROM chat_user_activity WHERE chat_id = ? AND user_id = ?',
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
        await db.commit()
        return int(row[0]) if row else 1


async def get_recent_message_ids(
    chat_id: int,
    user_id: int,
    limit: int,
    within_seconds: int = 48 * 60 * 60,
):
    safe_limit = max(1, min(int(limit), 100))
    cutoff = int(time.time()) - max(1, int(within_seconds))
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            '''SELECT message_id
               FROM recent_user_messages
               WHERE chat_id = ? AND user_id = ?
                 AND deleted_at IS NULL AND created_at >= ?
               ORDER BY created_at DESC, message_id DESC
               LIMIT ?''',
            (chat_id, user_id, cutoff, safe_limit),
        )
        return [row[0] for row in await cursor.fetchall()]


async def mark_recent_messages_deleted(chat_id: int, message_ids):
    ids = [int(message_id) for message_id in message_ids]
    if not ids:
        return
    placeholders = ','.join('?' for _ in ids)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            f'''UPDATE recent_user_messages
                SET deleted_at = ?
                WHERE chat_id = ? AND message_id IN ({placeholders})''',
            (int(time.time()), chat_id, *ids),
        )
        await db.commit()

def total_available_xp(xp: int, level: int) -> int:
    """XP that can actually be lost, including completed level bars."""
    return max(0, int(xp)) + sum(int(LEVEL_CAPS[lvl]) for lvl in range(1, max(1, int(level))))


async def update_xp(user_id, xp_amount, count_monthly=False, month_key=None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
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
        if count_monthly and xp_amount > 0:
            key = month_key or datetime.now().strftime("%Y-%m")
            await db.execute('''
                INSERT INTO month_scores(month_key, user_id, xp_earned) VALUES (?, ?, ?)
                ON CONFLICT(month_key, user_id) DO UPDATE SET xp_earned = xp_earned + excluded.xp_earned
            ''', (key, user_id, int(xp_amount)))
        await db.commit()
        await _mark_dirty("users", *( ["month_scores"] if count_monthly and xp_amount > 0 else [] ))
        if current_lvl != old_lvl:
            _queue_level_tag_refresh(user_id)
        return (old_lvl, current_lvl, xp_amount)

async def give_reputation(from_user_id, to_user_id):
    if from_user_id == to_user_id:
        return "self_rep"

    today = datetime.now().strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute('SELECT 1 FROM rep_history WHERE from_id = ? AND to_id = ? AND date_str = ?', 
                                  (from_user_id, to_user_id, today))
        if await cursor.fetchone():
            return "daily_limit_user" 
        
        cursor = await db.execute('SELECT count(*) FROM rep_history WHERE from_id = ? AND date_str = ?',
                                  (from_user_id, today))
        count = await cursor.fetchone()
        if count and count[0] >= 3:
            return "daily_limit_total"

        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        cursor = await db.execute('''SELECT 1 FROM rep_history
            WHERE from_id = ? AND to_id = ? AND date_str >= ? LIMIT 1''',
                                  (from_user_id, to_user_id, since))
        repeated = bool(await cursor.fetchone())

        await db.execute('INSERT INTO rep_history (from_id, to_id, date_str) VALUES (?, ?, ?)',
                         (from_user_id, to_user_id, today))
        await db.execute('UPDATE users SET reputation = reputation + 1 WHERE user_id = ?', (to_user_id,))
        await db.commit()
        await _mark_dirty("rep_history", "users")
        
        return "success_repeat" if repeated else "success_full"


def _previous_month_key(now=None):
    now = now or datetime.now()
    first = now.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


async def get_month_score(user_id: int, month_key=None):
    key = month_key or datetime.now().strftime("%Y-%m")
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute(
            "SELECT xp_earned FROM month_scores WHERE month_key=? AND user_id=?", (key, user_id)
        )).fetchone()
        score = int(row[0]) if row else 0
        rank_row = await (await db.execute('''SELECT 1 + COUNT(*) FROM month_scores
            WHERE month_key=? AND xp_earned > ?''', (key, score))).fetchone()
        return score, int(rank_row[0]) if rank_row and score > 0 else None


async def get_month_leaders(limit=10, month_key=None):
    key = month_key or datetime.now().strftime("%Y-%m")
    async with aiosqlite.connect(DB_NAME) as db:
        c = await db.execute('''SELECT COALESCE(u.full_name, 'Игрок'), s.xp_earned, s.user_id
            FROM month_scores s LEFT JOIN users u ON u.user_id=s.user_id
            WHERE s.month_key=? ORDER BY s.xp_earned DESC, s.user_id ASC LIMIT ?''', (key, limit))
        return await c.fetchall()


async def get_month_wins(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute(
            "SELECT COUNT(*) FROM month_results WHERE winner_id=?", (user_id,)
        )).fetchone()
        last = await (await db.execute('''SELECT month_key FROM month_results
            WHERE winner_id=? ORDER BY month_key DESC LIMIT 1''', (user_id,))).fetchone()
        return int(row[0] or 0), (last[0] if last else None)


async def finalize_closed_months(now=None):
    """Finalize every scored month before the current one exactly once."""
    now = now or datetime.now()
    current_key = now.strftime("%Y-%m")
    async with _month_finalize_lock:
        async with aiosqlite.connect(DB_NAME) as db:
            rows = await (await db.execute(
                "SELECT DISTINCT month_key FROM month_scores WHERE month_key < ? ORDER BY month_key", (current_key,)
            )).fetchall()
        for (key,) in rows:
            level_changes = set()
            async with aiosqlite.connect(DB_NAME) as db:
                exists = await (await db.execute(
                    "SELECT 1 FROM month_results WHERE month_key=?", (key,)
                )).fetchone()
                if exists:
                    continue
                leaders = await (await db.execute('''SELECT user_id FROM month_scores
                    WHERE month_key=? ORDER BY xp_earned DESC, user_id ASC LIMIT 3''', (key,))).fetchall()
                ids = [int(r[0]) for r in leaders]
                for idx, uid in enumerate(ids):
                    row = await (await db.execute("SELECT xp, level FROM users WHERE user_id=?", (uid,))).fetchone()
                    if not row:
                        continue
                    xp, lvl = int(row[0]), int(row[1])
                    old_lvl = lvl
                    amount = MONTH_PRIZES[idx]
                    new_xp = xp + amount
                    while lvl < 5 and new_xp >= LEVEL_CAPS[lvl]:
                        new_xp -= int(LEVEL_CAPS[lvl]); lvl += 1
                    await db.execute("UPDATE users SET xp=?, level=? WHERE user_id=?", (new_xp, lvl, uid))
                    if lvl != old_lvl:
                        level_changes.add(uid)
                padded = ids + [None] * (3 - len(ids))
                await db.execute('''INSERT INTO month_results
                    (month_key,winner_id,second_id,third_id,finalized_at) VALUES(?,?,?,?,?)''',
                    (key, padded[0], padded[1], padded[2], int(time.time())))
                await db.commit()
            await _mark_dirty("users", "month_results")
            for uid in level_changes:
                _queue_level_tag_refresh(uid)


async def _month_processor():
    while True:
        try:
            await finalize_closed_months()
        except Exception:
            logging.exception("Ошибка завершения месячного рейтинга")
        await asyncio.sleep(300)


def start_month_processor():
    global _month_processor_task
    if not _month_processor_task or _month_processor_task.done():
        _month_processor_task = asyncio.create_task(_month_processor())


async def get_previous_month_title():
    key = _previous_month_key()
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute('''SELECT r.winner_id, COALESCE(u.full_name,'Игрок')
            FROM month_results r LEFT JOIN users u ON u.user_id=r.winner_id WHERE r.month_key=?''', (key,))).fetchone()
        return (key, row[0], row[1]) if row and row[0] else None


async def save_active_duel(duel: dict):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''INSERT OR REPLACE INTO active_duels
            (chat_id,message_id,player1_id,player2_id,player1_name,player2_name,bet,state,
             player1_choice,player2_choice,escrowed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (duel["chat_id"], duel.get("message_id", 0), duel["initiator"], duel["target"],
             duel["initiator_name"], duel["target_name"], duel["bet"], duel["state"],
             duel.get("p1_choice"), duel.get("p2_choice"), int(duel.get("escrowed", False)),
             duel.get("created_at", int(time.time()))))
        await db.commit()
    await _mark_dirty("active_duels")


async def load_active_duel(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute("SELECT * FROM active_duels WHERE chat_id=?", (chat_id,))).fetchone()
    if not row:
        return None
    return {"chat_id": row[0], "message_id": row[1], "initiator": row[2], "target": row[3],
            "initiator_name": row[4], "target_name": row[5], "bet": row[6], "state": row[7],
            "p1_choice": row[8], "p2_choice": row[9], "escrowed": bool(row[10]), "created_at": row[11]}


async def escrow_active_duel(chat_id: int) -> bool:
    """Atomically deduct both stakes and move a waiting duel to fighting."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        duel = await (await db.execute('''SELECT player1_id,player2_id,bet,state
            FROM active_duels WHERE chat_id=?''', (chat_id,))).fetchone()
        if not duel or duel[3] != "waiting_accept":
            await db.rollback(); return False
        balances = []
        level_changes = set()
        for uid in duel[:2]:
            row = await (await db.execute("SELECT xp,level FROM users WHERE user_id=?", (uid,))).fetchone()
            if not row or total_available_xp(row[0], row[1]) < int(duel[2]):
                await db.rollback(); return False
            old_lvl = int(row[1])
            xp, lvl = int(row[0]) - int(duel[2]), old_lvl
            while xp < 0 and lvl > 1:
                lvl -= 1; xp += int(LEVEL_CAPS[lvl])
            if xp < 0:
                await db.rollback(); return False
            balances.append((xp, lvl, uid))
            if lvl != old_lvl:
                level_changes.add(int(uid))
        await db.executemany("UPDATE users SET xp=?,level=? WHERE user_id=?", balances)
        await db.execute("UPDATE active_duels SET state='fighting',escrowed=1 WHERE chat_id=?", (chat_id,))
        await db.commit()
    await _mark_dirty("users", "active_duels")
    for uid in level_changes:
        _queue_level_tag_refresh(uid)
    return True


async def delete_active_duel(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM active_duels WHERE chat_id=?", (chat_id,)); await db.commit()
    await _mark_dirty("active_duels")


async def refund_interrupted_duels():
    """A restart can invalidate old buttons; escrow is therefore returned safely."""
    level_changes = set()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await (await db.execute(
            "SELECT player1_id,player2_id,bet FROM active_duels WHERE escrowed=1")).fetchall()
        for p1, p2, bet in rows:
            for uid in (p1, p2):
                state = await (await db.execute("SELECT xp,level FROM users WHERE user_id=?", (uid,))).fetchone()
                if not state: continue
                old_lvl = int(state[1])
                xp, lvl = int(state[0]) + int(bet), old_lvl
                while lvl < 5 and xp >= LEVEL_CAPS[lvl]: xp -= int(LEVEL_CAPS[lvl]); lvl += 1
                await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, lvl, uid))
                if lvl != old_lvl:
                    level_changes.add(int(uid))
        await db.execute("DELETE FROM active_duels"); await db.commit()
    if rows:
        await _mark_dirty("active_duels", "users")
        for uid in level_changes:
            _queue_level_tag_refresh(uid)


async def expire_stale_duels(max_age_seconds=900):
    cutoff = int(time.time()) - int(max_age_seconds)
    level_changes = set()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await (await db.execute('''SELECT chat_id,player1_id,player2_id,bet,escrowed
            FROM active_duels WHERE created_at<=?''', (cutoff,))).fetchall()
        for _chat, p1, p2, bet, escrowed in rows:
            if not escrowed: continue
            for uid in (p1, p2):
                state = await (await db.execute("SELECT xp,level FROM users WHERE user_id=?", (uid,))).fetchone()
                if not state: continue
                old_lvl = int(state[1])
                xp, lvl = int(state[0]) + int(bet), old_lvl
                while lvl < 5 and xp >= LEVEL_CAPS[lvl]: xp -= int(LEVEL_CAPS[lvl]); lvl += 1
                await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, lvl, uid))
                if lvl != old_lvl:
                    level_changes.add(int(uid))
        if rows:
            await db.executemany("DELETE FROM active_duels WHERE chat_id=?", [(r[0],) for r in rows])
        await db.commit()
    if rows:
        await _mark_dirty("active_duels", "users")
        for uid in level_changes:
            _queue_level_tag_refresh(uid)
    return [(int(r[0]), bool(r[4])) for r in rows]


async def duel_pair_count_today(player1_id: int, player2_id: int) -> int:
    start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    a, b = sorted((player1_id, player2_id))
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute('''SELECT COUNT(*) FROM duel_history
            WHERE MIN(player1_id,player2_id)=? AND MAX(player1_id,player2_id)=?
              AND resolved_at>=? AND result!='draw' ''', (a, b, start))).fetchone()
        return int(row[0] or 0)


async def record_duel(duel: dict, result: str, commission_rate: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''INSERT INTO duel_history
            (chat_id,player1_id,player2_id,bet,result,commission_rate,created_at,resolved_at)
            VALUES(?,?,?,?,?,?,?,?)''', (duel["chat_id"], duel["initiator"], duel["target"], duel["bet"],
            result, commission_rate, duel.get("created_at", int(time.time())), int(time.time())))
        await db.execute("DELETE FROM active_duels WHERE chat_id=?", (duel["chat_id"],))
        await db.commit()
    await _mark_dirty("duel_history", "active_duels")


async def settle_active_duel(duel: dict, winner_id=None, commission_rate=0):
    """Atomically pay/refund an escrowed duel and close it."""
    level_changes = set()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT escrowed FROM active_duels WHERE chat_id=?", (duel["chat_id"],))).fetchone()
        if not row or not int(row[0]):
            await db.rollback(); return None
        bet = int(duel["bet"])
        awards = ({duel["initiator"]: bet, duel["target"]: bet} if winner_id is None
                  else {int(winner_id): bet * 2 * (100 - int(commission_rate)) // 100})
        for uid, amount in awards.items():
            state = await (await db.execute("SELECT xp,level FROM users WHERE user_id=?", (uid,))).fetchone()
            if not state: continue
            old_lvl = int(state[1])
            xp, lvl = int(state[0]) + int(amount), old_lvl
            while lvl < 5 and xp >= LEVEL_CAPS[lvl]: xp -= int(LEVEL_CAPS[lvl]); lvl += 1
            await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, lvl, uid))
            if lvl != old_lvl:
                level_changes.add(int(uid))
        result = "draw" if winner_id is None else str(winner_id)
        await db.execute('''INSERT INTO duel_history
            (chat_id,player1_id,player2_id,bet,result,commission_rate,created_at,resolved_at)
            VALUES(?,?,?,?,?,?,?,?)''', (duel["chat_id"], duel["initiator"], duel["target"], bet,
            result, int(commission_rate), duel.get("created_at", int(time.time())), int(time.time())))
        await db.execute("DELETE FROM active_duels WHERE chat_id=?", (duel["chat_id"],))
        await db.commit()
    await _mark_dirty("users", "duel_history", "active_duels")
    for uid in level_changes:
        _queue_level_tag_refresh(uid)
    return sum(awards.values()) if winner_id is not None else 0


async def farm_is_complete(user_id: int) -> bool:
    state = await farm_get_state(user_id)
    return (len(state.get("opened_cells", [])) == FARM_CELLS_TOTAL
            and len(state.get("cells", [])) == FARM_CELLS_TOTAL
            and all(int(c.get("level", 0)) >= 3 for c in state.get("cells", [])))

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
        "bootstrap_complete": _bootstrap_complete,
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
    global _next_sync_attempt_at, _neon_had_failure
    try:
        async with _sync_execution_lock:
            if not await _bootstrap_from_neon_if_needed():
                _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
                _schedule_retry_wakeup()
                details = _last_neon_error or "причина не зафиксирована"
                return {"ok": False, "message": f"Startup-восстановление Neon не завершено.\nДетали: {details}"}

            await migrate_legacy_level_fives()
            dirty = await _load_dirty_tables()
            if not dirty:
                return {"ok": True, "message": "Синхронизация не нужна: изменений нет."}

            if not await _connect_neon_ready():
                _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
                _schedule_retry_wakeup()
                details = _last_neon_error or "причина не зафиксирована"
                return {"ok": False, "message": f"Neon недоступен, ручной sync не выполнен.\nДетали: {details}"}

            result = await _sync_dirty_tables_once()
        if result["ok"]:
            await _set_meta_int("sync_activity_count", 0)
            _next_sync_attempt_at = _now_ts() + SYNC_MIN_INTERVAL_SECONDS
            if result["synced"]:
                synced_list = ", ".join(result["synced"])
                _neon_had_failure = False
                await _send_db_alert(
                    "manual_sync_success",
                    f"Ручная синхронизация Neon выполнена. Таблицы: {synced_list}.",
                )
                return {"ok": True, "message": f"Ручная синхронизация выполнена успешно.\nСинхронизировано таблиц: {synced_list}"}
            return {"ok": True, "message": "Изменения уже были синхронизированы, новых таблиц не найдено."}

        _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
        _schedule_retry_wakeup()
        table = result["failed_table"] or "неизвестно"
        details = result["error"] or "причина не зафиксирована"
        return {"ok": False, "message": f"Ошибка ручного sync таблицы {table}.\nДетали: {details}"}
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        _neon_had_failure = True
        _set_last_neon_error(error)
        await _reset_neon_pool()
        await _send_db_alert("manual_sync_failed", f"Ошибка ручного sync Neon.\nОшибка: {error}")
        _next_sync_attempt_at = _now_ts() + SYNC_RETRY_INTERVAL_SECONDS
        _schedule_retry_wakeup()
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
    global _bootstrap_complete
    async with _db_import_lock:
        await asyncio.to_thread(_validate_sqlite_file_sync, uploaded_db_path)
        await asyncio.to_thread(_replace_local_db_sync, uploaded_db_path)
        await create_tables()
        # A file explicitly uploaded by the owner is authoritative and should
        # be pushed to Neon instead of being overwritten by startup restore.
        _bootstrap_complete = True
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


async def farm_spend_coins(user_id: int, amount: int) -> bool:
    """Atomic factory purchase; unlike adjustment it never clamps or overspends."""
    await _farm_get_or_create_player(user_id)
    amount = max(0, int(amount))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute('''UPDATE farm_players SET coins=coins-?, updated_at=?
            WHERE user_id=? AND coins>=?''', (amount, _farm_now(), user_id, amount))
        ok = cur.rowcount == 1
        await db.commit()
    if ok:
        await _mark_dirty("farm_players")
    return ok


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

