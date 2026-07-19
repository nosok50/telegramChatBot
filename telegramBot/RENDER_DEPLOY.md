# Deploy to GitHub and Render

## What to upload to GitHub

This repository already has an outer `telegramBot` directory. Update its
contents from this local directory:

```text
telegramChatBot-main/telegramBot/
```

The GitHub layout must be `telegramBot/main.py`,
`telegramBot/requirements.txt`, `telegramBot/config.py`, and
`telegramBot/modules/`. Do not create another nested
`telegramChatBot-main` directory in GitHub.

The `.gitignore` in this directory deliberately excludes `.env`, virtual
environments, logs, and every `.db` file. Never force-add any of them.

## Render service settings

Use an existing **Web Service** connected to this GitHub repository.

- Root Directory: `telegramBot`
- Build Command: `pip install -r requirements.txt`
- Start Command: `python main.py`
- Health Check Path: `/`

The application starts its small Flask health endpoint automatically on Render.
Your existing five-minute ping should target the service root URL.

## Render environment variables

Open the service, choose **Environment**, and add these values there:

```text
BOT_TOKEN=<new production bot token>
DATABASE_URL=<new Neon pooled connection string>
OWNER_ID=1089429471
DB_STARTUP_BOOTSTRAP_NEON=1
DB_SYNC_MIN_ACTIVITY_MESSAGES=100
DB_SYNC_MIN_INTERVAL=18000
DB_SYNC_RETRY_INTERVAL=600
```

Do not add these values to GitHub, `render.yaml`, source files, or local
documentation. Saving an environment value with **Save and deploy** restarts
the service using the new secret.

## Deploy

If Render auto-deploy is enabled, pushing to the configured branch deploys the
new commit automatically. Otherwise open **Manual Deploy** in Render and
choose **Deploy latest commit**.

The first boot restores the local SQLite database from Neon when Neon already
contains users. Later normal sync remains event-driven: it needs 100 activity
messages and respects the existing five-hour minimum interval.

## Secret rotation

Before the production deploy, revoke any Telegram token or Neon password that
has ever been posted in chat or committed to Git. Generate fresh replacements,
then enter them directly into Render's Environment form.
