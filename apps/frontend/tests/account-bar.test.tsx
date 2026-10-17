import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AccountBar } from '@/components/common/account-bar';

const push = vi.fn();
const replace = vi.fn();
const refresh = vi.fn();
const signOut = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push, replace, refresh }),
}));

let mockAuth: { user: { email: string } | null; loading: boolean };

vi.mock('@/lib/context/auth-context', () => ({
  useAuth: () => ({ ...mockAuth, signOut }),
}));

describe('AccountBar', () => {
  beforeEach(() => {
    push.mockClear();
    replace.mockClear();
    refresh.mockClear();
    signOut.mockClear();
  });

  it('shows a login action and no account info when signed out', () => {
    mockAuth = { user: null, loading: false };
    render(<AccountBar />);

    expect(screen.getByRole('button', { name: /log in/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign out/i })).not.toBeInTheDocument();
  });

  it('navigates to /login when the login action is clicked', () => {
    mockAuth = { user: null, loading: false };
    render(<AccountBar />);

    fireEvent.click(screen.getByRole('button', { name: /log in/i }));
    expect(push).toHaveBeenCalledWith('/login');
  });

  it('shows the signed-in email and a sign-out action when authenticated', () => {
    mockAuth = { user: { email: 'person@example.com' }, loading: false };
    render(<AccountBar />);

    expect(screen.getByText('person@example.com')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign out/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /log in/i })).not.toBeInTheDocument();
  });

  it('signs out and redirects to /login when sign-out is clicked', async () => {
    mockAuth = { user: { email: 'person@example.com' }, loading: false };
    render(<AccountBar />);

    fireEvent.click(screen.getByRole('button', { name: /sign out/i }));

    await waitFor(() => expect(signOut).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith('/login');
    expect(refresh).toHaveBeenCalled();
  });

  it('shows neither action while the auth state is still loading', () => {
    mockAuth = { user: null, loading: true };
    render(<AccountBar />);

    expect(screen.queryByRole('button', { name: /log in/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign out/i })).not.toBeInTheDocument();
  });
});
