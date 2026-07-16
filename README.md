# Mailbot

A Telegram bot that helps MBA/college students manage academic email: compose polite emails to faculty, and get categorized Telegram alerts when Gmail contains actionable items.

## Features (core)

- **`/send`** — Turn rough instructions into a polished email draft, approve, and send via Gmail
- **Email watcher** — Polls Gmail every 15 minutes, classifies emails with Groq (Llama 3.3), routes alerts to category-specific Telegram groups
- **Scheduled digests** — Morning (7:00 IST) and evening (19:00 IST) summaries per category group
- **Attachments** — Downloads mail attachments, reads PDF/Excel/Word text for classification, and forwards files to the category Telegram group **and** your DM
- **Class timetable** — Parses weekly schedules from body or attachments; morning digest + `/classes` show today's courses, times, and rooms
- **14-day backfill** — One-time scan of recent inbox mail via `POST /tasks/backfill`

Email categories: `deadline`, `placement`, `admin`, `other`.

Scaffolded for later: submission matcher (#3), manual reminder parser (#4).

## Architecture

- **FastAPI** app on Render (free tier) with Telegram webhook
- **GitHub Actions** cron hits `/tasks/watch-gmail` every 15 min, plus digest endpoints at 7:00 and 19:00 IST
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
3. Optionally set `CEREBRAS_API_KEY` as fallback when Groq hits daily limits

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

The `.github/workflows/watcher.yml` workflow will:

| Schedule (UTC) | IST | Endpoint |
|----------------|-----|----------|
| `*/15 * * * *` | every 15 min | `/tasks/watch-gmail` |
| `30 1 * * *` | 7:00 | `/tasks/digest-morning` |
| `30 13 * * *` | 19:00 | `/tasks/digest-evening` |

Use **Actions → Gmail Deadline Watcher → Run workflow** to manually trigger the watcher or either digest.

### One-time 14-day backfill

After deploy, scan recent inbox mail once (does not affect ongoing watcher):

```bash
curl.exe -X POST -H "x-task-secret: <TASK_SECRET>" "https://your-app.onrender.com/tasks/backfill?days=14"
```

Backfill stores all classified emails but only alerts on actionable items with future (or unknown) deadlines.

### Test digests (dry run)

Returns JSON previews without sending Telegram messages:

```bash
curl.exe -X POST -H "x-task-secret: <TASK_SECRET>" "https://your-app.onrender.com/tasks/digest-morning?dry_run=1"
curl.exe -X POST -H "x-task-secret: <TASK_SECRET>" "https://your-app.onrender.com/tasks/digest-evening?dry_run=1"
```

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
| `CEREBRAS_API_KEY` | Cerebras fallback when Groq is rate-limited |
| `CEREBRAS_MODEL` | Cerebras model (default: `gemma-4-31b`) |
| `GMAIL_CLIENT_ID` | Google OAuth client ID |
| `GMAIL_CLIENT_SECRET` | Google OAuth client secret |
| `GMAIL_REFRESH_TOKEN` | From get_gmail_token.py |
| `GMAIL_USER` | Your Gmail address |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `TASK_SECRET` | Protects `/tasks/watch-gmail`, `/tasks/backfill`, and digest endpoints |
| `STUDENT_NAME` | Sign-off name for composed emails |
| `RENDER_URL` | Public URL (for set_webhook.py) |

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome + help |
| `/due` or `/list` | Show numbered open items for this chat's category |
| `/done <n>` | Mark item #n from the last `/due` list as complete |
| `/classes` | Today's classes with time and room (from timetable emails) |
| `/ask <question>` | Ask about deadlines, today's mails, senders, or email content |
| `/send` | Compose and send an email |
| `/cancel` | Cancel the current `/send` flow |

In groups, also reply to the bot or mention it (`@IMTAssistant_bot what's due?`).  
For free-text in groups without mentioning, disable privacy in BotFather: `/setprivacy` → Disable.

## Notes

- Verify `GROQ_MODEL` matches a model available on the Groq free tier
- Low-confidence classifier outputs are logged to `low_confidence_log` in Supabase
- Only `medium` and `high` confidence actionable emails trigger real-time Telegram alerts (every ~15 min)
- **Morning digest (7:00 IST):** today's due items + recently tracked assignments per category group
- **Evening digest (19:00 IST):** overdue, today, and tomorrow due items per category group
- If a category group has nothing due, that group is skipped; if all groups are empty, a short note goes to owner DM only
- Alerts route to category-specific groups when chat IDs are configured
- Watcher only processes **new** mail after first run; use `/tasks/backfill` for older mail
- Both the watcher and backfill scan the **Inbox and Spam** folders (important emails sometimes land in spam)
- LLM calls use **Groq first**, then **Cerebras** if Groq fails / rate-limits
- **Deadlines resolve in IST (Asia/Kolkata):** relative phrases like "tomorrow 9:45" are normalized server-side; date-only deadlines default to 23:59 IST
- If email classification fails (LLM/JSON error), the message is **not** marked processed and will retry on the next watcher run (~15 min)
- `/ask` uses stronger Gmail retrieval for date/topic questions and falls back to structured extracts when AI is unavailable
- Actionable alerts forward attachments to the category group **and** owner DM (skips tiny signature images; max ~20MB)
- Attachment text (PDF/Excel/DOCX) is read and fed into classification — useful for buried deadlines and timetables
- Run `supabase/migration_class_sessions.sql` once in Supabase for class timetable storage
