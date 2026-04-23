# Web Setup

## Login flow

The web app no longer uses Firebase Auth popups. It now uses:

1. `GET /v1/auth/authorize`
2. backend callback at `/v1/auth/callback/google`
3. session exchange at `/v1/auth/token`

That means the Google OAuth app must point at the backend, not at `firebaseapp.com`.

## Google OAuth config

Create or edit a Google OAuth web application and add:

- Authorized redirect URI: `http://127.0.0.1:3030/v1/auth/callback/google`
- Authorized JavaScript origin: `http://127.0.0.1:3110`
- Authorized JavaScript origin: `http://localhost:3110`

Then set the same values in:

- `backend/.env`
- `supabase/.env`

```text
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
SUPABASE_AUTH_EXTERNAL_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
SUPABASE_AUTH_EXTERNAL_GOOGLE_SECRET=...
```

## Running the web app

Docker path:

```bash
docker compose -f compose.supabase-local.yml up --build web-home
```

Direct Next.js path:

```bash
cd web/app
cp .env.local.example .env.local
npm ci
npm run dev
```

## Optional browser push

Browser push remains optional.

If you do not set the `NEXT_PUBLIC_FIREBASE_*` messaging vars, the build now emits a no-op service worker instead of failing.

If you want browser push, set the Firebase messaging vars in `web/app/.env.local` before `npm run dev` or `npm run build`.
