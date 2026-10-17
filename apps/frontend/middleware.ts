import { createServerClient } from '@supabase/ssr';
import { NextResponse, type NextRequest } from 'next/server';
import { SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL } from '@/lib/supabase/env';

// Routes that don't require a signed-in session. Everything else under the
// app (dashboard, builder, tailor, tracker, settings, resumes/*) is
// protected: an unauthenticated visitor is redirected to /login before any
// page component runs, so no user-owned data is ever fetched client-side
// without a session.
const PUBLIC_PATHS = ['/login', '/signup'];

function isAuthPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));
}

function isPublicPath(pathname: string): boolean {
  if (isAuthPath(pathname)) return true;
  // Landing page is public for signed-out visitors; print-only PDF-render
  // routes are not account-scoped UI. A signed-in user hitting '/' is
  // redirected straight to /dashboard below instead of seeing the landing
  // page, so this only governs *unauthenticated* access.
  if (pathname === '/') return true;
  if (pathname.startsWith('/print/')) return true;
  return false;
}

function redirectPreservingCookies(request: NextRequest, response: NextResponse, pathname: string) {
  const redirectUrl = request.nextUrl.clone();
  redirectUrl.pathname = pathname;
  redirectUrl.search = '';
  const redirectResponse = NextResponse.redirect(redirectUrl);
  for (const cookie of response.cookies.getAll()) {
    redirectResponse.cookies.set(cookie);
  }
  return redirectResponse;
}

export async function middleware(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value } of cookiesToSet) {
          request.cookies.set(name, value);
        }
        response = NextResponse.next({ request });
        for (const { name, value, options } of cookiesToSet) {
          response.cookies.set(name, value, options);
        }
      },
    },
  });

  // Refreshing the session (if needed) here — not just in Server Components —
  // is required by @supabase/ssr so cookies stay valid across the app.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname } = request.nextUrl;

  if (!user && !isPublicPath(pathname)) {
    const redirectUrl = request.nextUrl.clone();
    redirectUrl.pathname = '/login';
    redirectUrl.searchParams.set('next', pathname);
    const redirectResponse = NextResponse.redirect(redirectUrl);
    for (const cookie of response.cookies.getAll()) {
      redirectResponse.cookies.set(cookie);
    }
    return redirectResponse;
  }

  if (user && (isAuthPath(pathname) || pathname === '/')) {
    return redirectPreservingCookies(request, response, '/dashboard');
  }

  return response;
}

export const config = {
  matcher: [
    /*
     * Match all request paths except static assets and Next.js internals.
     * The backend API is same-origin-proxied under /api — that must stay
     * reachable without a redirect loop, so it's excluded here; the API
     * itself is what actually enforces auth on data (via the bearer token
     * apiFetch attaches, verified server-side by app/auth.py).
     */
    '/((?!_next/static|_next/image|favicon.ico|api/|docs|redoc|openapi.json|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)',
  ],
};
