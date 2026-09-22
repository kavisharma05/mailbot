-- Run once in the Supabase SQL editor.
-- watch_state, digest_state, and oauth_tokens were locked to a single row (id = 1).
-- A second mailbox needs its own row, and Gmail token upserts need a real unique key.

alter table watch_state drop constraint if exists watch_state_id_check;
alter table digest_state drop constraint if exists digest_state_id_check;
alter table oauth_tokens drop constraint if exists oauth_tokens_id_check;

update watch_state
set user_id = '00000000-0000-4000-8000-000000000001'::uuid
where user_id is null;

update digest_state
set user_id = '00000000-0000-4000-8000-000000000001'::uuid
where user_id is null;

update oauth_tokens
set user_id = '00000000-0000-4000-8000-000000000001'::uuid
where user_id is null;

create sequence if not exists watch_state_id_seq;
select setval(
    'watch_state_id_seq',
    coalesce((select max(id) from watch_state), 1)
);
alter table watch_state alter column id set default nextval('watch_state_id_seq');
alter sequence watch_state_id_seq owned by watch_state.id;

create sequence if not exists digest_state_id_seq;
select setval(
    'digest_state_id_seq',
    coalesce((select max(id) from digest_state), 1)
);
alter table digest_state alter column id set default nextval('digest_state_id_seq');
alter sequence digest_state_id_seq owned by digest_state.id;

create sequence if not exists oauth_tokens_id_seq;
select setval(
    'oauth_tokens_id_seq',
    coalesce((select max(id) from oauth_tokens), 1)
);
alter table oauth_tokens alter column id set default nextval('oauth_tokens_id_seq');
alter sequence oauth_tokens_id_seq owned by oauth_tokens.id;

create unique index if not exists idx_watch_state_user_unique on watch_state (user_id);
create unique index if not exists idx_digest_state_user_unique on digest_state (user_id);

drop index if exists idx_oauth_tokens_user_provider;
create unique index if not exists idx_oauth_tokens_user_provider
    on oauth_tokens (user_id, provider);
