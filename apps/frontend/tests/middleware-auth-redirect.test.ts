import { describe, expect, it, vi, beforeEach } from 'vitest';
import { NextRequest } from 'next/server';

// Resolved per-test via `mockUser`; mirrors the shape middleware reads off
// `supabase.auth.getUser()`.
let mockUser: { id: string } | null = null;

vi.mock('@supabase/ssr', () => ({
  createServerClient: () => ({
    auth: {
      getUser: async () => ({ data: { user: mockUser } }),
    },
  }),
}));

vi.mock('@/lib/supabase/env', () => ({
  SUPABASE_URL: 'https://example.supabase.co',
  SUPABASE_PUBLISHABLE_KEY: 'publishable-key',
}));

// Imported after the mocks above so middleware picks them up.
const { middleware } = await import('@/middleware');

function makeRequest(pathname: string): NextRequest {
  return new NextRequest(new URL(pathname, 'https://resumematcher.example'));
}

describe('middleware auth routing', () => {
  beforeEach(() => {
    mockUser = null;
  });

  it('redirects an authenticated visitor hitting / straight to /dashboard', async () => {
    mockUser = { id: 'user-1' };
    const response = await middleware(makeRequest('/'));

    expect(response.status).toBe(307);
    expect(new URL(response.headers.get('location')!).pathname).toBe('/dashboard');
  });

  it('lets an unauthenticated visitor see the landing page at /', async () => {
    mockUser = null;
    const response = await middleware(makeRequest('/'));

    expect(response.status).not.toBe(307);
  });

  it('redirects an unauthenticated visitor hitting a protected route to /login', async () => {
    mockUser = null;
    const response = await middleware(makeRequest('/dashboard'));

    expect(response.status).toBe(307);
    const location = new URL(response.headers.get('location')!);
    expect(location.pathname).toBe('/login');
    expect(location.searchParams.get('next')).toBe('/dashboard');
  });

  it('lets an authenticated visitor reach a protected route without a redirect', async () => {
    mockUser = { id: 'user-1' };
    const response = await middleware(makeRequest('/dashboard'));

    expect(response.status).not.toBe(307);
  });

  it('redirects an authenticated visitor away from /login to /dashboard', async () => {
    mockUser = { id: 'user-1' };
    const response = await middleware(makeRequest('/login'));

    expect(response.status).toBe(307);
    expect(new URL(response.headers.get('location')!).pathname).toBe('/dashboard');
  });
});
