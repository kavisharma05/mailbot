-- Durable delivery leases and reminder dedupe (run once in Supabase SQL editor)

create table if not exists delivery_claims (
    claim_key text primary key,
    kind text not null,
    claim_date date not null,
    claimed_at timestamptz not null default now(),
    expires_at timestamptz not null,
    completed_at timestamptz
);

create index if not exists idx_delivery_claims_kind_date
    on delivery_claims (kind, claim_date);

-- Prevent duplicate scheduled reminder tiers per assignment
create unique index if not exists idx_scheduled_reminders_assignment_tier
    on scheduled_reminders (assignment_id, tier)
    where status = 'pending';

-- Pending email dead-letter tracking
alter table pending_emails add column if not exists dead_letter boolean not null default false;
