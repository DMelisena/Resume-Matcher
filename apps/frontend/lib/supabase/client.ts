/**
 * Browser Supabase client — the only place email/password login/signup and
 * session persistence happen. Uses the publishable (anon) key only.
 */
'use client';

import { createBrowserClient } from '@supabase/ssr';
import { SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL } from './env';

export function createClient() {
  return createBrowserClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
}
