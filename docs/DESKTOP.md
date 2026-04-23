# Desktop Setup

Desktop is included, but it is less polished than the web and Android paths.

The repo contains:

- `desktop/Desktop`: Swift app
- `desktop/Backend-Rust`: local Rust sidecar/backend
- `desktop/run.sh`: local launcher

## What works

- local build and launch path
- Supabase auth/session flow through the desktop backend
- local `.env` wiring for backend URLs

## What is still rough

- some desktop-only compatibility paths still assume optional legacy env vars
- the desktop path has had less end-to-end validation than web and Android

## Configure the desktop backend

```bash
cd desktop/Backend-Rust
cp .env.example .env
```

Minimum useful values:

```text
PORT=10201
BASE_API_URL=http://127.0.0.1:10201
OMI_PYTHON_API_URL=http://127.0.0.1:3030
SUPABASE_URL=http://127.0.0.1:54321
SUPABASE_AUTH_URL=http://127.0.0.1:54321/auth/v1
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
```

If you want Google sign-in through the desktop backend, add:

```text
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

Then add this redirect URI in Google Cloud:

- `http://127.0.0.1:10201/v1/auth/callback/google`

## Launch

From `desktop/`:

```bash
./run.sh
```

If you already have the Rust backend running elsewhere:

```bash
OMI_SKIP_BACKEND=1 ./run.sh
```

## Legacy compatibility envs

`FIREBASE_PROJECT_ID` and `GOOGLE_APPLICATION_CREDENTIALS` are now treated as optional in the public launcher.

Leave them blank unless you specifically need the remaining desktop-only compatibility routes that still talk to GCP services.
