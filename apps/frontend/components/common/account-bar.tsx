'use client';

import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/context/auth-context';
import { Button } from '@/components/ui/button';

/** Thin account strip: shows the signed-in email and a sign-out action. */
export function AccountBar() {
  const { user, loading, signOut } = useAuth();
  const router = useRouter();

  if (loading || !user) return null;

  const handleSignOut = async () => {
    await signOut();
    router.replace('/login');
    router.refresh();
  };

  return (
    <div className="flex items-center justify-end gap-3 border-b border-black bg-canvas px-4 py-2">
      <span className="font-mono text-xs text-ink-soft truncate max-w-[16rem]">{user.email}</span>
      <Button variant="ghost" size="sm" onClick={handleSignOut}>
        Sign out
      </Button>
    </div>
  );
}
