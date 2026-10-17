'use client';

import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/context/auth-context';
import { Button } from '@/components/ui/button';

/**
 * Thin account strip shown in the shared layout on every (default)-group
 * page: a persistently visible auth-state indicator. Signed-out visitors
 * see a "Log in" action; signed-in users see their email and "Sign out".
 * Kept visible (rather than returning null) while loading, as an empty
 * placeholder, so the strip doesn't pop in/out and shift layout.
 */
export function AccountBar() {
  const { user, loading, signOut } = useAuth();
  const router = useRouter();

  const handleSignOut = async () => {
    await signOut();
    router.replace('/login');
    router.refresh();
  };

  return (
    <div className="flex items-center justify-end gap-3 border-b border-black bg-canvas px-4 py-2">
      {loading ? null : user ? (
        <>
          <span className="font-mono text-xs text-ink-soft truncate max-w-[16rem]">
            {user.email}
          </span>
          <Button variant="ghost" size="sm" onClick={handleSignOut}>
            Sign out
          </Button>
        </>
      ) : (
        <Button variant="ghost" size="sm" onClick={() => router.push('/login')}>
          Log in
        </Button>
      )}
    </div>
  );
}
