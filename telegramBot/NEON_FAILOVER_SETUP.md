## Neon failover setup

The project reads the Neon connection string only from the environment:

```text
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

`NEON_DATABASE_URL` may be used as an alternative name. Do not place a real
connection string in this repository, documentation, source files, or commits.

### Local testing

The local `.env` deliberately has no database URL. The test bot therefore uses
the isolated SQLite file configured by `DB_PATH`.

### Render

Set `DATABASE_URL` (and `BOT_TOKEN`) in the Render service environment, then
redeploy. Do not use a connection string stored in the repository.

### Behaviour

- Neon is optional; the bot can continue with its local SQLite data if Neon is unavailable.
- Startup sync is disabled by default to preserve free-tier compute time.
- Sync behaviour can be tuned with the `DB_SYNC_*` variables in `database.py`.
