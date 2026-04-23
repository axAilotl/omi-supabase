# Migration

This repo includes a one-shot migration script for moving an existing Firebase-backed Omi deployment to Supabase.

The detailed operator guide lives here:

- [backend/scripts/migrate/README.md](../backend/scripts/migrate/README.md)

## What the migration script does

- exports Firebase Auth users
- exports Firestore data
- imports users into Supabase Auth
- imports Firestore-shaped data into `public.firestore_documents`
- backfills `public.user_profiles`
- copies storage objects into Supabase Storage
- re-encrypts UID-bound protected payloads when Firebase UIDs become Supabase UUIDs

## Minimum inputs

- Firebase service account JSON
- source Firebase project ID
- target Supabase URL
- target Postgres URL
- target Supabase JWT secret
- the same `ENCRYPTION_SECRET` used by the source backend

## Recommended cutover shape

1. Run a dry-run export/import against a disposable local Supabase stack.
2. Validate counts and decryption.
3. Freeze writes on the Firebase-backed deployment.
4. Run the final export/import.
5. Switch clients to the Supabase-backed backend.

Do not rely on long-lived dual-write here. This branch is built for a cutover, not a permanently mirrored Firebase/Supabase deployment.
