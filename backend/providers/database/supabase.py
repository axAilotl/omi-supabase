import hashlib
import os
import uuid
from functools import lru_cache

from sqlalchemy import create_engine, text

from database.firestore_compat import SupabaseFirestoreClient


def _resolve_database_url() -> str | None:
    for env_key in ("SUPABASE_DB_URL", "SUPABASE_DATABASE_URL", "DATABASE_URL"):
        value = os.getenv(env_key)
        if value:
            return value
    return None


@lru_cache(maxsize=1)
def get_engine():
    database_url = _resolve_database_url()
    if not database_url:
        return None
    return create_engine(database_url, future=True, pool_pre_ping=True)


def get_db():
    engine = get_engine()
    if engine is None:
        return None
    return SupabaseFirestoreClient(engine)


def get_users_uid():
    engine = get_engine()
    if engine is None:
        raise RuntimeError("Supabase backend requires SUPABASE_DB_URL or DATABASE_URL")

    with engine.connect() as connection:
        rows = connection.execute(text("select id::text from auth.users"))
        return [str(row[0]) for row in rows]


def document_id_from_seed(seed: str) -> uuid.UUID:
    seed_hash = hashlib.sha256(seed.encode('utf-8')).digest()
    generated_uuid = uuid.UUID(bytes=seed_hash[:16], version=4)
    return str(generated_uuid)
