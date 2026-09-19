create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

alter table public.resumes add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table public.jobs add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table public.applications add column if not exists user_id uuid references auth.users(id) on delete cascade;

create index if not exists resumes_user_id_idx on public.resumes(user_id);
create index if not exists jobs_user_id_idx on public.jobs(user_id);
create index if not exists applications_user_id_idx on public.applications(user_id);

alter table public.profiles enable row level security;
alter table public.resumes enable row level security;
alter table public.jobs enable row level security;
alter table public.applications enable row level security;

create policy "profiles own rows" on public.profiles for all to authenticated
using ((select auth.uid()) = id) with check ((select auth.uid()) = id);
create policy "resumes own rows" on public.resumes for all to authenticated
using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "jobs own rows" on public.jobs for all to authenticated
using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "applications own rows" on public.applications for all to authenticated
using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
