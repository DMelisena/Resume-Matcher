# Supabase Auth Migration

Resume Matcher moved from a single-tenant, local-SQLite app to Supabase Auth
(email/password) + Supabase Postgres with Row-Level Security. This is the
authoritative reference for the architecture and for standing up a real
deployment; see [`backend-guide.md`](../architecture/backend-guide.md) for
the rest of the backend and [`frontend-workflow.md`](../architecture/frontend-workflow.md)
for the rest of the frontend.

## Why Postgres, not SQLite + app-level checks

RLS is a Postgres feature; SQLite has no equivalent. Enforcing isolation with
only `WHERE user_id = ...` filters in application code was rejected as the
sole mechanism — a forgotten filter in a future endpoint would leak data with
no backstop. Postgres RLS policies are the **authoritative** boundary here;
the facade's own `user_id` filtering is defense-in-depth on top of it (see
`apps/backend/tests/integration/test_auth_isolation.py::TestRowLevelSecurityAtTheDatabaseLayer`
for a test that proves RLS holds even when a query "forgets" the filter).

## Architecture

- **Frontend** (`apps/frontend`) authenticates directly against Supabase Auth
  using the **publishable (anon) key** — the backend is never in the
  login/signup path and never sees a password.
  - `lib/supabase/{client,server}.ts` — browser/server Supabase clients.
  - `lib/context/auth-context.tsx` — `AuthProvider`/`useAuth()`, session
    state; `getAccessToken()` is what `lib/api/client.ts::apiFetch` (and the
    raw-fetch upload path in `hooks/use-file-upload.ts`) call to attach
    `Authorization: Bearer <token>` to every backend request.
  - `middleware.ts` — refreshes the Supabase session cookie and redirects
    unauthenticated visitors to `/login` for every route except the landing
    page, `/login`, `/signup`, and `/print/*` (server-rendered for PDF
    capture, not account-scoped UI). Signed-in users hitting `/`, `/login`,
    or `/signup` (with or without a trailing slash) are redirected straight
    to `/dashboard`; both redirect branches copy cookies from the response
    `supabase.auth.getUser()` may have refreshed, so a session rotated on
    the same request isn't lost.
  - `app/login/`, `app/signup/` — Swiss-styled email/password forms.
  - `components/common/account-bar.tsx` — persistent auth-state indicator
    rendered in the `(default)` layout on every page: signed-out visitors
    see "Log in", signed-in users see their email + "Sign out".
- **Backend** (`apps/backend`) verifies that token on every request via
  `app/auth.py::get_current_user_id` (a FastAPI dependency), preferring
  Supabase's JWKS endpoint (asymmetric signing) with an optional
  `SUPABASE_JWT_SECRET` HS256 fallback for older projects. It never holds the
  service-role key.
- **Database**: every user-owned table (`resumes`, `jobs`, `improvements`,
  `applications`, `api_keys`) has a `user_id uuid references auth.users(id)`
  column and RLS policies scoping `SELECT`/`INSERT`/`UPDATE`/`DELETE` to
  `auth.uid() = user_id` — see
  `supabase/migrations/20260925165120_user_owned_tables_and_rls.sql`.
  - The app connects as `resume_matcher_app`, a login role with **no
    privileges of its own** — the same migration grants it membership in
    `authenticated` (`NOINHERIT`, so it doesn't hold those privileges by
    default either).
  - Every unit of work (`app/database.py::Database._user_session` /
    `_user_sync_session`) runs `SET LOCAL ROLE authenticated` plus
    `SELECT set_config('request.jwt.claims', ...)` as the first statements of
    its transaction — `SET LOCAL` is transaction-scoped, so this can never
    leak one request's identity into the next on a pooled connection.
  - Master-resume, tracker-position, and API-key uniqueness invariants are
    now **per account** (e.g. `ux_resumes_single_master_per_user`), not
    global.
- **PDF rendering**: `app/pdf.py::render_resume_pdf` forwards the *original*
  caller's bearer token as an `Authorization` header on Chromium's navigation
  to the frontend's `/print/*` route (`app/routers/resumes.py`'s PDF
  endpoints thread it through via `app/auth.py::get_bearer_token`); the print
  page (a server component with no browser session of its own) forwards that
  same header on its own fetch back to the backend. A PDF can never contain
  data the original caller wasn't authorized to see.

## Deployment requirements

1. **Create a Supabase project** (hosted or self-hosted) and note its URL,
   publishable (anon) key, and JWKS URL (`<url>/auth/v1/.well-known/jwks.json`).
2. **Apply the migration**: `supabase db push` (or run the SQL file directly)
   against that project's Postgres.
3. **Set the app connection role's password** (not embedded in the migration —
   see that file's header comment):
   ```sql
   alter role resume_matcher_app with password '<generate-a-strong-secret>';
   ```
4. **Backend env** (`apps/backend/.env`, see `.env.example`):
   - `DATABASE_URL` — direct Postgres connection as `resume_matcher_app`
     (never the service-role/superuser connection).
   - `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`.
   - `SUPABASE_JWT_SECRET` — only if the project still signs tokens
     symmetrically (legacy); leave unset otherwise.
5. **Frontend env** (`apps/frontend/.env.local`, see `.env.sample`):
   - `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` (publishable
     key only — never the service-role key).
6. **Existing single-tenant installs**: the pre-migration SQLite file
   (`apps/backend/data/resume_matcher.db`) is no longer read automatically at
   startup. Back-fill it into the new Postgres for one specific account with:
   ```bash
   uv run python -m app.scripts.migrate_sqlite_to_supabase \
       --sqlite-path data/resume_matcher.db \
       --owner-user-id <the-supabase-auth-user-id-that-should-own-this-data>
   ```
   Idempotent (safe to re-run); does not delete the source file.

## Local development

`supabase start` (Supabase CLI, requires Docker) brings up a full local
stack — Postgres, GoTrue (Auth), Kong — on the ports in `supabase/config.toml`
(shifted off the CLI defaults in this repo to avoid clashing with another
local Supabase project on the same machine; run `supabase status` for the
actual URLs/keys). Apply the migration once, set the app role's password, and
point both apps' env files at it as above.

`apps/backend/tests/conftest.py::isolated_db`/`isolated_db2` create real,
disposable Supabase Auth accounts against that local stack (`user_id` has a
real FK into `auth.users` — a fabricated UUID won't satisfy it) and skip
cleanly if no Postgres is reachable. Override the target with
`TEST_DATABASE_URL` / `TEST_SUPABASE_URL` / `TEST_SUPABASE_SERVICE_ROLE_KEY`.

## Known follow-ups (not blocking, not fixed in this pass)

- LLM provider API keys (`api_keys` table) are now per-account. A pre-existing
  install's global keys are **not** auto-migrated to any account —
  `app/config.py::migrate_legacy_keys()` is now a safe no-op (logs and
  skips); fold them in manually per account after backfilling, or have each
  user re-enter their key in Settings.
- Login/signup page copy is plain English, not run through the `lib/i18n/`
  system — adding the ~15 new keys across all 5 locale files was left out of
  this pass to keep scope contained; do it before treating auth pages as
  fully localized.
- Next.js 16 deprecated the `middleware.ts` convention in favor of `proxy.ts`
  (build-time warning only, not an error) — a rename is a reasonable small
  follow-up.
- A handful of frontend component tests (`resume-wizard-page`,
  `resume-builder-autosave`, `resume-viewer-delete`) fail with
  `TypeError: Cannot read properties of undefined (reading 'clear')` on
  `localStorage.clear()`. Confirmed **pre-existing and unrelated** to this
  migration (reproduces identically on the pre-migration code) — it's a
  Node 26 vs. jsdom/vitest global `localStorage` incompatibility on this
  machine, not a code defect.
