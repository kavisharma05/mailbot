-- Run once in Supabase SQL editor
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
