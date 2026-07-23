-- Run once in Supabase SQL editor
-- Persists Telegram user_data (conversation memory + compose drafts) across restarts.

create table if not exists user_data_state (
    user_id text primary key,
    data jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create index if not exists idx_user_data_state_updated on user_data_state (updated_at desc);
