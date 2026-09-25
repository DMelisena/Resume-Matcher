'use client';

import { useState, type FormEvent } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card } from '@/components/ui/card';
import { createClient } from '@/lib/supabase/client';

export default function SignupPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const supabase = createClient();
      const { data, error: signUpError } = await supabase.auth.signUp({
        email,
        password,
      });
      if (signUpError) {
        setError(signUpError.message);
        return;
      }
      // If email confirmation is required, Supabase returns a user with no
      // active session yet — send them to check their inbox instead of the
      // dashboard (which would just redirect back to /login).
      if (data.session) {
        window.location.href = '/dashboard';
        return;
      }
      setSubmitted(true);
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-background px-4 py-12">
        <Card variant="outline" className="w-full max-w-sm text-center">
          <h1 className="font-serif text-2xl font-bold text-ink mb-2">Check your email</h1>
          <p className="font-sans text-sm text-ink-soft">
            We sent a confirmation link to <span className="font-mono">{email}</span>. Follow it to
            activate your account, then sign in.
          </p>
          <Link href="/login" className="text-primary underline text-sm mt-6 inline-block">
            Back to sign in
          </Link>
        </Card>
      </main>
    );
  }

  return (
    <main className="min-h-screen flex items-center justify-center bg-background px-4 py-12">
      <Card variant="outline" className="w-full max-w-sm">
        <h1 className="font-serif text-2xl font-bold text-ink mb-1">Create account</h1>
        <p className="font-sans text-sm text-ink-soft mb-6">
          Each account&apos;s resumes, jobs, and tracker data are private to it.
        </p>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              autoComplete="new-password"
              minLength={8}
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          {error && (
            <p role="alert" className="font-mono text-xs text-destructive">
              {error}
            </p>
          )}
          <Button type="submit" disabled={submitting} className="mt-2">
            {submitting ? 'Creating account…' : 'Create account'}
          </Button>
        </form>
        <p className="font-sans text-sm text-ink-soft mt-6">
          Already have an account?{' '}
          <Link href="/login" className="text-primary underline">
            Sign in
          </Link>
        </p>
      </Card>
    </main>
  );
}
