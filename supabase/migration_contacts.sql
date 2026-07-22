-- Run once in Supabase SQL editor
create table if not exists contacts (
    id uuid primary key default gen_random_uuid(),
    email text not null unique,
    display_name text,
    name_key text not null default '',
    message_count int not null default 1,
    last_seen_at timestamptz not null default now(),
    created_at timestamptz not null default now()
);

create index if not exists idx_contacts_name_key on contacts (name_key);
create index if not exists idx_contacts_last_seen on contacts (last_seen_at desc);
