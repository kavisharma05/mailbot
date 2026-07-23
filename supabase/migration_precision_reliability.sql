-- Precision timing, heartbeat, OAuth reconnect (run once in Supabase SQL editor)

-- Deadline-relative scheduled reminders
create table if not exists scheduled_reminders (
    id uuid primary key default gen_random_uuid(),
    assignment_id uuid not null references assignments (id) on delete cascade,
    fire_at timestamptz not null,
    tier text not null check (tier in ('24h', '3h', '1h', '30m')),
    status text not null default 'pending' check (status in ('pending', 'sent', 'cancelled')),
    created_at timestamptz not null default now()
);

create index if not exists idx_scheduled_reminders_due
    on scheduled_reminders (status, fire_at)
    where status = 'pending';

-- Fine-grained categorization
alter table assignments add column if not exists item_type text
    check (item_type in ('assignment', 'exam', 'fee', 'form', 'other'));
alter table assignments add column if not exists priority text
    check (priority in ('high', 'normal', 'low'));

-- OAuth token storage (DB-first refresh token; env fallback)
create table if not exists oauth_tokens (
    id int primary key default 1 check (id = 1),
    provider text not null default 'gmail',
    refresh_token text not null,
    updated_at timestamptz not null default now()
);

-- Watch state extensions for heartbeat + Gmail outage tracking
alter table watch_state add column if not exists gmail_disconnected_at timestamptz;
alter table watch_state add column if not exists gmail_reconnected_at timestamptz;
alter table watch_state add column if not exists last_heartbeat_at timestamptz;
