-- Mailbot reliability + polish migration (run once in Supabase SQL editor)

-- Certification category + remind-again
alter table assignments drop constraint if exists assignments_category_check;
alter table assignments add constraint assignments_category_check
  check (category in ('deadline', 'placement', 'certification', 'admin', 'other'));

alter table assignments add column if not exists remind_at timestamptz;
create index if not exists idx_assignments_remind_at on assignments (remind_at)
  where remind_at is not null and status = 'open';

-- Dedupe: one open assignment per Gmail message
-- (Existing duplicates must be closed before the unique index can be created.)
with ranked as (
  select id,
         row_number() over (
           partition by gmail_message_id
           order by created_at desc nulls last, id desc
         ) as rn
  from assignments
  where status = 'open'
    and gmail_message_id is not null
)
update assignments
set status = 'dismissed'
where id in (select id from ranked where rn > 1);

create unique index if not exists idx_assignments_gmail_message_open
  on assignments (gmail_message_id)
  where gmail_message_id is not null and status = 'open';

-- Retry queue when LLM classification fails transiently
create table if not exists pending_emails (
    message_id text primary key,
    attempts int not null default 0,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_pending_emails_updated on pending_emails (updated_at);
