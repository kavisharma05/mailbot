-- Run once in Supabase SQL editor
create table if not exists user_prefs (
    id int primary key default 1 check (id = 1),
    preferred_section text,
    updated_at timestamptz not null default now()
);

insert into user_prefs (id) values (1) on conflict (id) do nothing;
