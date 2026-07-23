-- Pending email retry metadata for delivery-only retries and mark-failed recovery
alter table pending_emails add column if not exists classified_result jsonb;
alter table pending_emails add column if not exists already_alerted boolean not null default false;
alter table pending_emails add column if not exists delivered_channels jsonb not null default '{}'::jsonb;
