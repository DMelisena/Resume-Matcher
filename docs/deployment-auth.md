# Authentication deployment

Set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` in frontend deployment environment. Never expose Supabase secret/service-role keys.

Apply `supabase/migrations/20260919000000_account_isolation.sql` to project database. Enable email auth in Supabase Authentication.

In Authentication → URL Configuration, set Site URL to deployed frontend URL and add callback URL `{DEPLOYED_FRONTEND_URL}/auth/callback`. Add local callback URL when developing.

The frontend's Next.js `middleware.ts` gates page navigation on an authenticated Supabase session.

**Known limitation — not account-isolated yet:** `supabase/migrations/20260919000000_account_isolation.sql` defines RLS policies for a `public.resumes`/`jobs`/`applications` schema on the Supabase Postgres project, scoped with `auth.uid()`. That schema is separate from the data the app actually serves: the FastAPI backend persists resumes/jobs/applications in its own SQLite database (`apps/backend/app/database.py`) and does not yet authenticate requests or filter by `user_id`, so the Supabase RLS policies do not protect the live API. Until the FastAPI routers verify the Supabase session and scope every query by `user_id` (and the per-user "master resume" invariant replaces today's single global master), any caller of the backend API can read or write any user's resumes, jobs, and applications.
