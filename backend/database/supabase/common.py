import json
from typing import Any

from database._client import engine


def require_engine():
    if engine is None:
        raise RuntimeError("Supabase backend requires SUPABASE_DB_URL or DATABASE_URL")
    return engine


def json_param(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value)


def decode_json_field(value: Any, default: Any):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def row_to_dict(row) -> dict | None:
    if row is None:
        return None
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)
