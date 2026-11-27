-- 0003_outreach.sql
-- Reference copy. The live migration is applied by the Lovable agent; keep both in sync.
--
-- Portal side of the outbound system: Smartlead sends, Edge Functions ingest webhooks,
-- Claude classifies replies, assign_rep() routes them, reps approve drafts in the portal.
-- Additive and re-runnable: every statement is "if not exists", "create or replace" or guarded
-- by a do-block. Column names were checked against the live schema on 2026-11-27.
--
-- Facts about the live schema this file relies on (introspected, not assumed):
--   * RLS pattern: a RESTRICTIVE "approved users only" policy (is_approved(auth.uid())) on every
--     table, plus PERMISSIVE per-command policies using is_manager(auth.uid()) and
--     has_role(auth.uid(), 'admin'::app_role). New tables below follow the same pattern.
--   * contacts already has unique index contacts_email_uq on lower(email) where email is not null.
--   * webhook_events already has processed_at, error and unique (provider, idempotency_key).
--   * email_drafts.status has a check constraint (draft, pending_approval, approved, rejected, sent);
--     it is widened here to the outreach statuses.
--   * meetings "held" = status 'completed' (enum meeting_status) or outcome ilike 'held%'.
--   * tasks has no lead/contact link; lead_domain, contact_id and source are added for routing.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------------------------
-- 1. Enum public.disposition (values must equal moh_leadgen.outreach.models.Disposition)
-- ---------------------------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'public' and t.typname = 'disposition') then
    create type public.disposition as enum (
      'interested', 'meeting_requested', 'meeting_booked', 'question_objection', 'maybe_later',
      'not_interested', 'wrong_person_referral', 'out_of_office', 'auto_reply', 'unsubscribe',
      'bounce', 'unclear'
    );
  end if;
end $$;

-- ---------------------------------------------------------------------------------------------
-- 2. Columns on existing tables
-- ---------------------------------------------------------------------------------------------
alter table public.leads add column if not exists segment text;
alter table public.leads add column if not exists disposition public.disposition;
alter table public.leads add column if not exists enrolled_at timestamptz;
alter table public.leads add column if not exists last_outreach_at timestamptz;
alter table public.leads add column if not exists suppressed_at timestamptz;
alter table public.leads add column if not exists suppression_reason text;
alter table public.leads add column if not exists smartlead_campaign_id bigint;
alter table public.leads add column if not exists first_touch_source text;
alter table public.leads add column if not exists meta_lead_id text;
create unique index if not exists leads_meta_lead_id_uq on public.leads (meta_lead_id)
  where meta_lead_id is not null;
create index if not exists leads_segment_idx on public.leads (segment);
create index if not exists leads_disposition_idx on public.leads (disposition);

alter table public.contacts add column if not exists lead_domain text;
alter table public.contacts add column if not exists email_status text;
alter table public.contacts add column if not exists is_decision_maker boolean;
alter table public.contacts add column if not exists linkedin text;
alter table public.contacts add column if not exists smartlead_lead_id bigint;
alter table public.contacts add column if not exists smartlead_campaign_id bigint;
alter table public.contacts add column if not exists routed_to uuid references public.profiles (id) on delete set null;
alter table public.contacts add column if not exists snooze_until date;
alter table public.contacts add column if not exists unsubscribed_at timestamptz;
alter table public.contacts add column if not exists bounced_at timestamptz;
alter table public.contacts add column if not exists utm_source text;
alter table public.contacts add column if not exists utm_campaign text;
alter table public.contacts add column if not exists utm_content text;
alter table public.contacts add column if not exists fbclid text;
alter table public.contacts add column if not exists meta_lead_id text;
-- Same name as the existing live index, so this is a no-op on the portal and creates it elsewhere.
create unique index if not exists contacts_email_uq on public.contacts (lower(email)) where email is not null;
create index if not exists contacts_lead_domain_idx on public.contacts (lead_domain);
create index if not exists contacts_smartlead_lead_id_idx on public.contacts (smartlead_lead_id);

alter table public.email_drafts add column if not exists lead_domain text;
alter table public.email_drafts add column if not exists variant_id uuid;
alter table public.email_drafts add column if not exists smartlead_campaign_id bigint;
alter table public.email_drafts add column if not exists smartlead_lead_id bigint;
alter table public.email_drafts add column if not exists provider_message_id text;
alter table public.email_drafts add column if not exists direction text default 'outbound';
alter table public.email_drafts add column if not exists opened_at timestamptz;
alter table public.email_drafts add column if not exists bounced_at timestamptz;
alter table public.email_drafts add column if not exists clicked_at timestamptz;
-- Smartlead email_stats_id of the message a portal reply answers (needed by reply-email-thread).
alter table public.email_drafts add column if not exists smartlead_stats_id text;
-- Links a pending_approval reply draft to the classification that produced it.
alter table public.email_drafts add column if not exists reply_classification_id uuid;
create unique index if not exists email_drafts_smartlead_step_uq
  on public.email_drafts (smartlead_lead_id, sequence_step, direction)
  where smartlead_lead_id is not null;
create index if not exists email_drafts_variant_idx on public.email_drafts (variant_id);
create index if not exists email_drafts_status_idx on public.email_drafts (status);

-- Widen the existing status check to the outreach lifecycle (keeps 'draft').
alter table public.email_drafts drop constraint if exists email_drafts_status_check;
alter table public.email_drafts add constraint email_drafts_status_check check (status = any (array[
  'draft', 'queued', 'sent', 'delivered', 'opened', 'replied', 'bounced', 'unsubscribed',
  'pending_approval', 'approved', 'rejected'
]));
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'email_drafts_direction_check') then
    alter table public.email_drafts add constraint email_drafts_direction_check
      check (direction in ('outbound', 'inbound', 'reply'));
  end if;
end $$;

alter table public.webhook_events add column if not exists processed_at timestamptz;
alter table public.webhook_events add column if not exists error text;
do $$
begin
  if not exists (
    select 1 from pg_index i
    join pg_class c on c.oid = i.indrelid
    where c.relname = 'webhook_events' and i.indisunique
      and pg_get_indexdef(i.indexrelid) ilike '%(provider, idempotency_key)%'
  ) then
    create unique index webhook_events_provider_idempotency_key_key
      on public.webhook_events (provider, idempotency_key);
  end if;
end $$;
create index if not exists webhook_events_unprocessed_idx on public.webhook_events (received_at)
  where processed_at is null;

alter table public.meetings add column if not exists lead_domain text;
alter table public.meetings add column if not exists source text;
alter table public.meetings add column if not exists booking_uid text;
create unique index if not exists meetings_booking_uid_key on public.meetings (booking_uid);

alter table public.tasks add column if not exists lead_domain text;
alter table public.tasks add column if not exists contact_id uuid references public.contacts (id) on delete set null;
-- e.g. 'reply:<reply_classifications.id>' or 'stuck_webhooks:<date-hour>'; unique so retries never duplicate.
alter table public.tasks add column if not exists source text;
create unique index if not exists tasks_source_uq on public.tasks (source) where source is not null;

-- ---------------------------------------------------------------------------------------------
-- 3. New tables
-- ---------------------------------------------------------------------------------------------
create table if not exists public.suppressions (
  id bigserial primary key,
  email text,
  domain text,
  reason text check (reason in ('unsubscribe', 'bounce', 'complaint', 'not_interested',
                                'customer', 'competitor', 'manual')),
  source text not null,
  expires_at timestamptz,
  created_by uuid references public.profiles (id) on delete set null,
  created_at timestamptz not null default now(),
  check (email is not null or domain is not null)
);
create unique index if not exists suppressions_email_uq on public.suppressions (lower(email))
  where email is not null;
create unique index if not exists suppressions_domain_uq on public.suppressions (lower(domain))
  where email is null and domain is not null;

create table if not exists public.variants (
  id uuid primary key default gen_random_uuid(),
  segment text not null,
  sequence_step smallint not null,
  name text not null,
  angle text,
  subject text,
  body text,
  hypothesis text,
  parent_id uuid references public.variants (id) on delete set null,
  status text not null default 'active' check (status in ('active', 'control', 'retired', 'proposed')),
  smartlead_campaign_id bigint,
  smartlead_variant_label text,
  sends int default 0,
  opens int default 0,
  replies int default 0,
  positive_replies int default 0,
  meetings int default 0,
  alpha numeric default 1,
  beta numeric default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (segment, sequence_step, name)
);

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'email_drafts_variant_id_fkey') then
    alter table public.email_drafts add constraint email_drafts_variant_id_fkey
      foreign key (variant_id) references public.variants (id) on delete set null;
  end if;
end $$;

create table if not exists public.reply_classifications (
  id uuid primary key default gen_random_uuid(),
  webhook_event_id uuid references public.webhook_events (id) on delete set null,
  contact_id uuid references public.contacts (id) on delete set null,
  lead_domain text,
  -- The OUTBOUND email_drafts row that was replied to (drives per-variant attribution).
  email_draft_id uuid references public.email_drafts (id) on delete set null,
  disposition public.disposition not null,
  confidence text,
  summary text,
  objection text,
  snooze_until date,
  referral jsonb,
  requested_times text[],
  draft_reply text,
  contains_phi boolean default false,
  model text,
  routed_to uuid references public.profiles (id) on delete set null,
  created_at timestamptz not null default now()
);
create unique index if not exists reply_classifications_event_uq
  on public.reply_classifications (webhook_event_id) where webhook_event_id is not null;
create index if not exists reply_classifications_disposition_idx on public.reply_classifications (disposition);
create index if not exists reply_classifications_routed_idx on public.reply_classifications (routed_to);

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'email_drafts_reply_classification_id_fkey') then
    alter table public.email_drafts add constraint email_drafts_reply_classification_id_fkey
      foreign key (reply_classification_id) references public.reply_classifications (id) on delete set null;
  end if;
end $$;

create table if not exists public.rep_rules (
  id serial primary key,
  priority int not null,
  name text not null,
  condition jsonb not null,
  rep_id uuid references public.profiles (id) on delete set null,
  active boolean default true
);
create unique index if not exists rep_rules_name_uq on public.rep_rules (name);

-- updated_at trigger for variants (reuses the portal's existing leads_set_updated_at()).
drop trigger if exists variants_set_updated_at on public.variants;
create trigger variants_set_updated_at before update on public.variants
  for each row execute function public.leads_set_updated_at();

-- ---------------------------------------------------------------------------------------------
-- 4. RLS, in the portal's existing style
-- ---------------------------------------------------------------------------------------------
alter table public.suppressions enable row level security;
alter table public.variants enable row level security;
alter table public.reply_classifications enable row level security;
alter table public.rep_rules enable row level security;

do $$
declare
  t text;
begin
  foreach t in array array['suppressions', 'variants', 'reply_classifications', 'rep_rules'] loop
    if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = t
                   and policyname = 'approved users only') then
      execute format(
        'create policy "approved users only" on public.%I as restrictive for all to authenticated '
        'using (is_approved(auth.uid())) with check (is_approved(auth.uid()))', t);
    end if;
  end loop;
end $$;

-- suppressions: every approved user reads and adds; managers delete (compliance list).
drop policy if exists "sp read" on public.suppressions;
create policy "sp read" on public.suppressions for select to authenticated using (is_approved(auth.uid()));
drop policy if exists "sp insert" on public.suppressions;
create policy "sp insert" on public.suppressions for insert to authenticated
  with check (is_approved(auth.uid()) and (created_by is null or created_by = auth.uid()));
drop policy if exists "sp delete" on public.suppressions;
create policy "sp delete" on public.suppressions for delete to authenticated using (is_manager(auth.uid()));

-- variants: everyone reads; managers write (copy changes go through review).
drop policy if exists "va read" on public.variants;
create policy "va read" on public.variants for select to authenticated using (is_approved(auth.uid()));
drop policy if exists "va insert" on public.variants;
create policy "va insert" on public.variants for insert to authenticated with check (is_manager(auth.uid()));
drop policy if exists "va update" on public.variants;
create policy "va update" on public.variants for update to authenticated using (is_manager(auth.uid()));
drop policy if exists "va delete" on public.variants;
create policy "va delete" on public.variants for delete to authenticated using (is_manager(auth.uid()));

-- reply_classifications: written by the service role; reps read their own or unrouted, managers all.
drop policy if exists "rc read" on public.reply_classifications;
create policy "rc read" on public.reply_classifications for select to authenticated
  using (routed_to is null or routed_to = auth.uid() or is_manager(auth.uid()));
drop policy if exists "rc update" on public.reply_classifications;
create policy "rc update" on public.reply_classifications for update to authenticated
  using (routed_to = auth.uid() or is_manager(auth.uid()));

-- rep_rules: everyone reads; admins write (same as app_settings).
drop policy if exists "rr read" on public.rep_rules;
create policy "rr read" on public.rep_rules for select to authenticated using (is_approved(auth.uid()));
drop policy if exists "rr insert" on public.rep_rules;
create policy "rr insert" on public.rep_rules for insert to authenticated
  with check (has_role(auth.uid(), 'admin'::app_role));
drop policy if exists "rr update" on public.rep_rules;
create policy "rr update" on public.rep_rules for update to authenticated
  using (has_role(auth.uid(), 'admin'::app_role));
drop policy if exists "rr delete" on public.rep_rules;
create policy "rr delete" on public.rep_rules for delete to authenticated
  using (has_role(auth.uid(), 'admin'::app_role));

-- email_drafts: reply drafts are inserted by the service role (author_id null), so the existing
-- author/manager policies would hide them from the rep they were routed to. These two permissive
-- policies let that rep read and approve/edit them; nothing else changes.
drop policy if exists "ed read routed" on public.email_drafts;
create policy "ed read routed" on public.email_drafts for select to authenticated
  using (exists (select 1 from public.reply_classifications rc
                  where rc.id = email_drafts.reply_classification_id and rc.routed_to = auth.uid()));
drop policy if exists "ed update routed" on public.email_drafts;
create policy "ed update routed" on public.email_drafts for update to authenticated
  using (exists (select 1 from public.reply_classifications rc
                  where rc.id = email_drafts.reply_classification_id and rc.routed_to = auth.uid()));

-- ---------------------------------------------------------------------------------------------
-- 5. Routing: rep_rules condition mini-language + assign_rep()
--    {"all": [{"field": F, "op": OP, "value": V}, ...]}
--    F  in disposition, score, legitscript_status, vertical, competitor_platform, source, segment
--    OP in eq, in, gte, lte, not_null, is_null
--    eq / in compare lower-cased text; gte / lte compare numerically.
--    An empty "all" list matches. Unknown field or op never matches.
--    A rule whose rep_id is null is skipped (so a seeded rule without a rep falls through).
--    moh_leadgen/outreach/routing.py must implement exactly these semantics.
-- ---------------------------------------------------------------------------------------------
create or replace function public.rep_rule_matches(p_condition jsonb, p_facts jsonb)
returns boolean
language plpgsql
immutable
set search_path = public
as $$
declare
  clause jsonb;
  op text;
  actual text;
  expected jsonb;
begin
  for clause in select * from jsonb_array_elements(coalesce(p_condition -> 'all', '[]'::jsonb)) loop
    op := clause ->> 'op';
    actual := p_facts ->> (clause ->> 'field');
    expected := clause -> 'value';
    if (clause ->> 'field') is null or (clause ->> 'field') not in ('disposition', 'score', 'legitscript_status', 'vertical',
                                    'competitor_platform', 'source', 'segment') then
      return false;
    end if;
    if op = 'not_null' then
      if actual is null or actual = '' then return false; end if;
    elsif op = 'is_null' then
      if actual is not null and actual <> '' then return false; end if;
    elsif actual is null then
      return false;
    elsif op = 'eq' then
      if lower(actual) <> lower(expected #>> '{}') then return false; end if;
    elsif op = 'in' then
      if not exists (select 1 from jsonb_array_elements_text(expected) v where lower(v) = lower(actual)) then
        return false;
      end if;
    elsif op = 'gte' then
      if actual !~ '^-?[0-9.]+$' or actual::numeric < (expected #>> '{}')::numeric then return false; end if;
    elsif op = 'lte' then
      if actual !~ '^-?[0-9.]+$' or actual::numeric > (expected #>> '{}')::numeric then return false; end if;
    else
      return false;
    end if;
  end loop;
  return true;
end;
$$;

create or replace function public.assign_rep(p_lead_domain text, p_disposition public.disposition)
returns uuid
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  facts jsonb;
  rule record;
  rep uuid;
  rep_ids uuid[];
begin
  select jsonb_build_object(
           'disposition', coalesce(p_disposition::text, l.disposition::text),
           'score', l.score,
           'legitscript_status', l.legitscript_status,
           'vertical', l.vertical,
           'competitor_platform', l.competitor_platform,
           'source', l.source,
           'segment', l.segment)
    into facts
    from leads l
   where lower(l.domain) = lower(p_lead_domain);
  if facts is null then
    facts := jsonb_build_object('disposition', p_disposition::text);
  end if;

  for rule in
    select r.rep_id, r.condition from rep_rules r
     where coalesce(r.active, true) and r.rep_id is not null
     order by r.priority, r.id
  loop
    if rep_rule_matches(rule.condition, facts) then
      return rule.rep_id;
    end if;
  end loop;

  -- Fallback: the active rep (app_settings.outreach.reps) with the fewest open tasks created in
  -- the last 7 days. If no reps are configured yet, fall back to active admins.
  select array_agg((r ->> 'profile_id')::uuid)
    into rep_ids
    from app_settings s, jsonb_array_elements(coalesce(s.value -> 'reps', '[]'::jsonb)) r
   where s.key = 'outreach' and coalesce(r ->> 'profile_id', '') <> '';
  if rep_ids is null then
    select array_agg(p.id) into rep_ids
      from profiles p join user_roles ur on ur.user_id = p.id
     where p.active and ur.role = 'admin'::app_role;
  end if;

  select p.id into rep
    from profiles p
    left join tasks t on t.assignee_id = p.id and not t.done and t.created_at > now() - interval '7 days'
   where p.id = any (rep_ids) and p.active
   group by p.id
   order by count(t.id), p.id
   limit 1;
  return rep;
end;
$$;

revoke all on function public.assign_rep(text, public.disposition) from public, anon;
grant execute on function public.assign_rep(text, public.disposition) to authenticated, service_role;

-- ---------------------------------------------------------------------------------------------
-- 6. Funnel view and variant posterior refresh
-- ---------------------------------------------------------------------------------------------
-- Attribution rules (keep in sync with outreach learn):
--   sends  = outbound email_drafts rows with sent_at set
--   opens  = those with opened_at set; replies = those with replied_at set
--   positive_replies = reply_classifications whose email_draft_id points at the outbound row and
--                      whose disposition is interested / meeting_requested / meeting_booked
--   meetings = meetings whose contact's most recent outbound send (before the meeting was
--              created) carried that variant; held = those with status 'completed' or outcome 'held%'
create or replace view public.outreach_funnel
with (security_invoker = true) as
with sent as (
  select d.id, coalesce(v.segment, l.segment) as segment, d.variant_id, d.sequence_step,
         d.opened_at, d.replied_at
    from email_drafts d
    left join variants v on v.id = d.variant_id
    left join leads l on lower(l.domain) = lower(d.lead_domain)
   where d.direction = 'outbound' and d.sent_at is not null
),
positives as (
  select rc.email_draft_id, count(*) as n
    from reply_classifications rc
   where rc.disposition in ('interested', 'meeting_requested', 'meeting_booked')
   group by rc.email_draft_id
),
meeting_variant as (
  select m.id as meeting_id, m.status, m.outcome, last_send.variant_id, last_send.sequence_step
    from meetings m
    join lateral (
      select d.variant_id, d.sequence_step
        from email_drafts d
       where d.contact_id = m.contact_id and d.direction = 'outbound'
         and d.sent_at is not null and d.sent_at <= m.created_at
       order by d.sent_at desc
       limit 1
    ) last_send on true
)
select s.segment,
       s.variant_id,
       s.sequence_step,
       count(*)::int as sends,
       count(s.opened_at)::int as opens,
       count(s.replied_at)::int as replies,
       coalesce(sum(p.n), 0)::int as positive_replies,
       (select count(*) from meeting_variant mv
         where mv.variant_id is not distinct from s.variant_id
           and mv.sequence_step is not distinct from s.sequence_step)::int as meetings,
       (select count(*) from meeting_variant mv
         where mv.variant_id is not distinct from s.variant_id
           and mv.sequence_step is not distinct from s.sequence_step
           and (mv.status = 'completed' or mv.outcome ilike 'held%'))::int as held
  from sent s
  left join positives p on p.email_draft_id = s.id
 group by s.segment, s.variant_id, s.sequence_step;

-- Beta(1 + positives, 1 + sends - positives), the same posterior the Thompson sampler uses.
create or replace function public.outreach_refresh_variants()
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
  n int;
begin
  with f as (
    select variant_id, sum(sends) as sends, sum(opens) as opens, sum(replies) as replies,
           sum(positive_replies) as positive_replies, sum(meetings) as meetings
      from outreach_funnel
     where variant_id is not null
     group by variant_id
  )
  update variants v
     set sends = coalesce(f.sends, 0),
         opens = coalesce(f.opens, 0),
         replies = coalesce(f.replies, 0),
         positive_replies = coalesce(f.positive_replies, 0),
         meetings = coalesce(f.meetings, 0),
         alpha = 1 + coalesce(f.positive_replies, 0),
         beta = 1 + greatest(coalesce(f.sends, 0) - coalesce(f.positive_replies, 0), 0)
    from variants v2
    left join f on f.variant_id = v2.id
   where v.id = v2.id;
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function public.outreach_refresh_variants() from public, anon;
grant execute on function public.outreach_refresh_variants() to authenticated, service_role;

-- Called every 15 minutes by pg_cron. Opens at most one task per hour for Shubh when webhook
-- events have sat unprocessed for over 15 minutes (the Python sweep retries them; this is the alarm).
create or replace function public.outreach_flag_stuck_webhooks()
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
  stuck int;
  shubh uuid;
begin
  select count(*) into stuck from webhook_events
   where processed_at is null and received_at < now() - interval '15 minutes';
  if stuck = 0 then
    return 0;
  end if;
  select id into shubh from profiles where lower(email) = 'shubh@myorbithealth.com';
  insert into tasks (title, priority, assignee_id, due_at, source)
  values (format('%s webhook event(s) unprocessed for over 15 minutes - check webhook_events.error', stuck),
          'high', shubh, now() + interval '1 hour',
          'stuck_webhooks:' || to_char(now() at time zone 'utc', 'YYYY-MM-DD"T"HH24'))
  on conflict (source) where source is not null do nothing;
  return stuck;
end;
$$;

revoke all on function public.outreach_flag_stuck_webhooks() from public, anon, authenticated;
grant execute on function public.outreach_flag_stuck_webhooks() to service_role;

-- ---------------------------------------------------------------------------------------------
-- 7. Seeds
-- ---------------------------------------------------------------------------------------------
-- The mini-language has only "all", so each OR in the routing policy becomes its own rule.
-- Profiles matched on 2026-11-27: shubh@myorbithealth.com (Shubh Dhruv, admin),
-- devang@myorbithealth.com (Devang Dhruv, sales_rep). No profile for Stacy exists yet, so the
-- Stacy rules are seeded with rep_id null (skipped by assign_rep) until her profile is created.
insert into public.rep_rules (priority, name, condition, rep_id) values
  (10, 'shubh_question_objection',
   '{"all":[{"field":"disposition","op":"in","value":["question_objection"]}]}',
   (select id from public.profiles where lower(email) = 'shubh@myorbithealth.com')),
  (11, 'shubh_high_score',
   '{"all":[{"field":"score","op":"gte","value":80}]}',
   (select id from public.profiles where lower(email) = 'shubh@myorbithealth.com')),
  (12, 'shubh_legitscript_seal',
   '{"all":[{"field":"legitscript_status","op":"eq","value":"seal_detected"}]}',
   (select id from public.profiles where lower(email) = 'shubh@myorbithealth.com')),
  (20, 'divang_dtc_competitor',
   '{"all":[{"field":"vertical","op":"in","value":["weight_loss_glp1","mens_health_trt","peptides_longevity","online_pharmacy","telehealth_general"]},{"field":"competitor_platform","op":"not_null"}]}',
   (select id from public.profiles where lower(email) = 'devang@myorbithealth.com')),
  (30, 'stacy_local_verticals',
   '{"all":[{"field":"vertical","op":"in","value":["med_spa","fitness_gym","primary_care","womens_health","addiction"]}]}',
   (select id from public.profiles where lower(full_name) like 'stacy%' limit 1)),
  (31, 'stacy_local_business',
   '{"all":[{"field":"source","op":"eq","value":"local_business"}]}',
   (select id from public.profiles where lower(full_name) like 'stacy%' limit 1))
on conflict (name) do nothing;

insert into public.app_settings (key, value) values
  ('outreach', jsonb_build_object(
     'reps', '[]'::jsonb,
     'daily_cap', 300,
     'company_name', 'MyOrbit Health',
     'postal_address', 'TO BE CONFIRMED',
     'reply_sla_minutes', 60,
     'ramp', '[]'::jsonb))
on conflict (key) do nothing;
