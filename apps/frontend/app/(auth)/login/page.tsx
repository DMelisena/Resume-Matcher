'use client';

import { FormEvent, useState } from 'react';
import { createSupabaseBrowserClient } from '@/lib/supabase-browser';

export default function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    const { error: signInError } = await createSupabaseBrowserClient().auth.signInWithPassword({ email, password });
    if (signInError) { setError(signInError.message); return; }
    window.location.assign('/dashboard');
  }

  return <main><h1>Resume Matcher</h1><form onSubmit={submit}>
    <input aria-label="Email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
    <input aria-label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
    <button type="submit">Log in</button>
    {error && <p role="alert">{error}</p>}
  </form></main>;
}
