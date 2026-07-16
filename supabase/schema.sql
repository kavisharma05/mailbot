-- Mailbot schema for Supabase Postgres

create table if not exists assignments (
    id uuid primary key default gen_random_uuid(),
    source text not null check (source in ('email', 'manual')),
    course_or_topic text,
    task text,
    deadline_text text,
    deadline_date timestamptz,
    gmail_thread_id text,
    gmail_message_id text,
    expected_filename_hint text,
    summary text,
    category text check (category in ('deadline', 'placement', 'admin', 'other')),
    confidence text check (confidence in ('high', 'medium', 'low')),
    status text not null default 'open' check (status in ('open', 'done', 'dismissed')),
    created_at timestamptz not null default now()
);

create index if not exists idx_assignments_status on assignments (status);
create index if not exists idx_assignments_deadline on assignments (deadline_date);

create table if not exists processed_emails (
    message_id text primary key,
    processed_at timestamptz not null default now(),
    is_actionable boolean,
    confidence text,
    assignment_id uuid references assignments (id) on delete set null
);

create table if not exists watch_state (
    id int primary key default 1 check (id = 1),
    last_history_id text,
    last_checked_at timestamptz
);

insert into watch_state (id) values (1) on conflict (id) do nothing;

create table if not exists low_confidence_log (
    id uuid primary key default gen_random_uuid(),
    kind text not null,
    payload jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_low_confidence_log_created on low_confidence_log (created_at desc);

create table if not exists class_sessions (
    id uuid primary key default gen_random_uuid(),
    course_or_topic text not null,
    session_date date not null,
    start_time text not null,
    end_time text,
    room text,
    faculty text,
    notes text,
    week_start date,
    gmail_message_id text,
    created_at timestamptz not null default now()
);

create index if not exists idx_class_sessions_date on class_sessions (session_date);
create index if not exists idx_class_sessions_week on class_sessions (week_start);

-- Migration for existing projects (run once in Supabase SQL editor):
-- alter table assignments add column if not exists category text
--   check (category in ('deadline', 'placement', 'admin', 'other'));
-- create table class_sessions (...as above...);
