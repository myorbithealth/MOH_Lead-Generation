-- Lead-generation table for the MyOrbit Health Sales Portal (Lovable project fa5411e5-...).
-- Applied to the portal's Supabase project on 2026-11-03. Re-runnable.

create table if not exists public.leads (
  id                    uuid primary key default gen_random_uuid(),
  domain                text not null unique,
  website               text,
  company_name          text,
  source                text not null,                  -- legitscript | competitor_platform | local_business | manual
  discovered_via        text,
  source_detail         jsonb not null default '{}'::jsonb,
  vertical              text,
  description           text,
  legitscript_status    text not null default 'unchecked',  -- seal_detected | mentioned | not_found | unchecked
  legitscript_evidence  jsonb not null default '{}'::jsonb,
  owner_name            text,
  owner_title           text,
  owner_email           text,
  owner_linkedin        text,
  people                jsonb not null default '[]'::jsonb,
  emails                jsonb not null default '[]'::jsonb,
  phones                jsonb not null default '[]'::jsonb,
  primary_email         text,
  primary_phone         text,
  address               text,
  city                  text,
  state                 text,
  country               text,
  socials               jsonb not null default '{}'::jsonb,
  tech_stack            jsonb not null default '[]'::jsonb,
  competitor_platform   text,
  domain_created        text,
  has_mx                boolean,
  score                 integer not null default 0,
  score_reasons         jsonb not null default '[]'::jsonb,
  status                text not null default 'new',    -- new | reviewed | contacted | meeting_booked | promoted | disqualified
  booking_link          text,
  notes                 text,
  owner_id              uuid references public.profiles(id),
  company_id            uuid references public.companies(id),
  contact_id            uuid references public.contacts(id),
  deal_id               uuid references public.deals(id),
  last_enriched_at      timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create index if not exists leads_score_idx on public.leads(score desc);
create index if not exists leads_status_idx on public.leads(status);
create index if not exists leads_source_idx on public.leads(source);
create index if not exists leads_vertical_idx on public.leads(vertical);

create or replace function public.leads_set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists leads_updated_at on public.leads;
create trigger leads_updated_at before update on public.leads
  for each row execute function public.leads_set_updated_at();

-- Row level security: approved portal users read; reps update status/notes/owner; managers delete.
-- The scraper writes with the service-role key, which bypasses RLS.
alter table public.leads enable row level security;

drop policy if exists "leads read" on public.leads;
create policy "leads read" on public.leads for select to authenticated
  using (public.is_approved(auth.uid()));

drop policy if exists "leads update" on public.leads;
create policy "leads update" on public.leads for update to authenticated
  using (public.is_approved(auth.uid()))
  with check (public.is_approved(auth.uid()));

drop policy if exists "leads insert" on public.leads;
create policy "leads insert" on public.leads for insert to authenticated
  with check (public.is_approved(auth.uid()));

drop policy if exists "leads delete" on public.leads;
create policy "leads delete" on public.leads for delete to authenticated
  using (public.is_manager(auth.uid()));

-- Promote a lead into the CRM proper: company + contact + deal (stage "new"), linked back.
create or replace function public.promote_lead(_domain text, _owner uuid default null)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  l       public.leads%rowtype;
  owner   uuid := coalesce(_owner, auth.uid());
  co_id   uuid;
  ct_id   uuid;
  dl_id   uuid;
  fn      text;
  ln      text;
begin
  select * into l from public.leads where domain = _domain for update;
  if not found then
    raise exception 'lead % not found', _domain;
  end if;
  if l.company_id is not null then
    return jsonb_build_object('company_id', l.company_id, 'contact_id', l.contact_id, 'deal_id', l.deal_id, 'already_promoted', true);
  end if;

  select id into co_id from public.companies where website ilike '%' || l.domain || '%' limit 1;
  if co_id is null then
    insert into public.companies(name, industry, website, owner_id)
    values (coalesce(l.company_name, l.domain), coalesce(l.vertical, l.source), coalesce(l.website, 'https://' || l.domain), owner)
    returning id into co_id;
  end if;

  if l.owner_name is not null or l.owner_email is not null or l.primary_email is not null then
    fn := split_part(coalesce(l.owner_name, ''), ' ', 1);
    ln := nullif(btrim(substr(coalesce(l.owner_name, ''), length(fn) + 1)), '');
    insert into public.contacts(company_id, first_name, last_name, email, phone, title, owner_id)
    values (co_id, nullif(fn, ''), ln, coalesce(l.owner_email, l.primary_email), l.primary_phone, l.owner_title, owner)
    returning id into ct_id;
  end if;

  insert into public.deals(title, company_id, contact_id, stage, owner_id, source)
  values (coalesce(l.company_name, l.domain) || ' - white-label telehealth', co_id, ct_id, 'new', owner, 'Lead Gen: ' || l.source)
  returning id into dl_id;

  insert into public.activities(kind, body, company_id, contact_id, deal_id, actor_id)
  values ('note',
          format('Promoted from lead gen (%s). Score %s. LegitScript: %s. Booking link: %s', l.source, l.score, l.legitscript_status, coalesce(l.booking_link, '-')),
          co_id, ct_id, dl_id, owner);

  update public.leads
     set status = 'promoted', company_id = co_id, contact_id = ct_id, deal_id = dl_id, owner_id = coalesce(l.owner_id, owner)
   where id = l.id;

  return jsonb_build_object('company_id', co_id, 'contact_id', ct_id, 'deal_id', dl_id, 'already_promoted', false);
end $$;

grant execute on function public.promote_lead(text, uuid) to authenticated, service_role;
