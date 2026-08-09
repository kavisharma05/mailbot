-- Store original mail sender/subject on assignments (run once in Supabase SQL editor).
-- Enables: reminder alerts showing "From/Subject", and one-tap Reply on owner alerts.

alter table assignments add column if not exists sender text;
alter table assignments add column if not exists subject text;
