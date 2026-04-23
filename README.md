# omi-supabase

Self-hosted Omi on Supabase.

This repo is a sanitized public export of a Supabase-first Omi branch. It is meant for people who want to run Omi locally or on their own infrastructure without depending on Firebase for the main backend path.

Current status:

- Verified locally: backend, pusher, web app, Android app, Firebase-to-Supabase migration tooling
- Included but less battle-tested: desktop app
- Not included: any private keys, local domains, machine-specific paths, or old project secrets

## What This Repo Contains

- `backend/`: Python API, auth flow, storage/vector/database adapters, migration scripts
- `supabase/`: local Supabase config and SQL migrations
- `web/app/`: Next.js web UI
- `app/`: Flutter mobile app
- `desktop/`: macOS desktop app and local Rust sidecar
- `compose.supabase-local.yml`: local backend, pusher, and web compose stack

## Local Ports

- Supabase API: `54321`
- Supabase Postgres: `54322`
- Supabase Studio: `54323`
- Backend API: `3030`
- Pusher: `3031`
- Web UI: `3110`
- Desktop Rust backend: `10201`

## Quick Start

1. Start Supabase.

```bash
cd supabase
cp .env.example .env
supabase start
supabase status
```

2. Configure the backend.

```bash
cd ../backend
cp .env.template .env
```

Set at least:

- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_JWT_SECRET`
- `SUPABASE_DB_URL`
- `SUPABASE_DATABASE_URL`
- `ENCRYPTION_SECRET`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`

For local Supabase, `SUPABASE_DB_URL` / `SUPABASE_DATABASE_URL` are usually:

```text
postgresql+psycopg://postgres:postgres@127.0.0.1:54322/postgres
```

3. Start the app-facing services.

```bash
cd ..
docker compose -f compose.supabase-local.yml up --build
```

4. Open the web UI.

- `http://127.0.0.1:3110/login`

5. For Android, point the app at your backend.

- Emulator: `http://10.0.2.2:3030/`
- Physical phone on the same LAN: `http://<your-host-ip>:3030/`

The Android app also supports a custom backend URL in Developer Settings. The value must end with a trailing slash.

## Google OAuth

The backend owns the OAuth flow. For Google sign-in, create or update a Google OAuth web application and add:

- Authorized redirect URI: `http://127.0.0.1:3030/v1/auth/callback/google`
- Authorized JavaScript origins: `http://127.0.0.1:3110`, `http://localhost:3110`

Then copy the same client ID and secret into:

- `backend/.env`
- `supabase/.env`

The mobile app still uses `omi://auth/callback`, but Google does not need that URI registered because Google returns to the backend, not directly to the app.

## Docs

- [Local setup](docs/LOCAL_SETUP.md)
- [Web setup](docs/WEB.md)
- [Mobile setup](docs/MOBILE.md)
- [Desktop setup](docs/DESKTOP.md)
- [Migration](docs/MIGRATION.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

## Validation

After startup, these should work:

```bash
curl http://127.0.0.1:3030/v1/health
curl http://127.0.0.1:3031/health
open http://127.0.0.1:3110/login
```

Expected responses:

- backend: `{"status":"ok"}`
- pusher: `{"status":"healthy"}`

## Caveats

- The web and Android paths are the most tested in this repo.
- Desktop is included and can run locally, but some desktop-only routes still have legacy compatibility hooks.
- Browser push notifications remain optional. If you do not configure Firebase messaging env vars for the web app, the build now falls back to a no-op service worker.
