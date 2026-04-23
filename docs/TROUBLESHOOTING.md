# Troubleshooting

## Google returns `invalid_client`

Your Google OAuth client ID or secret does not match what the backend is using.

Check:

- `backend/.env`
- `supabase/.env`
- Google OAuth redirect URI includes `http://127.0.0.1:3030/v1/auth/callback/google`

## Web login popup succeeds but nothing happens

Make sure the web app is pointed at the local backend:

- `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:3030`
- `NEXT_PUBLIC_WS_BASE_URL=ws://127.0.0.1:3030`

Then hard-refresh the browser or retry in a private window.

## Web login shows `Failed to fetch`

This is usually CORS or a dead backend.

Check:

```bash
curl http://127.0.0.1:3030/v1/health
```

The backend should also allow your web origin through `CORS_ALLOWED_ORIGINS`.

## Mobile app reaches the backend but auth is flaky after restart

Use a build that includes the Supabase auth-session fixes from this repo.

If you are debugging an already-installed build:

- clear app data or sign out
- confirm the backend URL ends with `/`
- confirm the phone can reach `http://<host-ip>:3030/v1/health`

## Android physical device cannot reach `localhost`

Use your host LAN IP, not `127.0.0.1`.

Examples:

- emulator: `http://10.0.2.2:3030/`
- phone on LAN: `http://192.168.x.y:3030/`

## Web build fails because Firebase messaging env vars are missing

That should no longer happen in this repo. The web build now falls back to a no-op service worker when the messaging env vars are absent.

If it still fails, remove `web/app/public/firebase-messaging-sw.js` and rerun:

```bash
cd web/app
npm run build
```
