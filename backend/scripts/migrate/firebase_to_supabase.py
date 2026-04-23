#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import copy
import json
import math
import os
import struct
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import firebase_admin
import jwt
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from firebase_admin import auth, credentials
from google.cloud import firestore, storage
from google.oauth2.service_account import Credentials
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


DEFAULT_SOURCE_BUCKETS = [
    "omi-at-home-speech-profiles",
    "omi-at-home-plugin-assets",
    "omi-at-home-private-cloud-sync",
    "omi-at-home-chat-files",
]
DEFAULT_PUBLIC_BUCKETS = {
    "omi-at-home-plugin-assets",
    "omi-at-home-chat-files",
}
USER_ID_FIELDS = {"uid", "user_id", "root_uid"}
JSON_DUMP_KWARGS = {"indent": 2, "sort_keys": True}
NAMESPACE_UUID = uuid.UUID("dc5c1185-e4f3-448f-bcff-6b67ec8a4ec4")
TIMEOUT_SECONDS = 120.0


class _Sentinel:
    def __init__(self, name: str):
        self.name = name


SERVER_TIMESTAMP = _Sentinel("SERVER_TIMESTAMP")
DELETE_FIELD = _Sentinel("DELETE_FIELD")


@dataclass
class SnapshotBundle:
    auth_users: list[dict]
    firestore_documents: list[dict]
    storage_objects: list[dict]
    source_summary: dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate Firebase auth/firestore/storage into local Supabase.")
    parser.add_argument("command", choices=["export", "import", "validate", "run"])
    parser.add_argument("--firebase-creds", required=True, help="Path to the Firebase service-account JSON file.")
    parser.add_argument("--source-project-id", required=True, help="Firebase/GCP project ID.")
    parser.add_argument(
        "--source-bucket",
        action="append",
        dest="source_buckets",
        default=[],
        help="Source GCS bucket to copy. Can be provided multiple times.",
    )
    parser.add_argument(
        "--public-bucket",
        action="append",
        dest="public_buckets",
        default=[],
        help="Bucket that should be public in Supabase. Can be provided multiple times.",
    )
    parser.add_argument("--output-dir", required=True, help="Directory to store exported snapshot files.")
    parser.add_argument("--supabase-url", help="Supabase base URL, e.g. http://127.0.0.1:54321.")
    parser.add_argument("--supabase-db-url", help="Postgres connection URL for the target Supabase database.")
    parser.add_argument("--supabase-jwt-secret", help="JWT signing secret used by the local Supabase auth service.")
    parser.add_argument(
        "--encryption-secret",
        default=os.getenv("ENCRYPTION_SECRET", ""),
        help="Omi ENCRYPTION_SECRET used to re-encrypt UID-derived payloads during import.",
    )
    parser.add_argument("--reset-target", action="store_true", help="Wipe target auth/data before importing.")
    parser.add_argument(
        "--storage-workers",
        type=int,
        default=8,
        help="Number of concurrent workers to use while copying storage objects.",
    )
    return parser.parse_args()


def source_buckets_from_args(args: argparse.Namespace) -> list[str]:
    if args.source_buckets:
        return args.source_buckets
    return list(DEFAULT_SOURCE_BUCKETS)


def public_buckets_from_args(args: argparse.Namespace) -> set[str]:
    if args.public_buckets:
        return set(args.public_buckets)
    return set(DEFAULT_PUBLIC_BUCKETS)


def output_dir_from_args(args: argparse.Namespace) -> Path:
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, **JSON_DUMP_KWARGS) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def snapshot_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "auth_users": output_dir / "auth_users.json",
        "firestore_documents": output_dir / "firestore_documents.jsonl",
        "storage_objects": output_dir / "storage_objects.jsonl",
        "source_summary": output_dir / "source_summary.json",
        "user_map": output_dir / "user_map.json",
        "import_summary": output_dir / "import_summary.json",
        "validation": output_dir / "validation.json",
    }


def load_snapshot(output_dir: Path) -> SnapshotBundle:
    paths = snapshot_paths(output_dir)
    return SnapshotBundle(
        auth_users=read_json(paths["auth_users"]),
        firestore_documents=read_jsonl(paths["firestore_documents"]),
        storage_objects=read_jsonl(paths["storage_objects"]),
        source_summary=read_json(paths["source_summary"]),
    )


def save_snapshot(output_dir: Path, bundle: SnapshotBundle) -> None:
    paths = snapshot_paths(output_dir)
    write_json(paths["auth_users"], bundle.auth_users)
    write_jsonl(paths["firestore_documents"], bundle.firestore_documents)
    write_jsonl(paths["storage_objects"], bundle.storage_objects)
    write_json(paths["source_summary"], bundle.source_summary)


def initialize_firebase_app(creds_path: Path, project_id: str):
    app_name = f"firebase-migration-{int(time.time())}"
    return firebase_admin.initialize_app(
        credentials.Certificate(str(creds_path)),
        {"projectId": project_id},
        name=app_name,
    )


def encode_firestore_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return {"__omi_type__": "datetime", "value": value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, date):
        return {"__omi_type__": "date", "value": value.isoformat()}
    if isinstance(value, bytes):
        return {"__omi_type__": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Enum):
        return value.value
    if value is SERVER_TIMESTAMP:
        return {"__omi_type__": "server_timestamp"}
    if value is DELETE_FIELD:
        return {"__omi_type__": "delete_field"}
    if isinstance(value, dict):
        return {str(key): encode_firestore_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_firestore_value(item) for item in value]
    return value


def decode_datetime_marker(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    if value.get("__omi_type__") != "datetime":
        return None
    raw = value.get("value")
    if not isinstance(raw, str):
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_firestore_snapshot(db: firestore.Client) -> list[dict]:
    documents: list[dict] = []

    def walk_collection(collection_ref) -> None:
        for snapshot in collection_ref.stream():
            path = snapshot.reference.path
            parts = path.split("/")
            collection_path = "/".join(parts[:-1])
            parent_path = "/".join(parts[:-2]) or None
            documents.append(
                {
                    "path": path,
                    "collection_path": collection_path,
                    "collection_id": collection_ref.id,
                    "doc_id": snapshot.id,
                    "parent_path": parent_path,
                    "root_uid": parts[1] if len(parts) >= 2 and parts[0] == "users" else None,
                    "data": encode_firestore_value(snapshot.to_dict() or {}),
                }
            )
            for subcollection in snapshot.reference.collections():
                walk_collection(subcollection)

    for collection_ref in db.collections():
        walk_collection(collection_ref)

    return sorted(documents, key=lambda item: item["path"])


def build_auth_snapshot(app) -> list[dict]:
    auth_users: list[dict] = []
    for user in auth.list_users(app=app).iterate_all():
        auth_users.append(
            {
                "firebase_uid": user.uid,
                "email": user.email,
                "email_verified": user.email_verified,
                "display_name": user.display_name,
                "photo_url": user.photo_url,
                "phone_number": user.phone_number,
                "disabled": user.disabled,
                "created_at": to_iso(
                    user.user_metadata.creation_timestamp
                    and datetime.fromtimestamp(user.user_metadata.creation_timestamp / 1000, tz=timezone.utc)
                ),
                "last_sign_in_at": to_iso(
                    user.user_metadata.last_sign_in_timestamp
                    and datetime.fromtimestamp(user.user_metadata.last_sign_in_timestamp / 1000, tz=timezone.utc)
                ),
                "provider_data": [
                    {
                        "provider_id": provider.provider_id,
                        "uid": provider.uid,
                        "email": provider.email,
                        "display_name": provider.display_name,
                        "photo_url": provider.photo_url,
                    }
                    for provider in user.provider_data
                ],
                "custom_claims": user.custom_claims or {},
            }
        )
    return auth_users


def build_storage_manifest(storage_client: storage.Client, bucket_names: list[str]) -> list[dict]:
    manifest: list[dict] = []
    for bucket_name in bucket_names:
        for blob in storage_client.list_blobs(bucket_name):
            manifest.append(
                {
                    "bucket": bucket_name,
                    "path": blob.name,
                    "size": blob.size,
                    "content_type": blob.content_type or "application/octet-stream",
                    "updated_at": to_iso(blob.updated),
                }
            )
    return sorted(manifest, key=lambda item: (item["bucket"], item["path"]))


def export_snapshot(args: argparse.Namespace) -> SnapshotBundle:
    creds_path = Path(args.firebase_creds).expanduser().resolve()
    google_credentials = Credentials.from_service_account_file(str(creds_path))
    firebase_app = initialize_firebase_app(creds_path, args.source_project_id)
    firestore_db = firestore.Client(project=args.source_project_id, credentials=google_credentials)
    storage_client = storage.Client(project=args.source_project_id, credentials=google_credentials)

    auth_users = build_auth_snapshot(firebase_app)
    firestore_documents = build_firestore_snapshot(firestore_db)
    storage_objects = build_storage_manifest(storage_client, source_buckets_from_args(args))

    collection_counts = Counter(item["collection_path"] for item in firestore_documents)
    bucket_counts = Counter(item["bucket"] for item in storage_objects)
    source_summary = {
        "source_project_id": args.source_project_id,
        "exported_at": to_iso(utc_now()),
        "auth_user_count": len(auth_users),
        "firestore_document_count": len(firestore_documents),
        "firestore_collection_counts": dict(sorted(collection_counts.items())),
        "storage_object_count": len(storage_objects),
        "storage_bucket_counts": dict(sorted(bucket_counts.items())),
    }
    firebase_admin.delete_app(firebase_app)
    return SnapshotBundle(
        auth_users=auth_users,
        firestore_documents=firestore_documents,
        storage_objects=storage_objects,
        source_summary=source_summary,
    )


def create_engine_or_raise(database_url: str | None) -> Engine:
    if not database_url:
        raise SystemExit("--supabase-db-url is required for import/validate/run")
    return create_engine(database_url, future=True, pool_pre_ping=True)


def service_role_token(jwt_secret: str | None) -> str:
    if not jwt_secret:
        raise SystemExit("--supabase-jwt-secret is required for import/run")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "supabase",
            "sub": "service_role",
            "role": "service_role",
            "aud": "authenticated",
            "iat": now,
            "exp": now + 86400,
        },
        jwt_secret,
        algorithm="HS256",
    )


def supabase_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "apikey": token,
        "Content-Type": "application/json",
    }


def encryption_secret_bytes(raw_secret: str | None) -> bytes:
    secret = (raw_secret or "").encode("utf-8")
    if len(secret) < 32:
        raise SystemExit("--encryption-secret (or ENCRYPTION_SECRET) must be set and at least 32 bytes long")
    return secret


def derive_uid_key(secret: bytes, uid: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=uid.encode("utf-8"),
        info=b"user-data-encryption",
    )
    return hkdf.derive(secret)


def encrypt_string_for_uid(value: str, uid: str, secret: bytes) -> str:
    if not value:
        return value
    key = derive_uid_key(secret, uid)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, value.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def decrypt_string_for_uid(value: str, uid: str, secret: bytes) -> str:
    if not value:
        return value
    key = derive_uid_key(secret, uid)
    aesgcm = AESGCM(key)
    encrypted_payload = base64.b64decode(value.encode("utf-8"))
    nonce = encrypted_payload[:12]
    ciphertext = encrypted_payload[12:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def encrypt_audio_chunk_for_uid(data: bytes, uid: str, secret: bytes) -> bytes:
    key = derive_uid_key(secret, uid)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    encrypted_payload = nonce + ciphertext
    return struct.pack(">I", len(encrypted_payload)) + encrypted_payload


def decrypt_audio_file_for_uid(encrypted_data: bytes, uid: str, secret: bytes) -> bytes:
    key = derive_uid_key(secret, uid)
    aesgcm = AESGCM(key)
    decrypted_audio = bytearray()
    offset = 0
    while offset < len(encrypted_data):
        length = struct.unpack(">I", encrypted_data[offset : offset + 4])[0]
        offset += 4
        encrypted_payload = encrypted_data[offset : offset + length]
        offset += length
        nonce = encrypted_payload[:12]
        ciphertext = encrypted_payload[12:]
        decrypted_audio.extend(aesgcm.decrypt(nonce, ciphertext, None))
    return bytes(decrypted_audio)


def reencrypt_string_field(data: dict, field_name: str, old_uid: str, new_uid: str, secret: bytes) -> None:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        return
    try:
        plaintext = decrypt_string_for_uid(value, old_uid, secret)
        data[field_name] = encrypt_string_for_uid(plaintext, new_uid, secret)
    except Exception:
        return


def reencrypt_document_fields(
    collection_id: str, data: dict, old_uid: str | None, new_uid: str | None, secret: bytes
) -> dict:
    if not old_uid or not new_uid or old_uid == new_uid:
        return data
    if data.get("data_protection_level") != "enhanced":
        return data

    rewritten = copy.deepcopy(data)
    if collection_id == "conversations":
        reencrypt_string_field(rewritten, "transcript_segments", old_uid, new_uid, secret)
    elif collection_id == "photos":
        reencrypt_string_field(rewritten, "base64", old_uid, new_uid, secret)
    elif collection_id == "memories":
        reencrypt_string_field(rewritten, "content", old_uid, new_uid, secret)
    elif collection_id == "messages":
        reencrypt_string_field(rewritten, "text", old_uid, new_uid, secret)
    elif collection_id == "phone_calls":
        reencrypt_string_field(rewritten, "phone_number", old_uid, new_uid, secret)
    return rewritten


def rewrite_path_segments(path: str, user_map: dict[str, str]) -> str:
    return "/".join(user_map.get(segment, segment) for segment in path.split("/"))


def public_storage_url(storage_base_url: str, bucket_name: str, object_path: str) -> str:
    return f"{storage_base_url.rstrip('/')}/object/public/{quote(bucket_name, safe='')}/{quote(object_path.lstrip('/'), safe='/')}"


def rewrite_value(value: Any, user_map: dict[str, str], storage_base_url: str, public_buckets: set[str]) -> Any:
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            rewritten_key = key
            if key in USER_ID_FIELDS and isinstance(item, str) and item in user_map:
                rewritten[rewritten_key] = user_map[item]
                continue
            rewritten[rewritten_key] = rewrite_value(item, user_map, storage_base_url, public_buckets)
        return rewritten
    if isinstance(value, list):
        return [rewrite_value(item, user_map, storage_base_url, public_buckets) for item in value]
    if not isinstance(value, str):
        return value

    if value.startswith("https://storage.googleapis.com/"):
        _, _, remainder = value.partition("https://storage.googleapis.com/")
        bucket_name, _, object_path = remainder.partition("/")
        rewritten_object_path = rewrite_path_segments(object_path, user_map)
        if bucket_name in public_buckets:
            return public_storage_url(storage_base_url, bucket_name, rewritten_object_path)
        value = rewritten_object_path

    rewritten_value = value
    for old_uid, new_uid in user_map.items():
        rewritten_value = rewritten_value.replace(old_uid, new_uid)
    return rewritten_value


def rewrite_firestore_documents(
    firestore_documents: list[dict],
    user_map: dict[str, str],
    storage_base_url: str,
    public_buckets: set[str],
    secret: bytes,
) -> list[dict]:
    rewritten_documents: list[dict] = []
    for document in firestore_documents:
        original_parts = document["path"].split("/")
        old_uid = original_parts[1] if len(original_parts) >= 2 and original_parts[0] == "users" else None
        rewritten_path = rewrite_path_segments(document["path"], user_map)
        rewritten_data = rewrite_value(copy.deepcopy(document["data"]), user_map, storage_base_url, public_buckets)
        rewritten_parts = rewritten_path.split("/")
        new_uid = rewritten_parts[1] if len(rewritten_parts) >= 2 and rewritten_parts[0] == "users" else None
        rewritten_data = reencrypt_document_fields(document["collection_id"], rewritten_data, old_uid, new_uid, secret)
        rewritten_documents.append(
            {
                "path": rewritten_path,
                "collection_path": "/".join(rewritten_parts[:-1]),
                "collection_id": rewritten_parts[-2] if len(rewritten_parts) >= 2 else document["collection_id"],
                "doc_id": rewritten_parts[-1],
                "parent_path": "/".join(rewritten_parts[:-2]) or None,
                "root_uid": infer_root_uid(rewritten_path, rewritten_data),
                "data": rewritten_data,
            }
        )
    return rewritten_documents


def infer_root_uid(path: str, data: dict) -> str | None:
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "users":
        return parts[1]
    for field_name in ("root_uid", "uid", "user_id"):
        value = data.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def rewrite_storage_manifest(storage_objects: list[dict], user_map: dict[str, str]) -> list[dict]:
    rewritten_objects: list[dict] = []
    for item in storage_objects:
        rewritten = dict(item)
        original_parts = item["path"].split("/")
        old_uid = None
        if item["bucket"] == "omi-at-home-private-cloud-sync" and len(original_parts) >= 3:
            if original_parts[0] in {"chunks", "audio", "merged"}:
                old_uid = original_parts[1]
        elif item["bucket"] == "omi-at-home-speech-profiles" and original_parts:
            old_uid = original_parts[0]
        rewritten["source_path"] = item["path"]
        rewritten["target_path"] = rewrite_path_segments(item["path"], user_map)
        rewritten["source_uid"] = old_uid
        rewritten["target_uid"] = user_map.get(old_uid) if old_uid else None
        rewritten_objects.append(rewritten)
    return rewritten_objects


def delete_target_object(supabase_url: str, admin_token: str, bucket_name: str, object_path: str) -> None:
    response = requests.delete(
        f"{supabase_url.rstrip('/')}/storage/v1/object/{quote(bucket_name, safe='')}/{quote(object_path.lstrip('/'), safe='/')}",
        headers={
            "Authorization": f"Bearer {admin_token}",
            "apikey": admin_token,
        },
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


def reset_target_storage(engine: Engine, supabase_url: str, admin_token: str, bucket_names: Iterable[str]) -> None:
    bucket_names = list(bucket_names)
    if not bucket_names:
        return
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                select bucket_id, name
                from storage.objects
                where bucket_id = any(:bucket_ids)
                order by bucket_id, name
                """
            ),
            {"bucket_ids": bucket_names},
        ).fetchall()
    for bucket_id, object_path in rows:
        delete_target_object(supabase_url, admin_token, str(bucket_id), str(object_path))


def reset_target(engine: Engine, supabase_url: str, admin_token: str, bucket_names: Iterable[str]) -> None:
    reset_target_storage(engine, supabase_url, admin_token, bucket_names)
    with engine.begin() as connection:
        connection.execute(text("delete from public.chat_messages"))
        connection.execute(text("delete from public.chat_sessions"))
        connection.execute(text("delete from public.user_profiles"))
        connection.execute(text("delete from public.firestore_documents"))
        connection.execute(text("delete from auth.identities"))
        connection.execute(text("delete from auth.sessions"))
        connection.execute(text("delete from auth.refresh_tokens"))
        connection.execute(text("delete from auth.one_time_tokens"))
        connection.execute(text("delete from auth.users"))


def find_existing_auth_user(engine: Engine, firebase_user: dict) -> str | None:
    email = firebase_user.get("email")
    query = text(
        """
        select id::text
        from auth.users
        where raw_user_meta_data ->> 'legacy_firebase_uid' = :firebase_uid
        order by created_at asc
        limit 1
        """
    )
    params = {"firebase_uid": firebase_user["firebase_uid"]}
    if email is not None:
        query = text(
            """
            select id::text
            from auth.users
            where raw_user_meta_data ->> 'legacy_firebase_uid' = :firebase_uid
               or email = :email
            order by created_at asc
            limit 1
            """
        )
        params["email"] = email

    with engine.connect() as connection:
        row = connection.execute(query, params).first()
    return str(row[0]) if row else None


def upsert_auth_user(
    engine: Engine,
    supabase_url: str,
    admin_token: str,
    firebase_user: dict,
) -> str:
    existing_id = find_existing_auth_user(engine, firebase_user)
    email = firebase_user.get("email") or f"firebase-import+{firebase_user['firebase_uid']}@invalid.local"
    user_metadata = {
        "full_name": firebase_user.get("display_name") or "",
        "avatar_url": firebase_user.get("photo_url"),
        "legacy_firebase_uid": firebase_user["firebase_uid"],
        "firebase_provider_data": firebase_user.get("provider_data") or [],
    }
    payload = {
        "email": email,
        "email_confirm": True,
        "user_metadata": user_metadata,
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "ban_duration": "none",
    }
    headers = supabase_headers(admin_token)
    if existing_id:
        response = requests.put(
            f"{supabase_url.rstrip('/')}/auth/v1/admin/users/{existing_id}",
            headers=headers,
            data=json.dumps(payload),
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        return body["id"]

    response = requests.post(
        f"{supabase_url.rstrip('/')}/auth/v1/admin/users",
        headers=headers,
        data=json.dumps(payload),
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return body["id"]


def import_auth_users(
    engine: Engine,
    supabase_url: str,
    admin_token: str,
    auth_users: list[dict],
) -> dict[str, str]:
    user_map: dict[str, str] = {}
    for firebase_user in auth_users:
        target_uid = upsert_auth_user(engine, supabase_url, admin_token, firebase_user)
        user_map[firebase_user["firebase_uid"]] = target_uid
    return user_map


def insert_firestore_documents(engine: Engine, documents: list[dict], batch_size: int = 250) -> None:
    statement = text(
        """
        insert into public.firestore_documents (
            path,
            collection_path,
            collection_id,
            doc_id,
            parent_path,
            root_uid,
            data
        ) values (
            :path,
            :collection_path,
            :collection_id,
            :doc_id,
            :parent_path,
            :root_uid,
            cast(:data as jsonb)
        )
        on conflict (path) do update set
            collection_path = excluded.collection_path,
            collection_id = excluded.collection_id,
            doc_id = excluded.doc_id,
            parent_path = excluded.parent_path,
            root_uid = excluded.root_uid,
            data = excluded.data,
            updated_at = timezone('utc', now())
        """
    )
    for index in range(0, len(documents), batch_size):
        batch = documents[index : index + batch_size]
        payload = [
            {
                "path": item["path"],
                "collection_path": item["collection_path"],
                "collection_id": item["collection_id"],
                "doc_id": item["doc_id"],
                "parent_path": item["parent_path"],
                "root_uid": item["root_uid"],
                "data": json.dumps(item["data"]),
            }
            for item in batch
        ]
        with engine.begin() as connection:
            connection.execute(statement, payload)


def normalize_uuid_str(raw_value: str | None, seed: str) -> str | None:
    if raw_value is None:
        return None
    try:
        return str(uuid.UUID(str(raw_value)))
    except (ValueError, TypeError):
        return str(uuid.uuid5(NAMESPACE_UUID, seed))


def backfill_user_profiles(engine: Engine, user_map: dict[str, str], documents_by_path: dict[str, dict]) -> None:
    statement = text(
        """
        insert into public.user_profiles (
            id,
            data_protection_level,
            agent_vm,
            language,
            onboarding,
            migration_status,
            created_at,
            updated_at
        ) values (
            cast(:id as uuid),
            :data_protection_level,
            cast(:agent_vm as jsonb),
            :language,
            cast(:onboarding as jsonb),
            cast(:migration_status as jsonb),
            :created_at,
            :updated_at
        )
        on conflict (id) do update set
            data_protection_level = excluded.data_protection_level,
            agent_vm = excluded.agent_vm,
            language = excluded.language,
            onboarding = excluded.onboarding,
            migration_status = excluded.migration_status,
            updated_at = excluded.updated_at
        """
    )
    rows: list[dict] = []
    for target_uid in user_map.values():
        document = documents_by_path.get(f"users/{target_uid}")
        if document is None:
            continue
        data = document["data"]
        created_at = decode_datetime_marker(data.get("created_at")) or utc_now()
        updated_at = decode_datetime_marker(data.get("updated_at")) or created_at
        rows.append(
            {
                "id": target_uid,
                "data_protection_level": data.get("data_protection_level") or "enhanced",
                "agent_vm": json.dumps(data.get("agentVm") or data.get("agent_vm")),
                "language": data.get("language") or "",
                "onboarding": json.dumps(data.get("onboarding") or {}),
                "migration_status": json.dumps(data.get("migration_status")),
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )

    if not rows:
        return
    with engine.begin() as connection:
        connection.execute(statement, rows)


def backfill_chat_tables(engine: Engine, rewritten_documents: list[dict]) -> dict[str, int]:
    chat_sessions_rows: list[dict] = []
    chat_messages_rows: list[dict] = []
    for document in rewritten_documents:
        parts = document["path"].split("/")
        if len(parts) != 4 or parts[0] != "users":
            continue
        user_id = normalize_uuid_str(parts[1], parts[1])
        if user_id is None:
            continue
        data = document["data"]
        if parts[2] == "chat_sessions":
            chat_sessions_rows.append(
                {
                    "id": normalize_uuid_str(data.get("id") or parts[3], document["path"]),
                    "user_id": user_id,
                    "title": data.get("title"),
                    "preview": data.get("preview"),
                    "message_count": data.get("message_count", 0),
                    "starred": data.get("starred", False),
                    "created_at": decode_datetime_marker(data.get("created_at")) or utc_now(),
                    "updated_at": decode_datetime_marker(data.get("updated_at")) or utc_now(),
                    "app_id": data.get("app_id"),
                    "plugin_id": data.get("plugin_id") or data.get("app_id"),
                    "openai_thread_id": data.get("openai_thread_id"),
                    "openai_assistant_id": data.get("openai_assistant_id"),
                    "message_ids": json.dumps(data.get("message_ids") or []),
                    "file_ids": json.dumps(data.get("file_ids") or []),
                }
            )
        if parts[2] == "messages":
            chat_messages_rows.append(
                {
                    "id": normalize_uuid_str(data.get("id") or parts[3], document["path"]),
                    "user_id": user_id,
                    "chat_session_id": normalize_uuid_str(
                        data.get("chat_session_id"),
                        f"{document['path']}:chat_session_id",
                    ),
                    "text": data.get("text", ""),
                    "created_at": decode_datetime_marker(data.get("created_at")) or utc_now(),
                    "sender": data.get("sender") or "unknown",
                    "app_id": data.get("app_id"),
                    "plugin_id": data.get("plugin_id") or data.get("app_id"),
                    "from_external_integration": data.get("from_external_integration", False),
                    "type": data.get("type") or "text",
                    "memories_id": json.dumps(data.get("memories_id") or []),
                    "files_id": json.dumps(data.get("files_id") or []),
                    "data_protection_level": data.get("data_protection_level"),
                    "reported": data.get("reported", False),
                    "report_reason": data.get("report_reason"),
                    "metadata": data.get("metadata"),
                    "rating": data.get("rating"),
                    "langsmith_run_id": data.get("langsmith_run_id"),
                    "prompt_name": data.get("prompt_name"),
                    "prompt_commit": data.get("prompt_commit"),
                    "chart_data": json.dumps(data.get("chart_data")),
                }
            )

    if chat_sessions_rows:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    insert into public.chat_sessions (
                        id, user_id, title, preview, message_count, starred, created_at, updated_at,
                        app_id, plugin_id, openai_thread_id, openai_assistant_id, message_ids, file_ids
                    ) values (
                        cast(:id as uuid), cast(:user_id as uuid), :title, :preview, :message_count, :starred,
                        :created_at, :updated_at, :app_id, :plugin_id, :openai_thread_id, :openai_assistant_id,
                        cast(:message_ids as jsonb), cast(:file_ids as jsonb)
                    )
                    on conflict (id) do update set
                        title = excluded.title,
                        preview = excluded.preview,
                        message_count = excluded.message_count,
                        starred = excluded.starred,
                        updated_at = excluded.updated_at,
                        app_id = excluded.app_id,
                        plugin_id = excluded.plugin_id,
                        openai_thread_id = excluded.openai_thread_id,
                        openai_assistant_id = excluded.openai_assistant_id,
                        message_ids = excluded.message_ids,
                        file_ids = excluded.file_ids
                    """
                ),
                chat_sessions_rows,
            )

    if chat_messages_rows:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    insert into public.chat_messages (
                        id, user_id, chat_session_id, text, created_at, sender, app_id, plugin_id,
                        from_external_integration, type, memories_id, files_id, data_protection_level,
                        reported, report_reason, metadata, rating, langsmith_run_id, prompt_name,
                        prompt_commit, chart_data
                    ) values (
                        cast(:id as uuid), cast(:user_id as uuid), cast(:chat_session_id as uuid), :text,
                        :created_at, :sender, :app_id, :plugin_id, :from_external_integration, :type,
                        cast(:memories_id as jsonb), cast(:files_id as jsonb), :data_protection_level,
                        :reported, :report_reason, :metadata, :rating, :langsmith_run_id, :prompt_name,
                        :prompt_commit, cast(:chart_data as jsonb)
                    )
                    on conflict (id) do update set
                        chat_session_id = excluded.chat_session_id,
                        text = excluded.text,
                        sender = excluded.sender,
                        app_id = excluded.app_id,
                        plugin_id = excluded.plugin_id,
                        from_external_integration = excluded.from_external_integration,
                        type = excluded.type,
                        memories_id = excluded.memories_id,
                        files_id = excluded.files_id,
                        data_protection_level = excluded.data_protection_level,
                        reported = excluded.reported,
                        report_reason = excluded.report_reason,
                        metadata = excluded.metadata,
                        rating = excluded.rating,
                        langsmith_run_id = excluded.langsmith_run_id,
                        prompt_name = excluded.prompt_name,
                        prompt_commit = excluded.prompt_commit,
                        chart_data = excluded.chart_data
                    """
                ),
                chat_messages_rows,
            )

    return {
        "chat_sessions_backfilled": len(chat_sessions_rows),
        "chat_messages_backfilled": len(chat_messages_rows),
    }


def ensure_bucket(supabase_url: str, admin_token: str, bucket_name: str, public: bool) -> None:
    response = requests.post(
        f"{supabase_url.rstrip('/')}/storage/v1/bucket",
        headers=supabase_headers(admin_token),
        data=json.dumps({"id": bucket_name, "name": bucket_name, "public": public}),
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code not in (200, 201, 400, 409):
        response.raise_for_status()


def copy_one_storage_object(
    source_credentials_path: Path,
    source_project_id: str,
    supabase_url: str,
    admin_token: str,
    source_object: dict,
    secret: bytes,
) -> dict:
    storage_client = storage.Client(
        project=source_project_id,
        credentials=Credentials.from_service_account_file(str(source_credentials_path)),
    )
    bucket = storage_client.bucket(source_object["bucket"])
    blob = bucket.blob(source_object["source_path"])
    payload = blob.download_as_bytes()
    if (
        source_object["bucket"] == "omi-at-home-private-cloud-sync"
        and source_object["source_path"].endswith((".enc", ".batch.enc"))
        and source_object.get("source_uid")
        and source_object.get("target_uid")
        and source_object["source_uid"] != source_object["target_uid"]
    ):
        decrypted = decrypt_audio_file_for_uid(payload, source_object["source_uid"], secret)
        payload = encrypt_audio_chunk_for_uid(decrypted, source_object["target_uid"], secret)
    upload_headers = {
        "Authorization": f"Bearer {admin_token}",
        "apikey": admin_token,
        "Content-Type": source_object.get("content_type") or "application/octet-stream",
        "x-upsert": "true",
    }
    response = requests.post(
        f"{supabase_url.rstrip('/')}/storage/v1/object/{quote(source_object['bucket'], safe='')}/{quote(source_object['target_path'].lstrip('/'), safe='/')}",
        headers=upload_headers,
        data=payload,
        timeout=max(TIMEOUT_SECONDS, math.ceil(len(payload) / 524288) * 30),
    )
    response.raise_for_status()
    return {
        "bucket": source_object["bucket"],
        "path": source_object["target_path"],
        "size": source_object["size"],
    }


def import_storage_objects(
    args: argparse.Namespace,
    admin_token: str,
    storage_objects: list[dict],
    public_buckets: set[str],
    secret: bytes,
) -> dict[str, Any]:
    supabase_url = args.supabase_url
    if not supabase_url:
        raise SystemExit("--supabase-url is required for import/run")

    for bucket_name in sorted({item["bucket"] for item in storage_objects}):
        ensure_bucket(supabase_url, admin_token, bucket_name, bucket_name in public_buckets)

    copied_objects: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.storage_workers)) as executor:
        futures = {
            executor.submit(
                copy_one_storage_object,
                Path(args.firebase_creds).expanduser().resolve(),
                args.source_project_id,
                supabase_url,
                admin_token,
                item,
                secret,
            ): item
            for item in storage_objects
        }
        for future in as_completed(futures):
            copied_objects.append(future.result())

    bucket_counts = Counter(item["bucket"] for item in copied_objects)
    return {
        "storage_objects_copied": len(copied_objects),
        "storage_bucket_counts": dict(sorted(bucket_counts.items())),
    }


def query_count_map(engine: Engine, sql: str) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(text(sql)).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def validate_import(
    engine: Engine,
    snapshot: SnapshotBundle,
    rewritten_documents: list[dict],
    rewritten_storage_objects: list[dict],
    user_map: dict[str, str],
) -> dict[str, Any]:
    firestore_counts = query_count_map(
        engine,
        """
        select collection_path, count(*)
        from public.firestore_documents
        group by collection_path
        order by collection_path
        """,
    )
    expected_firestore_counts = Counter(item["collection_path"] for item in rewritten_documents)
    expected_storage_counts = Counter(item["bucket"] for item in rewritten_storage_objects)
    expected_bucket_names = sorted(expected_storage_counts.keys())

    with engine.connect() as connection:
        auth_user_count = int(connection.execute(text("select count(*) from auth.users")).scalar_one())
        user_profiles_count = int(connection.execute(text("select count(*) from public.user_profiles")).scalar_one())
        firestore_document_count = int(
            connection.execute(text("select count(*) from public.firestore_documents")).scalar_one()
        )
        chat_sessions_count = int(connection.execute(text("select count(*) from public.chat_sessions")).scalar_one())
        chat_messages_count = int(connection.execute(text("select count(*) from public.chat_messages")).scalar_one())
        storage_rows = connection.execute(
            text(
                """
                select bucket_id, count(*)
                from storage.objects
                where bucket_id = any(:bucket_ids)
                group by bucket_id
                order by bucket_id
                """
            ),
            {"bucket_ids": expected_bucket_names},
        ).fetchall()
    storage_counts = {str(row[0]): int(row[1]) for row in storage_rows}

    mismatches: list[str] = []
    if auth_user_count != len(snapshot.auth_users):
        mismatches.append(f"auth.users count mismatch: expected {len(snapshot.auth_users)} got {auth_user_count}")
    if user_profiles_count != len(user_map):
        mismatches.append(f"user_profiles count mismatch: expected {len(user_map)} got {user_profiles_count}")
    if firestore_document_count != len(rewritten_documents):
        mismatches.append(
            f"firestore_documents count mismatch: expected {len(rewritten_documents)} got {firestore_document_count}"
        )
    if firestore_counts != dict(sorted(expected_firestore_counts.items())):
        mismatches.append("firestore collection counts mismatch")
    if storage_counts != dict(sorted(expected_storage_counts.items())):
        mismatches.append("storage bucket counts mismatch")

    return {
        "validated_at": to_iso(utc_now()),
        "auth_user_count": auth_user_count,
        "user_profiles_count": user_profiles_count,
        "firestore_document_count": firestore_document_count,
        "chat_sessions_count": chat_sessions_count,
        "chat_messages_count": chat_messages_count,
        "expected_firestore_collection_counts": dict(sorted(expected_firestore_counts.items())),
        "actual_firestore_collection_counts": firestore_counts,
        "expected_storage_bucket_counts": dict(sorted(expected_storage_counts.items())),
        "actual_storage_bucket_counts": storage_counts,
        "mismatches": mismatches,
        "status": "ok" if not mismatches else "failed",
    }


def import_snapshot(
    args: argparse.Namespace, snapshot: SnapshotBundle
) -> tuple[dict[str, str], list[dict], list[dict], dict[str, Any]]:
    engine = create_engine_or_raise(args.supabase_db_url)
    supabase_url = args.supabase_url
    if not supabase_url:
        raise SystemExit("--supabase-url is required for import/run")
    admin_token = service_role_token(args.supabase_jwt_secret)
    public_buckets = public_buckets_from_args(args)
    secret = encryption_secret_bytes(args.encryption_secret)

    if args.reset_target:
        reset_target(engine, supabase_url, admin_token, {item["bucket"] for item in snapshot.storage_objects})

    user_map = import_auth_users(engine, supabase_url, admin_token, snapshot.auth_users)
    storage_base_url = f"{supabase_url.rstrip('/')}/storage/v1"
    rewritten_documents = rewrite_firestore_documents(
        snapshot.firestore_documents,
        user_map,
        storage_base_url,
        public_buckets,
        secret,
    )
    rewritten_storage_objects = rewrite_storage_manifest(snapshot.storage_objects, user_map)

    insert_firestore_documents(engine, rewritten_documents)
    documents_by_path = {item["path"]: item for item in rewritten_documents}
    backfill_user_profiles(engine, user_map, documents_by_path)
    backfill_summary = backfill_chat_tables(engine, rewritten_documents)
    storage_summary = import_storage_objects(args, admin_token, rewritten_storage_objects, public_buckets, secret)

    import_summary = {
        "imported_at": to_iso(utc_now()),
        "user_map": user_map,
        "firestore_documents_imported": len(rewritten_documents),
        **backfill_summary,
        **storage_summary,
    }
    return user_map, rewritten_documents, rewritten_storage_objects, import_summary


def main() -> int:
    args = parse_args()
    output_dir = output_dir_from_args(args)
    paths = snapshot_paths(output_dir)

    if args.command in {"export", "run"}:
        snapshot = export_snapshot(args)
        save_snapshot(output_dir, snapshot)
    else:
        snapshot = load_snapshot(output_dir)

    if args.command == "export":
        return 0

    if args.command in {"import", "run"}:
        user_map, rewritten_documents, rewritten_storage_objects, import_summary = import_snapshot(args, snapshot)
        write_json(paths["user_map"], user_map)
        write_json(paths["import_summary"], import_summary)
    else:
        user_map = read_json(paths["user_map"])
        secret = encryption_secret_bytes(args.encryption_secret)
        rewritten_documents = rewrite_firestore_documents(
            snapshot.firestore_documents,
            user_map,
            f"{args.supabase_url.rstrip('/')}/storage/v1",
            public_buckets_from_args(args),
            secret,
        )
        rewritten_storage_objects = rewrite_storage_manifest(snapshot.storage_objects, user_map)

    validation = validate_import(
        create_engine_or_raise(args.supabase_db_url),
        snapshot,
        rewritten_documents,
        rewritten_storage_objects,
        user_map,
    )
    write_json(paths["validation"], validation)
    if validation["status"] != "ok":
        print(json.dumps(validation, **JSON_DUMP_KWARGS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
