'use client';

import envConfig from '../constants/envConfig';

const AUTH_SESSION_KEY = 'omi-web-auth-session';
const AUTH_RESULT_KEY = 'omi-web-auth-result';
const AUTH_EVENT = 'omi-auth-change';

type StoredSession = {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
  user: {
    uid: string;
    email: string | null;
    displayName: string | null;
    givenName: string | null;
    familyName: string | null;
  };
};

export interface AuthUser {
  uid: string;
  email: string | null;
  displayName: string | null;
  getIdToken: () => Promise<string | null>;
}

function resolveApiBaseUrl(): string {
  const baseUrl = envConfig.API_URL || 'http://localhost:8000';
  return baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/`;
}

function readSession(): StoredSession | null {
  if (typeof window === 'undefined') return null;
  const raw = window.localStorage.getItem(AUTH_SESSION_KEY);
  if (!raw) return null;

  try {
    return JSON.parse(raw) as StoredSession;
  } catch {
    window.localStorage.removeItem(AUTH_SESSION_KEY);
    return null;
  }
}

function writeSession(session: StoredSession | null) {
  if (typeof window === 'undefined') return;

  if (session) {
    window.localStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(session));
  } else {
    window.localStorage.removeItem(AUTH_SESSION_KEY);
  }

  window.localStorage.setItem(AUTH_EVENT, String(Date.now()));
  window.dispatchEvent(new Event(AUTH_EVENT));
}

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split('.');
  if (parts.length < 2) return null;

  try {
    const normalized = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
    return JSON.parse(window.atob(padded)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function coerceNumber(value: unknown, fallback: number): number {
  if (typeof value === 'number') return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return fallback;
}

function buildStoredSession(payload: Record<string, unknown>): StoredSession {
  const user = (payload.user as Record<string, unknown> | undefined) ?? {};
  const userMetadata = (user.user_metadata as Record<string, unknown> | undefined) ?? {};
  const providerPayload =
    typeof payload.id_token === 'string' ? decodeJwtPayload(payload.id_token) : null;
  const accessToken = payload.access_token as string;
  const accessPayload = decodeJwtPayload(accessToken);

  const uid =
    (user.id as string | undefined) || (accessPayload?.sub as string | undefined) || '';

  const fullName =
    (userMetadata.full_name as string | undefined) ||
    (userMetadata.name as string | undefined) ||
    (providerPayload?.name as string | undefined) ||
    null;

  const givenName =
    (userMetadata.given_name as string | undefined) ||
    (providerPayload?.given_name as string | undefined) ||
    (fullName ? fullName.split(' ')[0] : null) ||
    null;

  const familyName =
    (userMetadata.family_name as string | undefined) ||
    (providerPayload?.family_name as string | undefined) ||
    (fullName && fullName.includes(' ')
      ? fullName.split(' ').slice(1).join(' ')
      : null) ||
    null;

  return {
    accessToken,
    refreshToken: payload.refresh_token as string,
    expiresAt: Date.now() + coerceNumber(payload.expires_in, 3600) * 1000,
    user: {
      uid,
      email:
        (user.email as string | undefined) ||
        (accessPayload?.email as string | undefined) ||
        (providerPayload?.email as string | undefined) ||
        null,
      displayName: fullName || [givenName, familyName].filter(Boolean).join(' ') || null,
      givenName,
      familyName,
    },
  };
}

async function refreshAccessToken(): Promise<string | null> {
  const session = readSession();
  if (!session?.refreshToken) {
    writeSession(null);
    return null;
  }

  const response = await fetch(`${resolveApiBaseUrl()}v1/auth/refresh`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams({ refresh_token: session.refreshToken }),
  });

  if (!response.ok) {
    writeSession(null);
    return null;
  }

  const payload = (await response.json()) as Record<string, unknown>;
  const refreshedSession = buildStoredSession(payload);
  writeSession(refreshedSession);
  return refreshedSession.accessToken;
}

function getCurrentUser(): AuthUser | null {
  const session = readSession();
  if (!session?.user.uid) return null;

  return {
    uid: session.user.uid,
    email: session.user.email,
    displayName: session.user.displayName,
    getIdToken: async () => {
      const current = readSession();
      if (!current) return null;
      if (current.expiresAt > Date.now() + 5 * 60 * 1000) {
        return current.accessToken;
      }
      return refreshAccessToken();
    },
  };
}

export async function signInWithGoogle(): Promise<AuthUser | null> {
  const state = window.crypto.randomUUID();
  const redirectUri = `${window.location.origin}/auth/callback`;
  const authorizeUrl = new URL(`${resolveApiBaseUrl()}v1/auth/authorize`);
  authorizeUrl.searchParams.set('provider', 'google');
  authorizeUrl.searchParams.set('redirect_uri', redirectUri);
  authorizeUrl.searchParams.set('state', state);

  const popup = window.open(authorizeUrl.toString(), 'omi-auth', 'width=520,height=700');
  if (!popup) {
    throw new Error('Popup blocked');
  }

  return new Promise<AuthUser | null>((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error('Authentication timeout'));
    }, 5 * 60 * 1000);

    const interval = window.setInterval(() => {
      if (popup.closed) {
        cleanup();
        resolve(getCurrentUser());
      }
    }, 500);

    const handleStorage = (event: StorageEvent) => {
      if (event.key !== AUTH_RESULT_KEY || !event.newValue) return;
      cleanup();
      try {
        const result = JSON.parse(event.newValue) as {
          ok?: boolean;
          error?: string;
          state?: string;
        };
        window.localStorage.removeItem(AUTH_RESULT_KEY);
        if (result.state && result.state !== state) {
          reject(new Error('State mismatch'));
          return;
        }
        if (result.error) {
          reject(new Error(result.error));
          return;
        }
        resolve(getCurrentUser());
      } catch (error) {
        reject(error instanceof Error ? error : new Error('Authentication failed'));
      }
    };

    const cleanup = () => {
      window.clearTimeout(timeout);
      window.clearInterval(interval);
      window.removeEventListener('storage', handleStorage);
      popup.close();
    };

    window.addEventListener('storage', handleStorage);
  });
}

export async function signOutUser(): Promise<void> {
  writeSession(null);
}

export function onAuthStateChange(callback: (user: AuthUser | null) => void) {
  const notify = () => callback(getCurrentUser());
  notify();

  const handleStorage = (event: StorageEvent) => {
    if (event.key === AUTH_SESSION_KEY || event.key === AUTH_EVENT) {
      notify();
    }
  };

  window.addEventListener('storage', handleStorage);
  window.addEventListener(AUTH_EVENT, notify);

  return () => {
    window.removeEventListener('storage', handleStorage);
    window.removeEventListener(AUTH_EVENT, notify);
  };
}

export async function completeAuthCallback(code: string, state: string): Promise<void> {
  const redirectUri = `${window.location.origin}/auth/callback`;
  const response = await fetch(`${resolveApiBaseUrl()}v1/auth/token`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      redirect_uri: redirectUri,
    }),
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || 'Authentication failed');
  }

  const payload = (await response.json()) as Record<string, unknown>;
  writeSession(buildStoredSession(payload));
  window.localStorage.setItem(AUTH_RESULT_KEY, JSON.stringify({ ok: true, state }));
}
