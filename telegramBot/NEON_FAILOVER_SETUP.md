## Neon failover setup

### 1) Required env
- Необязателен: строка Neon уже встроена в код.
- Если нужно переопределить: `DATABASE_URL`.

### 2) Optional env
- DB-уведомления теперь всегда идут на `OWNER_ID` из `config.py`.
- `DB_SYNC_CHECK_INTERVAL` = local check interval in seconds. Default: `120`.
- `DB_SYNC_MIN_INTERVAL` = minimum pause between successful syncs. Default: `18000` (5h).
- `DB_SYNC_RETRY_INTERVAL` = retry pause after failed sync. Default: `600`.
- `DB_SYNC_MIN_LOCAL_CHANGES` = minimum local changes before sync. Default: `120`.
- `DB_SYNC_MAX_DIRTY_AGE` = force sync if dirty too long. Default: `604800`.

### 3) Local test in PowerShell
```powershell
$env:DATABASE_URL="postgresql://...neon.tech/..."
$env:DB_SYNC_MIN_INTERVAL="18000"
$env:DB_SYNC_MIN_LOCAL_CHANGES="120"
py -3 main.py
```

### 4) Behavior summary
- Bot always works with local SQLite runtime.
- If Neon is available, dirty tables are synced in background.
- If Neon is down, bot continues locally.
- If local DB is empty on startup and Neon has data, local DB is restored from Neon.
- If Neon is empty and local has data, Neon is seeded from local.
- Alerts are sent to your Telegram for DB events (if alert target is configured).

### 5) Status command
- Owner command: `/dbstatus`
- Shows current DB mode, dirty tables, counters, timers and alert target.
