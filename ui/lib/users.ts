/**
 * Administrator user management.
 *
 * The platform has no self-service signup by design — access is granted, not
 * requested — so every one of these is admin-only on the backend, and the last
 * active administrator cannot be removed, demoted or disabled by any of them.
 */
import { authHeader } from '@/lib/auth';

const BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export interface ManagedUser {
  id: string;
  username: string;
  role: 'admin' | 'user';
  disabled: boolean;
  created_at: string | null;
  last_login_at: string | null;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`);
  }
  return data as T;
}

export async function listUsers(): Promise<ManagedUser[]> {
  const d = await call<{ users: ManagedUser[] }>('/auth/users');
  return d.users;
}

export function createUser(username: string, password: string) {
  return call('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export function setRole(id: string, role: 'admin' | 'user') {
  return call(`/auth/users/${id}/role`, {
    method: 'POST',
    body: JSON.stringify({ role }),
  });
}

export function setPassword(id: string, password: string) {
  return call<{ note?: string }>(`/auth/users/${id}/password`, {
    method: 'POST',
    body: JSON.stringify({ password }),
  });
}

export function setUsername(id: string, username: string) {
  return call<{ username: string; note?: string }>(`/auth/users/${id}/username`, {
    method: 'POST',
    body: JSON.stringify({ username }),
  });
}

export function setDisabled(id: string, disabled: boolean) {
  return call(`/auth/users/${id}/${disabled ? 'disable' : 'enable'}`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export function deleteUser(id: string) {
  return call(`/auth/users/${id}`, { method: 'DELETE' });
}
