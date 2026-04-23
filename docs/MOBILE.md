# Mobile Setup

## Android

The Android app is the most practical mobile target for this repo.

## Configure the app

From `app/`:

```bash
cp .env.template .dev.env
```

Set `API_BASE_URL` based on where the app is running:

- Android emulator: `http://10.0.2.2:3030/`
- Physical phone on your LAN: `http://<your-host-ip>:3030/`

Leave the trailing slash in place.

The default template already enables the backend-driven auth flow:

- `USE_WEB_AUTH=true`
- `USE_AUTH_CUSTOM_TOKEN=true`

## Build and run

```bash
cd app
bash setup.sh android
```

Or, once dependencies are installed:

```bash
flutter pub get
dart run build_runner build
flutter run --flavor dev
```

## Custom backend URL

If the app is already installed, you can also point it at a self-hosted backend from inside the app:

- open Developer Settings
- set Custom Backend URL
- use a value ending in `/`
- restart the app

This is the easiest path when moving from emulator to physical device or from one LAN host to another.

## Google sign-in

The app still finishes auth at:

- `omi://auth/callback`

But Google itself redirects to the backend first:

- `http://127.0.0.1:3030/v1/auth/callback/google`

So the Google OAuth app only needs the backend callback URI.
