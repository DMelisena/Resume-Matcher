# Authentication deployment

Set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` in frontend deployment environment. Never expose Supabase secret/service-role keys.

Apply `supabase/migrations/20260919000000_account_isolation.sql` to project database. Enable email auth in Supabase Authentication.

In Authentication → URL Configuration, set Site URL to deployed frontend URL and add callback URL `{DEPLOYED_FRONTEND_URL}/auth/callback`. Add local callback URL when developing.

All client data requests must use authenticated Supabase session; database policies scope rows with `auth.uid()`.
