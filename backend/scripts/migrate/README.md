# Firebase To Supabase Migration

`firebase_to_supabase.py` migrates a Firebase-backed Omi backend into the Supabase-based `full-supabase` branch.

It handles four things in one workflow:

1. Firebase Auth export and Supabase Auth import.
2. Firestore export into `public.firestore_documents`.
3. `public.user_profiles` backfill from migrated `users/<uid>` docs.
4. Storage copy from GCS into Supabase Storage, including re-encryption of UID-keyed private-cloud-sync blobs.

## Why The Re-encryption Step Exists

Omi's enhanced-protection payloads are encrypted with a key derived from `ENCRYPTION_SECRET + uid`.

Firebase user IDs are arbitrary strings, but Supabase uses UUID user IDs. A straight document or blob copy will preserve ciphertext that can only be decrypted with the old Firebase UID. The migration script rewrites those payloads from the old Firebase UID to the new Supabase UUID during import.

That applies to:

- conversation transcript payloads
- memory content payloads
- chat message payloads if present
- phone-call payloads if present
- private-cloud-sync encrypted audio chunks

## Prerequisites

- Local or remote Supabase target already running.
- Postgres access to the target database.
- Firebase service-account JSON for the source project.
- The same `ENCRYPTION_SECRET` used by the source Omi backend.
- `uv` available on the host.

## One-shot Example

Export:

```bash
uv run \
  --with firebase-admin \
  --with google-cloud-firestore \
  --with google-cloud-storage \
  --with google-auth \
  --with requests \
  --with SQLAlchemy \
  --with 'psycopg[binary]' \
  --with PyJWT \
  python backend/scripts/migrate/firebase_to_supabase.py export \
  --firebase-creds /path/to/google-credentials.json \
  --source-project-id your-firebase-project \
  --output-dir /tmp/omi-firebase-to-supabase
```

Import into local Supabase:

```bash
uv run \
  --with firebase-admin \
  --with google-cloud-firestore \
  --with google-cloud-storage \
  --with google-auth \
  --with requests \
  --with SQLAlchemy \
  --with 'psycopg[binary]' \
  --with PyJWT \
  --with cryptography \
  python backend/scripts/migrate/firebase_to_supabase.py import \
  --firebase-creds /path/to/google-credentials.json \
  --source-project-id your-firebase-project \
  --output-dir /tmp/omi-firebase-to-supabase \
  --supabase-url http://127.0.0.1:54321 \
  --supabase-db-url postgresql+psycopg://postgres:postgres@127.0.0.1:54322/postgres \
  --supabase-jwt-secret super-secret-jwt-token-with-at-least-32-characters-long \
  --encryption-secret "$ENCRYPTION_SECRET" \
  --reset-target
```

Combined export + import + validation:

```bash
uv run \
  --with firebase-admin \
  --with google-cloud-firestore \
  --with google-cloud-storage \
  --with google-auth \
  --with requests \
  --with SQLAlchemy \
  --with 'psycopg[binary]' \
  --with PyJWT \
  --with cryptography \
  python backend/scripts/migrate/firebase_to_supabase.py run \
  --firebase-creds /path/to/google-credentials.json \
  --source-project-id your-firebase-project \
  --output-dir /tmp/omi-firebase-to-supabase \
  --supabase-url http://127.0.0.1:54321 \
  --supabase-db-url postgresql+psycopg://postgres:postgres@127.0.0.1:54322/postgres \
  --supabase-jwt-secret super-secret-jwt-token-with-at-least-32-characters-long \
  --encryption-secret "$ENCRYPTION_SECRET" \
  --reset-target
```

## Generated Files

The output directory contains:

- `auth_users.json`
- `firestore_documents.jsonl`
- `storage_objects.jsonl`
- `source_summary.json`
- `user_map.json`
- `import_summary.json`
- `validation.json`

`user_map.json` is important. It records the old Firebase UID to new Supabase UUID mapping that was used to rewrite document paths, storage paths, and encrypted payloads.

## Target Data Shape

After import:

- Firebase auth users exist in `auth.users`.
- Most application data lives in `public.firestore_documents`.
- `public.user_profiles` is backfilled for backend runtime needs.
- Storage buckets and object paths exist in Supabase Storage with rewritten UUID-owned prefixes.

This branch intentionally keeps a Firestore-compat document layer for unported domains while using native Supabase tables where they already exist.

## Auth Note

The importer creates email-based Supabase users and stores `legacy_firebase_uid` plus Firebase provider metadata in `raw_user_meta_data`.

For Google sign-in later, keep the same email address. Supabase can link a later verified OAuth identity to the imported account instead of creating a second user.

## Validation

The script writes `validation.json` and exits non-zero on count mismatches.

For backend-level smoke checks after import, verify at least:

1. `database.users.get_user_profile(new_uuid)` returns the migrated timezone, language, and transcription preferences.
2. `database.memories.get_memories(new_uuid, limit=1)` returns decrypted plain-text content.
3. A sample `omi-at-home-private-cloud-sync` object decrypts with the new Supabase UUID.

## Buckets

By default the script migrates these source buckets:

- `omi-at-home-speech-profiles`
- `omi-at-home-plugin-assets`
- `omi-at-home-private-cloud-sync`
- `omi-at-home-chat-files`

Override them with repeated `--source-bucket` flags if your deployment differs.

Public buckets can be overridden with repeated `--public-bucket` flags.

## Current Local Migration Result

On this host, the script was used to migrate the running `omi-at-home` Firebase project into the local Supabase stack on `127.0.0.1:54321` / `127.0.0.1:54322`.

The validated import result was:

- 1 auth user
- 483 Firestore documents
- 1 `user_profiles` row
- 2,026 storage objects
