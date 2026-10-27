import { describe, expect, it, vi, beforeEach } from 'vitest';
import { NextRequest } from 'next/server';

// Resolved per-test via `mockUser`; mirrors the shape middleware reads off
// `supabase.auth.getUser()`.
let mockUser: { id: string } | null = null;
// When set, simulates @supabase/ssr refreshing the session during getUser()
// by invoking the `setAll` callback middleware registered with the client.
let mockRefreshedCookie: { name: string; value: string } | null = null;

interface MockServerClientOptions {
  cookies: {
    setAll: (cookies: Array<{ name: string; value: string; options: unknown }>) => void;
  };
}

vi.mock('@supabase/ssr', () => ({
  createServerClient: (_url: string, _key: string, options: MockServerClientOptions) => ({
    auth: {
      getUser: async () => {
        if (mockRefreshedCookie) {
          options.cookies.setAll([{ ...mockRefreshedCookie, options: {} }]);
        }
        return { data: { user: mockUser } };
      },
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
    mockRefreshedCookie = null;
  });

  it('redirects an authenticated visitor hitting / straight to /dashboard', async () => {
    mockUser = { id: 'user-1' };
    const response = await middleware(makeRequest('/'));

    expect(response.status).toBe(307);
    expect(new URL(response.headers.get('location')!).pathname).toBe('/dashboard');
  });

  it('redirects an unauthenticated visitor hitting / to /login', async () => {
    mockUser = null;
    const response = await middleware(makeRequest('/'));

    expect(response.status).toBe(307);
    const location = new URL(response.headers.get('location')!);
    expect(location.pathname).toBe('/login');
    expect(location.searchParams.get('next')).toBe('/');
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

  it('redirects an authenticated visitor away from /login/ (trailing slash) to /dashboard', async () => {
    mockUser = { id: 'user-1' };
    const response = await middleware(makeRequest('/login/'));

    expect(response.status).toBe(307);
    // NextURL re-appends a trailing slash on clone+reassign when the source
    // pathname had one; either form is a successful redirect away from login.
    expect(new URL(response.headers.get('location')!).pathname).toMatch(/^\/dashboard\/?$/);
  });

  it('carries a session cookie refreshed mid-request through the /dashboard redirect', async () => {
    mockUser = { id: 'user-1' };
    mockRefreshedCookie = { name: 'sb-access-token', value: 'rotated-token' };
    const response = await middleware(makeRequest('/'));

    expect(response.status).toBe(307);
    expect(new URL(response.headers.get('location')!).pathname).toBe('/dashboard');
    expect(response.cookies.get('sb-access-token')?.value).toBe('rotated-token');
  });

  it('carries a session cookie refreshed mid-request through the /login redirect', async () => {
    mockUser = null;
    mockRefreshedCookie = { name: 'sb-access-token', value: 'rotated-token' };
    const response = await middleware(makeRequest('/dashboard'));

    expect(response.status).toBe(307);
    expect(new URL(response.headers.get('location')!).pathname).toBe('/login');
    expect(response.cookies.get('sb-access-token')?.value).toBe('rotated-token');
  });
});
