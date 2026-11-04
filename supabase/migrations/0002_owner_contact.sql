-- Decision-maker contact fields populated by people-data providers (Apollo / Lusha / Hunter).
alter table public.leads add column if not exists owner_email_status text;
alter table public.leads add column if not exists owner_phone text;
alter table public.leads add column if not exists email_pattern text;
