/**
 * Supabase project connection details for the browser/server clients.
 *
 * Both values are safe to ship to the client: `NEXT_PUBLIC_SUPABASE_ANON_KEY`
 * is the *publishable* (anon) key, not the service-role key. The service-role
 * key must never appear in any `NEXT_PUBLIC_*` variable or client bundle.
 */
export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? '';
export const SUPABASE_PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? '';

export function assertSupabaseConfigured(): void {
  if (!SUPABASE_URL || !SUPABASE_PUBLISHABLE_KEY) {
    throw new Error(
      'Supabase is not configured: set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY.'
    );
  }
}
