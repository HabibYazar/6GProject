/**
 * Client-side auth: the token, and who is holding it.
 *
 * The backend issues a bearer JWT (see nsg_ai/auth.py). It is kept in
 * localStorage rather than a cookie because the API is a separate origin and
 * every call already goes through one place — lib/api.ts — which attaches the
 * header. A cookie would need CORS credentials and a same-site story we do not
 * have yet, and would buy nothing while the API is header-authenticated.
 *
 * Enforcement is a DEPLOYMENT switch on the backend (NSG_AUTH_REQUIRED). When it
 * is off the whole app works logged-out exactly as before, so nothing here may
 * assume a session exists.
 */

const TOKEN_KEY = 'nsg.auth.token';

export interface AuthState {
  authenticated: boolean;
  /** Does this deployment require a login at all? */
  enforced: boolean;
  /** True when no user exists yet — the first account is created without one. */
  bootstrap: boolean;
  username: string;
  role: string;
  user_id: string | null;
}

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // private mode / blocked storage — treat as logged out
  }
}

export function setToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* nothing we can do; the session simply will not persist */
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/** Authorization header for a request, or nothing when logged out. */
export function authHeader(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

const BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    throw new Error(
      typeof detail === 'string' ? detail : `HTTP ${res.status}`
    );
  }
  return data as T;
}

/** Who the caller is, and whether this deployment even asks. */
export async function fetchAuthState(): Promise<AuthState> {
  const res = await fetch(`${BASE}/auth/me`, { headers: { ...authHeader() } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const state = (await res.json()) as AuthState;
  if (!state.authenticated && getToken()) {
    clearToken();
  }
  return state;
}

export async function login(username: string, password: string): Promise<AuthState> {
  const data = await post<{ access_token: string }>('/auth/login', {
    username,
    password,
  });
  setToken(data.access_token);
  try {
    const state = await fetchAuthState();
    if (!state.authenticated) {
      throw new Error('Session verification failed.');
    }
    return state;
  } catch (error) {
    clearToken();
    throw error;
  }
}

/** Create the FIRST account. Allowed without credentials only while none exist. */
export async function registerFirstAdmin(
  username: string,
  password: string
): Promise<AuthState> {
  await post('/auth/register', { username, password });
  return login(username, password);
}

export function logout(): void {
  clearToken();
}

/** Change your own password. The current one is the only verification: anything
 *  else a reset flow might ask for is not secret. A FORGOTTEN password is an
 *  administrator's job — see /admin/users. */
export function changeOwnPassword(
  currentPassword: string,
  newPassword: string
): Promise<{ note?: string }> {
  return post('/auth/password', {
    current_password: currentPassword,
    new_password: newPassword,
  });
}
