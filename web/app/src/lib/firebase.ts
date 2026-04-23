import { getApps, initializeApp, type FirebaseApp } from "firebase/app";
import {
  getMessaging,
  getToken,
  isSupported,
  onMessage,
  type MessagePayload,
  type Messaging,
} from "firebase/messaging";

type StoredSession = {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
  user: {
    uid: string;
    email: string | null;
    displayName: string | null;
    photoURL: string | null;
    givenName: string | null;
    familyName: string | null;
  };
};

export interface User {
  uid: string;
  email: string | null;
  displayName: string | null;
  photoURL: string | null;
  getIdToken: () => Promise<string | null>;
}

const AUTH_SESSION_KEY = "omi-web-app-auth-session";
const AUTH_RESULT_KEY = "omi-web-app-auth-result";
const AUTH_EVENT = "omi-web-app-auth-change";

const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  measurementId: process.env.NEXT_PUBLIC_FIREBASE_MEASUREMENT_ID,
};

let firebaseApp: FirebaseApp | null | undefined;
let messagingInstance: Messaging | null = null;
let authWatchersInitialized = false;

const authListeners = new Set<(user: User | null) => void>();

export const auth: { currentUser: User | null } = {
  currentUser: null,
};

function hasFirebaseMessagingConfig(): boolean {
  return Boolean(
    firebaseConfig.apiKey &&
    firebaseConfig.projectId &&
    firebaseConfig.messagingSenderId &&
    firebaseConfig.appId &&
    firebaseConfig.authDomain,
  );
}

function getFirebaseAppInstance(): FirebaseApp | null {
  if (firebaseApp !== undefined) {
    return firebaseApp;
  }

  if (!hasFirebaseMessagingConfig()) {
    firebaseApp = null;
    return firebaseApp;
  }

  firebaseApp =
    getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];
  return firebaseApp;
}

function resolveApiBaseUrl(): string {
  const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:3030";
  return baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
}

function readSession(): StoredSession | null {
  if (typeof window === "undefined") return null;

  const raw = window.localStorage.getItem(AUTH_SESSION_KEY);
  if (!raw) return null;

  try {
    const session = JSON.parse(raw) as StoredSession;
    const accessPayload = decodeJwtPayload(session.accessToken);
    if (!isSupabaseSessionPayload(accessPayload)) {
      window.localStorage.removeItem(AUTH_SESSION_KEY);
      return null;
    }
    return session;
  } catch {
    window.localStorage.removeItem(AUTH_SESSION_KEY);
    return null;
  }
}

function emitAuthChange(): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(AUTH_EVENT, String(Date.now()));
  window.dispatchEvent(new Event(AUTH_EVENT));
}

function writeSession(session: StoredSession | null): void {
  if (typeof window === "undefined") return;

  if (session) {
    window.localStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(session));
  } else {
    window.localStorage.removeItem(AUTH_SESSION_KEY);
  }

  emitAuthChange();
}

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  if (typeof window === "undefined") return null;

  const parts = token.split(".");
  if (parts.length < 2) return null;

  try {
    const normalized = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
    return JSON.parse(window.atob(padded)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function getString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function isSupabaseSessionPayload(
  payload: Record<string, unknown> | null,
): boolean {
  if (!payload) return false;

  const issuer = getString(payload.iss);
  const audience = payload.aud;
  const role = getString(payload.role);

  if (role === "authenticated" || role === "service_role") {
    return true;
  }

  if (issuer && issuer.includes("/auth/v1")) {
    return true;
  }

  if (audience === "authenticated") {
    return true;
  }

  if (Array.isArray(audience) && audience.includes("authenticated")) {
    return true;
  }

  return false;
}

function getSessionRecord(
  payload: Record<string, unknown>,
): Record<string, unknown> {
  const nestedSession = payload.session;
  if (nestedSession && typeof nestedSession === "object") {
    return nestedSession as Record<string, unknown>;
  }
  return payload;
}

function coerceNumber(value: unknown, fallback: number): number {
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return fallback;
}

function buildStoredSession(payload: Record<string, unknown>): StoredSession {
  const sessionPayload = getSessionRecord(payload);
  const user =
    (payload.user as Record<string, unknown> | undefined) ??
    (sessionPayload.user as Record<string, unknown> | undefined) ??
    {};
  const userMetadata =
    (user.user_metadata as Record<string, unknown> | undefined) ?? {};
  const providerPayload =
    typeof payload.id_token === "string"
      ? decodeJwtPayload(payload.id_token)
      : null;
  const accessToken =
    getString(payload.access_token) ?? getString(sessionPayload.access_token);
  if (!accessToken) {
    throw new Error("Authentication callback did not include an access token");
  }
  const accessPayload = decodeJwtPayload(accessToken);
  if (!isSupabaseSessionPayload(accessPayload)) {
    throw new Error("Authentication callback did not return a valid Omi session");
  }
  const refreshToken =
    getString(payload.refresh_token) ?? getString(sessionPayload.refresh_token);
  if (!refreshToken) {
    throw new Error("Authentication callback did not include a refresh token");
  }

  const uid =
    (user.id as string | undefined) ||
    (accessPayload?.sub as string | undefined) ||
    "";

  const fullName =
    (userMetadata.full_name as string | undefined) ||
    (userMetadata.name as string | undefined) ||
    (providerPayload?.name as string | undefined) ||
    null;

  const givenName =
    (userMetadata.given_name as string | undefined) ||
    (providerPayload?.given_name as string | undefined) ||
    (fullName ? fullName.split(" ")[0] : null) ||
    null;

  const familyName =
    (userMetadata.family_name as string | undefined) ||
    (providerPayload?.family_name as string | undefined) ||
    (fullName && fullName.includes(" ")
      ? fullName.split(" ").slice(1).join(" ")
      : null) ||
    null;

  return {
    accessToken,
    refreshToken,
    expiresAt:
      Date.now() +
      coerceNumber(payload.expires_in ?? sessionPayload.expires_in, 3600) * 1000,
    user: {
      uid,
      email:
        (user.email as string | undefined) ||
        (accessPayload?.email as string | undefined) ||
        (providerPayload?.email as string | undefined) ||
        null,
      displayName:
        fullName || [givenName, familyName].filter(Boolean).join(" ") || null,
      photoURL:
        (userMetadata.avatar_url as string | undefined) ||
        (userMetadata.picture as string | undefined) ||
        (providerPayload?.picture as string | undefined) ||
        null,
      givenName,
      familyName,
    },
  };
}

async function refreshAccessToken(): Promise<string | null> {
  const session = readSession();
  if (!session?.refreshToken) {
    writeSession(null);
    syncAuthState();
    return null;
  }

  const response = await fetch(`${resolveApiBaseUrl()}v1/auth/refresh`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({ refresh_token: session.refreshToken }),
  });

  if (!response.ok) {
    writeSession(null);
    syncAuthState();
    return null;
  }

  const payload = (await response.json()) as Record<string, unknown>;
  const refreshedSession = buildStoredSession(payload);
  writeSession(refreshedSession);
  syncAuthState();
  return refreshedSession.accessToken;
}

function buildUser(session: StoredSession): User | null {
  if (!session.user.uid) return null;

  return {
    uid: session.user.uid,
    email: session.user.email,
    displayName: session.user.displayName,
    photoURL: session.user.photoURL,
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

function getCurrentUserFromStorage(): User | null {
  const session = readSession();
  if (!session) return null;
  return buildUser(session);
}

function syncAuthState(): User | null {
  auth.currentUser = getCurrentUserFromStorage();
  authListeners.forEach((listener) => listener(auth.currentUser));
  return auth.currentUser;
}

function ensureAuthWatchers(): void {
  if (typeof window === "undefined" || authWatchersInitialized) return;
  authWatchersInitialized = true;

  const handleStorage = (event: StorageEvent) => {
    if (event.key === AUTH_SESSION_KEY || event.key === AUTH_EVENT) {
      syncAuthState();
    }
  };

  window.addEventListener("storage", handleStorage);
  window.addEventListener(AUTH_EVENT, () => {
    syncAuthState();
  });

  syncAuthState();
}

async function signInWithProvider(
  provider: "google" | "apple",
): Promise<User | null> {
  if (typeof window === "undefined") return null;

  const state = window.crypto.randomUUID();
  const redirectUri = `${window.location.origin}/auth/callback`;
  const authorizeUrl = new URL(`${resolveApiBaseUrl()}v1/auth/authorize`);
  window.localStorage.removeItem(AUTH_RESULT_KEY);
  authorizeUrl.searchParams.set("provider", provider);
  authorizeUrl.searchParams.set("redirect_uri", redirectUri);
  authorizeUrl.searchParams.set("state", state);

  const popup = window.open(
    authorizeUrl.toString(),
    "omi-auth",
    "width=520,height=700",
  );
  if (!popup) {
    throw new Error("Popup blocked");
  }

  return new Promise<User | null>((resolve, reject) => {
    let popupClosedAt: number | null = null;

    const resolveWithCurrentUser = () => {
      cleanup();
      resolve(syncAuthState());
    };

    const rejectWithError = (error: Error) => {
      cleanup();
      reject(error);
    };

    const processAuthResult = (rawResult: string) => {
      try {
        const result = JSON.parse(rawResult) as {
          error?: string;
          state?: string;
        };
        window.localStorage.removeItem(AUTH_RESULT_KEY);

        if (result.state && result.state !== state) {
          rejectWithError(new Error("State mismatch"));
          return true;
        }

        if (result.error) {
          rejectWithError(new Error(result.error));
          return true;
        }

        resolveWithCurrentUser();
        return true;
      } catch (error) {
        rejectWithError(
          error instanceof Error ? error : new Error("Authentication failed"),
        );
        return true;
      }
    };

    const timeout = window.setTimeout(
      () => {
        rejectWithError(new Error("Authentication timeout"));
      },
      5 * 60 * 1000,
    );

    const interval = window.setInterval(() => {
      if (!popup.closed) {
        popupClosedAt = null;
        return;
      }

      const pendingResult = window.localStorage.getItem(AUTH_RESULT_KEY);
      if (pendingResult) {
        processAuthResult(pendingResult);
        return;
      }

      const currentUser = syncAuthState();
      if (currentUser) {
        resolveWithCurrentUser();
        return;
      }

      if (!popupClosedAt) {
        popupClosedAt = Date.now();
        return;
      }

      if (Date.now() - popupClosedAt > 1500) {
        rejectWithError(new Error("Authentication window closed before completion"));
      }
    }, 250);

    const handleStorage = (event: StorageEvent) => {
      if (event.key !== AUTH_RESULT_KEY || !event.newValue) return;
      processAuthResult(event.newValue);
    };

    const cleanup = () => {
      window.clearTimeout(timeout);
      window.clearInterval(interval);
      window.removeEventListener("storage", handleStorage);
      popup.close();
    };

    window.addEventListener("storage", handleStorage);
  });
}

export const signInWithGoogle = async (): Promise<User | null> => {
  ensureAuthWatchers();
  return signInWithProvider("google");
};

export const signInWithApple = async (): Promise<User | null> => {
  ensureAuthWatchers();
  return signInWithProvider("apple");
};

export const signOutUser = async (): Promise<void> => {
  writeSession(null);
  syncAuthState();
};

export const getIdToken = async (): Promise<string | null> => {
  ensureAuthWatchers();
  const user = auth.currentUser || syncAuthState();
  if (!user) return null;
  return user.getIdToken();
};

export const onAuthStateChange = (callback: (user: User | null) => void) => {
  ensureAuthWatchers();
  authListeners.add(callback);
  callback(auth.currentUser);

  return () => {
    authListeners.delete(callback);
  };
};

export async function completeAuthCallback(
  code: string,
  state: string,
): Promise<void> {
  if (typeof window === "undefined") return;

  const redirectUri = `${window.location.origin}/auth/callback`;
  const response = await fetch(`${resolveApiBaseUrl()}v1/auth/token`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri,
    }),
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || "Authentication failed");
  }

  const payload = (await response.json()) as Record<string, unknown>;
  writeSession(buildStoredSession(payload));
  syncAuthState();
  window.localStorage.setItem(
    AUTH_RESULT_KEY,
    JSON.stringify({ ok: true, state }),
  );
}

// ============================================
// Firebase Cloud Messaging (FCM) for Push Notifications
// ============================================

const VAPID_KEY = process.env.NEXT_PUBLIC_FIREBASE_VAPID_KEY;

export const isMessagingSupported = async (): Promise<boolean> => {
  if (typeof window === "undefined") return false;
  if (!hasFirebaseMessagingConfig()) return false;

  try {
    return await isSupported();
  } catch {
    return false;
  }
};

export const getMessagingInstance = async (): Promise<Messaging | null> => {
  if (typeof window === "undefined") return null;

  if (messagingInstance) return messagingInstance;

  const app = getFirebaseAppInstance();
  if (!app) {
    console.warn("Firebase messaging configuration is missing");
    return null;
  }

  const supported = await isMessagingSupported();
  if (!supported) {
    console.warn("Firebase Messaging is not supported in this browser");
    return null;
  }

  try {
    messagingInstance = getMessaging(app);
    return messagingInstance;
  } catch (error) {
    console.error("Failed to initialize Firebase Messaging:", error);
    return null;
  }
};

const registerServiceWorker =
  async (): Promise<ServiceWorkerRegistration | null> => {
    if (typeof window === "undefined" || !("serviceWorker" in navigator)) {
      return null;
    }

    try {
      const registration = await navigator.serviceWorker.register(
        "/firebase-messaging-sw.js",
      );

      const installingWorker = registration.installing;
      if (installingWorker) {
        await new Promise<void>((resolve) => {
          const handler = (event: Event) => {
            if ((event.target as ServiceWorker).state === "activated") {
              installingWorker.removeEventListener("statechange", handler);
              resolve();
            }
          };
          installingWorker.addEventListener("statechange", handler);
        });
      } else {
        const waitingWorker = registration.waiting;
        if (waitingWorker) {
          await new Promise<void>((resolve) => {
            const handler = (event: Event) => {
              if ((event.target as ServiceWorker).state === "activated") {
                waitingWorker.removeEventListener("statechange", handler);
                resolve();
              }
            };
            waitingWorker.addEventListener("statechange", handler);
          });
        }
      }

      await navigator.serviceWorker.ready;

      return registration;
    } catch (error) {
      console.error("Service Worker registration failed:", error);
      return null;
    }
  };

export const requestNotificationPermission = async (): Promise<
  string | null
> => {
  if (typeof window === "undefined") return null;
  if (!hasFirebaseMessagingConfig()) return null;

  if (!("Notification" in window)) {
    console.warn("This browser does not support notifications");
    return null;
  }

  if (!("serviceWorker" in navigator)) {
    console.warn("Service workers are not supported");
    return null;
  }

  const swRegistration = await registerServiceWorker();
  if (!swRegistration) return null;

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    return null;
  }

  const messaging = await getMessagingInstance();
  if (!messaging) return null;

  try {
    const token = await getToken(messaging, {
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: swRegistration,
    });

    return token || null;
  } catch (error) {
    console.error("Failed to get FCM token:", error);
    return null;
  }
};

export const getCurrentFCMToken = async (): Promise<string | null> => {
  if (typeof window === "undefined") return null;
  if (!hasFirebaseMessagingConfig()) return null;

  if (Notification.permission !== "granted") {
    return null;
  }

  if (!("serviceWorker" in navigator)) {
    return null;
  }

  const swRegistration = await registerServiceWorker();
  if (!swRegistration) return null;

  const messaging = await getMessagingInstance();
  if (!messaging) return null;

  try {
    const token = await getToken(messaging, {
      vapidKey: VAPID_KEY,
      serviceWorkerRegistration: swRegistration,
    });
    return token || null;
  } catch (error) {
    console.error("Failed to get current FCM token:", error);
    return null;
  }
};

export const onForegroundMessage = async (
  callback: (payload: MessagePayload) => void,
): Promise<(() => void) | null> => {
  const messaging = await getMessagingInstance();
  if (!messaging) {
    return null;
  }

  return onMessage(messaging, (payload) => {
    callback(payload);
  });
};

export const getNotificationPermission = ():
  | NotificationPermission
  | "unsupported" => {
  if (typeof window === "undefined" || !("Notification" in window)) {
    return "unsupported";
  }
  return Notification.permission;
};

export default getFirebaseAppInstance();
