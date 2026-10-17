-- Resume Matcher: per-account data model + Row-Level Security.
--
-- Creates the five user-owned tables the backend's SQLAlchemy facade
-- (apps/backend/app/models.py, app/database.py) maps onto, each with a
-- `user_id` FK into `auth.users` (Supabase's own account table) and RLS
-- policies that make every SELECT/INSERT/UPDATE/DELETE scoped to
-- `auth.uid()`. This is the *authoritative* isolation boundary — the
-- backend's own `WHERE user_id = ...` filtering (app/database.py) is
-- defense-in-depth on top of it, not a substitute for it.
--
-- The app connects to Postgres as `resume_matcher_app` (see
-- apps/backend/app/config.py::database_url — never the service_role key or
-- the `postgres` superuser, both of which bypass RLS entirely), a login role
-- created below that is a *member* of `authenticated` but has no privileges
-- of its own. Each transaction runs `SET LOCAL ROLE authenticated` plus sets
-- `request.jwt.claims` so `auth.uid()` resolves to the verified caller (see
-- apps/backend/app/database.py::Database._user_session). `SET LOCAL` scopes
-- both to the current transaction only, so a connection can never leak one
-- request's identity into the next request that reuses it from the pool.

-- ---------------------------------------------------------------------------
-- Application login role. Created with NO password here (passwords don't
-- belong in a checked-in migration) — set one out-of-band after deploying,
-- e.g. via the Supabase SQL editor or `supabase db execute`:
--   alter role resume_matcher_app with password '<generate-a-strong-secret>';
-- then put the resulting connection string in DATABASE_URL. Rotate by
-- re-running that ALTER ROLE; no migration/deploy needed.
-- ---------------------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'resume_matcher_app') then
        create role resume_matcher_app with login nosuperuser nocreatedb nocreaterole noinherit;
    end if;
end
$$;

grant authenticated to resume_matcher_app;
-- `authenticated` isn't inherited automatically (NOINHERIT above, deliberate
-- — the connection starts with none of authenticated's privileges until it
-- explicitly `SET LOCAL ROLE authenticated` for a request it has verified).
grant usage on schema public to resume_matcher_app, authenticated;

-- ---------------------------------------------------------------------------
-- resumes
-- ---------------------------------------------------------------------------
create table if not exists public.resumes (
    resume_id text primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    content text not null,
    content_type text not null default 'md',
    filename text,
    is_master boolean not null default false,
    parent_id text,
    processed_data jsonb,
    processing_status text not null default 'pending',
    cover_letter text,
    outreach_message text,
    interview_prep text,
    title text,
    original_markdown text,
    created_at text not null,
    updated_at text not null
);

create index if not exists ix_resumes_user_id on public.resumes (user_id);

-- At most one master resume per account.
create unique index if not exists ux_resumes_single_master_per_user
    on public.resumes (user_id)
    where is_master = true;

alter table public.resumes enable row level security;

create policy resumes_select_own on public.resumes
    for select using (auth.uid() = user_id);
create policy resumes_insert_own on public.resumes
    for insert with check (auth.uid() = user_id);
create policy resumes_update_own on public.resumes
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy resumes_delete_own on public.resumes
    for delete using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- jobs
-- ---------------------------------------------------------------------------
create table if not exists public.jobs (
    job_id text primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    content text not null,
    resume_id text,
    created_at text not null,
    metadata_json jsonb not null default '{}'::jsonb
);

create index if not exists ix_jobs_user_id on public.jobs (user_id);

alter table public.jobs enable row level security;

create policy jobs_select_own on public.jobs
    for select using (auth.uid() = user_id);
create policy jobs_insert_own on public.jobs
    for insert with check (auth.uid() = user_id);
create policy jobs_update_own on public.jobs
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy jobs_delete_own on public.jobs
    for delete using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- improvements
-- ---------------------------------------------------------------------------
create table if not exists public.improvements (
    request_id text primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    original_resume_id text not null,
    tailored_resume_id text not null,
    job_id text not null,
    improvements jsonb not null default '[]'::jsonb,
    created_at text not null
);

create index if not exists ix_improvements_user_id on public.improvements (user_id);
create index if not exists ix_improvements_tailored_resume_id
    on public.improvements (tailored_resume_id);

alter table public.improvements enable row level security;

create policy improvements_select_own on public.improvements
    for select using (auth.uid() = user_id);
create policy improvements_insert_own on public.improvements
    for insert with check (auth.uid() = user_id);
create policy improvements_update_own on public.improvements
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy improvements_delete_own on public.improvements
    for delete using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- applications (Kanban tracker)
-- ---------------------------------------------------------------------------
create table if not exists public.applications (
    application_id text primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    job_id text not null,
    resume_id text not null,
    master_resume_id text,
    status text not null default 'applied',
    company text,
    role text,
    applied_at text,
    notes text,
    position integer not null default 0,
    created_at text not null,
    updated_at text not null,
    constraint uq_application_user_job_resume unique (user_id, job_id, resume_id)
);

create index if not exists ix_applications_user_id on public.applications (user_id);
create index if not exists ix_applications_job_id on public.applications (job_id);
create index if not exists ix_applications_resume_id on public.applications (resume_id);
create index if not exists ix_applications_status on public.applications (status);

alter table public.applications enable row level security;

create policy applications_select_own on public.applications
    for select using (auth.uid() = user_id);
create policy applications_insert_own on public.applications
    for insert with check (auth.uid() = user_id);
create policy applications_update_own on public.applications
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy applications_delete_own on public.applications
    for delete using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- api_keys (encrypted LLM provider keys; ciphertext only, per account)
-- ---------------------------------------------------------------------------
create table if not exists public.api_keys (
    id text primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    provider text not null,
    ciphertext text not null,
    updated_at text not null,
    constraint uq_api_keys_user_provider unique (user_id, provider)
);

create index if not exists ix_api_keys_user_id on public.api_keys (user_id);

alter table public.api_keys enable row level security;

create policy api_keys_select_own on public.api_keys
    for select using (auth.uid() = user_id);
create policy api_keys_insert_own on public.api_keys
    for insert with check (auth.uid() = user_id);
create policy api_keys_update_own on public.api_keys
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy api_keys_delete_own on public.api_keys
    for delete using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Grants: the app connects as `authenticated` (never service_role). Grant it
-- exactly the DML these tables need; RLS policies above further restrict rows.
-- `anon` (unauthenticated) gets nothing — every table here is user-owned data.
-- ---------------------------------------------------------------------------
grant select, insert, update, delete on
    public.resumes,
    public.jobs,
    public.improvements,
    public.applications,
    public.api_keys
to authenticated;

revoke all on
    public.resumes,
    public.jobs,
    public.improvements,
    public.applications,
    public.api_keys
from anon;
