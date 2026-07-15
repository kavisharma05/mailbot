# Mailbot

A Telegram bot that helps MBA/college students manage academic email: compose polite emails to faculty, and get categorized Telegram alerts when Gmail contains actionable items.

## Features (core)

- **`/send`** — Turn rough instructions into a polished email draft, approve, and send via Gmail
- **Email watcher** — Polls Gmail every 15 minutes, classifies emails with Groq (Llama 3.3), routes alerts to category-specific Telegram groups
- **14-day backfill** — One-time scan of recent inbox mail via `POST /tasks/backfill`

Email categories: `deadline`, `placement`, `admin`, `other`.

Scaffolded for later: submission matcher (#3), manual reminder parser (#4).

## Architecture

- **FastAPI** app on Render (free tier) with Telegram webhook
- **GitHub Actions** cron hits `/tasks/watch-gmail` every 15 min (keeps Render awake + polls Gmail + keeps Supabase active)
- **Supabase** Postgres for assignments, processed emails, watch state
- **Gmail API** (OAuth2) for read + send
- **Groq API** (free tier, Llama 3.3 70B) for structured JSON classification/composition

## Quick start (local dev)

```bash
cd mailbot
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env     # fill in all values
```

### 1. Google Cloud + Gmail OAuth

1. Create a project at [Google Cloud Console](https://console.cloud.google.com/)
2. Enable **Gmail API**
3. Create OAuth 2.0 credentials (Desktop app)
4. Add yourself as a test user
5. Run the token script locally:

```bash
python scripts/get_gmail_token.py
```

Copy the printed `GMAIL_REFRESH_TOKEN` into `.env`.

### 2. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy `TELEGRAM_BOT_TOKEN`
3. Get your chat ID: message [@userinfobot](https://t.me/userinfobot) or send a message to your bot and call `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Set `TELEGRAM_WEBHOOK_SECRET` to a random string (e.g. `openssl rand -hex 16`)

#### Category groups (recommended for MBA)

Create four Telegram groups and add your bot to each:

| Group | Env var | Receives |
|-------|---------|----------|
| Deadlines | `TELEGRAM_CHAT_DEADLINES` | Assignments, projects, exams |
| Placements | `TELEGRAM_CHAT_PLACEMENTS` | Shortlists, CV deadlines, interviews |
| Admin | `TELEGRAM_CHAT_ADMIN` | Fees, registration, forms |
| Other | `TELEGRAM_CHAT_OTHER` | Events, clubs, FYI |

To get each group's chat ID: send a message in the group, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser and look for `"chat":{"id":-100...}`. If a category chat ID is blank, alerts fall back to `TELEGRAM_OWNER_CHAT_ID`.

### 3. Supabase

1. Create a project at [supabase.com](https://supabase.com)
2. Run `supabase/schema.sql` in the SQL editor
3. If upgrading an existing project, also run the migration at the bottom of `supabase/schema.sql` to add the `category` column
4. Copy `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (service role, not anon)

### 4. Groq (free)

1. Get a free API key from [console.groq.com/keys](https://console.groq.com/keys)
2. Set `GROQ_API_KEY` and optionally `GROQ_MODEL`

### 5. Run locally

```bash
uvicorn app:app --reload --port 8000
```

For local Telegram testing, use polling mode or ngrok + `scripts/set_webhook.py`.

## Deploy to Render (free)

1. Push this repo to GitHub
2. Create a **Web Service** on [render.com](https://render.com) (free tier)
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Add all env vars from `.env.example`
6. After deploy, register the webhook:

```bash
python scripts/set_webhook.py
```

7. Add GitHub repo secrets for the cron workflow:
   - `RENDER_URL` — your Render app URL (e.g. `https://mailbot-abc.onrender.com`)
   - `TASK_SECRET` — same value as in Render env

The `.github/workflows/watcher.yml` workflow will ping `/tasks/watch-gmail` every 15 minutes.

### One-time 14-day backfill

After deploy, scan recent inbox mail once (does not affect ongoing watcher):

```bash
curl.exe -X POST -H "x-task-secret: <TASK_SECRET>" "https://your-app.onrender.com/tasks/backfill?days=14"
```

Backfill stores all classified emails but only alerts on actionable items with future (or unknown) deadlines.

## Environment variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_OWNER_CHAT_ID` | Your Telegram chat ID (fallback for all alerts) |
| `TELEGRAM_WEBHOOK_SECRET` | Random secret for webhook path |
| `TELEGRAM_CHAT_DEADLINES` | Chat ID for deadline alerts (optional) |
| `TELEGRAM_CHAT_PLACEMENTS` | Chat ID for placement alerts (optional) |
| `TELEGRAM_CHAT_ADMIN` | Chat ID for admin alerts (optional) |
| `TELEGRAM_CHAT_OTHER` | Chat ID for other/FYI alerts (optional) |
| `GROQ_API_KEY` | Groq API key (free from console.groq.com) |
| `GROQ_MODEL` | Model name (default: llama-3.3-70b-versatile) |
| `GMAIL_CLIENT_ID` | Google OAuth client ID |
| `GMAIL_CLIENT_SECRET` | Google OAuth client secret |
| `GMAIL_REFRESH_TOKEN` | From get_gmail_token.py |
| `GMAIL_USER` | Your Gmail address |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `TASK_SECRET` | Protects `/tasks/watch-gmail` and `/tasks/backfill` |
| `STUDENT_NAME` | Sign-off name for composed emails |
| `RENDER_URL` | Public URL (for set_webhook.py) |

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/send` | Compose and send an email |

## Notes

- Verify `GROQ_MODEL` matches a model available on the Groq free tier
- Low-confidence classifier outputs are logged to `low_confidence_log` in Supabase
- Only `medium` and `high` confidence actionable emails trigger Telegram alerts
- Alerts route to category-specific groups when chat IDs are configured
- Watcher only processes **new** mail after first run; use `/tasks/backfill` for older mail
- Both the watcher and backfill scan the **Inbox and Spam** folders (important emails sometimes land in spam)
