-- Multi-user mailbot (run once in Supabase SQL editor after prior migrations)
-- Backfills existing data to a seed owner row; app startup (ensure_owner_user) fills Telegram/Gmail from env.

-- Fixed id so Python can match: services/users.py OWNER_SEED_USER_ID
-- Seed owner is updated from env on every app start.

create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id text unique,
    telegram_chat_id text,
    telegram_username text,
    display_name text not null default 'Student',
    gmail_address text,
    preferred_section text default 'A',
    role text not null default 'student' check (role in ('owner', 'student')),
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_users_role on users (role) where is_active = true;
create index if not exists idx_users_telegram_username on users (lower(telegram_username)) where telegram_username is not null;

insert into users (id, role, display_name, is_active)
values ('00000000-0000-4000-8000-000000000001'::uuid, 'owner', 'Owner', true)
on conflict (id) do nothing;

create table if not exists user_invites (
    id uuid primary key default gen_random_uuid(),
    telegram_username text not null,
    invite_code text not null unique,
    claimed_by uuid references users (id) on delete set null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_user_invites_username on user_invites (lower(telegram_username));
create index if not exists idx_user_invites_claimed on user_invites (claimed_by) where claimed_by is not null;

create table if not exists web_sessions (
    token_hash text primary key,
    user_id uuid not null references users (id) on delete cascade,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    last_seen_at timestamptz not null default now()
);

create index if not exists idx_web_sessions_user on web_sessions (user_id);

create table if not exists login_codes (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users (id) on delete cascade,
    code_hash text not null,
    expires_at timestamptz not null,
    attempts int not null default 0,
    consumed_at timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists idx_login_codes_user on login_codes (user_id, created_at desc);

-- Domain tables: user_id
alter table assignments add column if not exists user_id uuid references users (id) on delete cascade;
alter table processed_emails add column if not exists user_id uuid references users (id) on delete cascade;
alter table pending_emails add column if not exists user_id uuid references users (id) on delete cascade;
alter table scheduled_reminders add column if not exists user_id uuid references users (id) on delete cascade;
alter table contacts add column if not exists user_id uuid references users (id) on delete cascade;
alter table class_sessions add column if not exists user_id uuid references users (id) on delete cascade;
alter table low_confidence_log add column if not exists user_id uuid references users (id) on delete set null;

update assignments set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null;
update processed_emails set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null;
update pending_emails set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null;
update contacts set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null;
update class_sessions set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null;

-- watch_state: per-user (migrate singleton row)
alter table watch_state add column if not exists user_id uuid references users (id) on delete cascade;
update watch_state set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null and id = 1;

-- digest_state: per-user
alter table digest_state add column if not exists user_id uuid references users (id) on delete cascade;
update digest_state set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null and id = 1;

-- oauth_tokens: per-user
alter table oauth_tokens add column if not exists user_id uuid references users (id) on delete cascade;
update oauth_tokens set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null and id = 1;

-- Preferred section from user_prefs -> users
update users u
set preferred_section = coalesce(
    (select preferred_section from user_prefs where id = 1 limit 1),
    u.preferred_section
)
where u.id = '00000000-0000-4000-8000-000000000001'::uuid;

-- Uniqueness: assignments open dedupe per user
drop index if exists idx_assignments_gmail_message_open;
create unique index if not exists idx_assignments_user_gmail_message_open
    on assignments (user_id, gmail_message_id)
    where gmail_message_id is not null and status = 'open';

-- contacts: per user email
alter table contacts drop constraint if exists contacts_email_key;
create unique index if not exists idx_contacts_user_email on contacts (user_id, email);

-- processed_emails: composite identity (keep message_id PK for now; add unique per user)
-- New installs: use (user_id, message_id). Existing PK is message_id only — add unique constraint
create unique index if not exists idx_processed_emails_user_message
    on processed_emails (user_id, message_id)
    where user_id is not null;

-- pending_emails: per user (message ids can collide across mailboxes in theory)
alter table pending_emails add column if not exists user_id uuid references users (id) on delete cascade;
update pending_emails set user_id = '00000000-0000-4000-8000-000000000001'::uuid where user_id is null;

create unique index if not exists idx_pending_emails_user_message
    on pending_emails (user_id, message_id)
    where user_id is not null;

-- Indexes for scoped queries
create index if not exists idx_assignments_user_status on assignments (user_id, status);
create index if not exists idx_watch_state_user on watch_state (user_id);
create index if not exists idx_digest_state_user on digest_state (user_id);
create index if not exists idx_oauth_tokens_user_provider on oauth_tokens (user_id, provider);
