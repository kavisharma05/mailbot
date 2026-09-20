-- Run once in Supabase SQL editor
-- IMT Nagpur MapMyAccess e-resources integration (catalog, publications, reports, state).

create table if not exists imt_state (
    id int primary key default 1 check (id = 1),
    session_token text,
    session_display_name text,
    session_saved_at timestamptz,
    last_catalog_sync_at timestamptz,
    last_daily_run_on date,
    last_error text,
    updated_at timestamptz not null default now()
);

insert into imt_state (id) values (1) on conflict (id) do nothing;

create table if not exists imt_resources (
    portal_id text primary key,
    name text not null,
    category text,
    url text,
    proxied boolean not null default false,
    is_active boolean not null default true,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now()
);

create index if not exists imt_resources_active_idx on imt_resources (is_active, category);

create table if not exists imt_publications (
    id uuid primary key default gen_random_uuid(),
    kind text not null,                -- epw | ft | et_prime
    section text,                      -- EPW section (Editorials, Special Articles, ...)
    title text not null,
    url text not null,
    publication_date date not null,
    summary text,
    fetched_at timestamptz not null default now(),
    unique (kind, url, publication_date)
);

create index if not exists imt_publications_date_idx
    on imt_publications (publication_date desc, kind);

create table if not exists imt_reports (
    id uuid primary key default gen_random_uuid(),
    kind text not null default 'catalog_analysis',
    content text not null,
    created_at timestamptz not null default now()
);

create index if not exists imt_reports_kind_idx on imt_reports (kind, created_at desc);
