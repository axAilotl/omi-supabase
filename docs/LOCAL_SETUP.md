# Local Setup

## Prerequisites

- Docker and Docker Compose
- Supabase CLI
- Python 3.11+
- Node.js 20+
- `uv` for migration tooling
- Flutter 3.35+ if you want the mobile app
- Xcode 16+ if you want the desktop app

## 1. Start Supabase

```bash
cd supabase
cp .env.example .env
supabase start
supabase status
```

Copy these values from `supabase status` into `backend/.env` and, if needed, `desktop/Backend-Rust/.env`:

- `anon key`
- `service_role key`
- `JWT secret`

Use this DB URL for the local backend unless you changed the default ports:

```text
postgresql+psycopg://postgres:postgres@127.0.0.1:54322/postgres
```

## 2. Configure the backend

```bash
cd ../backend
cp .env.template .env
```

Required values:

- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_JWT_SECRET`
- `SUPABASE_DB_URL`
- `SUPABASE_DATABASE_URL`
- `ENCRYPTION_SECRET`

If you want Google sign-in:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `BASE_API_URL=http://127.0.0.1:3030`

The default template already points local backend traffic at:

- `HOSTED_PUSHER_API_URL=http://127.0.0.1:3031`

## 3. Start backend, pusher, and web

From repo root:

```bash
docker compose -f compose.supabase-local.yml up --build
```

That gives you:

- backend on `http://127.0.0.1:3030`
- pusher on `http://127.0.0.1:3031`
- web app on `http://127.0.0.1:3110`

## 4. Health checks

```bash
curl http://127.0.0.1:3030/v1/health
curl http://127.0.0.1:3031/health
```

## 5. Optional web dev mode

If you want `next dev` instead of Docker for the web app:

```bash
cd web/app
cp .env.local.example .env.local
npm ci
npm run dev
```

The default `.env.local.example` already points to the local backend and websocket ports.
