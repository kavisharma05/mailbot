-- Run once in Supabase SQL editor
-- Tracks last sent digest dates so in-app scheduler and GitHub Actions cron cannot double-send.

create table if not exists digest_state (
    id int primary key default 1 check (id = 1),
    last_morning date,
    last_evening date,
    updated_at timestamptz not null default now()
);

insert into digest_state (id) values (1) on conflict (id) do nothing;
