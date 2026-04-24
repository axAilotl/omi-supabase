import json
from typing import Optional

from sqlalchemy import text

from database._client import db

from .common import decode_json_field, json_param, require_engine, row_to_dict


def is_exists_user(uid: str) -> bool:
    engine = require_engine()
    with engine.connect() as connection:
        row = connection.execute(
            text("select 1 from auth.users where id = cast(:uid as uuid)"),
            {"uid": uid},
        ).first()
    return row is not None


def _ensure_user_profile(uid: str) -> None:
    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                insert into public.user_profiles (id)
                values (cast(:uid as uuid))
                on conflict (id) do nothing
                """
            ),
            {"uid": uid},
        )


def _get_compat_user_profile(uid: str) -> dict:
    if db is None:
        return {}

    snapshot = db.collection("users").document(uid).get()
    if not snapshot.exists:
        return {}

    data = snapshot.to_dict() or {}
    data.setdefault("id", uid)
    data.setdefault("uid", uid)
    return data


def get_user_profile(uid: str) -> Optional[dict]:
    if not uid:
        return None

    compat_profile = _get_compat_user_profile(uid)
    if is_exists_user(uid):
        _ensure_user_profile(uid)

    engine = require_engine()
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                select
                    id::text as id,
                    data_protection_level,
                    agent_vm,
                    language,
                    onboarding,
                    migration_status,
                    created_at,
                    updated_at
                from public.user_profiles
                where id = cast(:uid as uuid)
                """
            ),
            {"uid": uid},
        ).first()

    data = row_to_dict(row)
    if data is None and not compat_profile:
        return None

    result = dict(compat_profile)
    result["id"] = uid
    result["uid"] = uid

    native_data_protection_level = "enhanced"
    native_agent_vm = None
    native_language = None
    native_onboarding = None
    native_migration_status = None
    native_created_at = None
    native_updated_at = None
    if data is not None:
        native_data_protection_level = data.get("data_protection_level") or "enhanced"
        native_agent_vm = decode_json_field(data.get("agent_vm"), None)
        native_language = data.get("language")
        native_onboarding = decode_json_field(data.get("onboarding"), None)
        native_migration_status = decode_json_field(data.get("migration_status"), None)
        native_created_at = data.get("created_at")
        native_updated_at = data.get("updated_at")

    compat_agent_vm = result.get("agentVm")
    if compat_agent_vm is None:
        compat_agent_vm = result.get("agent_vm")

    result["data_protection_level"] = native_data_protection_level or result.get("data_protection_level") or "enhanced"
    result["agentVm"] = native_agent_vm if native_agent_vm is not None else compat_agent_vm
    result["language"] = native_language if native_language not in (None, "") else result.get("language", "")
    result["onboarding"] = native_onboarding if native_onboarding is not None else result.get("onboarding", {})
    result["migration_status"] = (
        native_migration_status if native_migration_status is not None else result.get("migration_status")
    )

    if native_created_at is not None:
        result["created_at"] = native_created_at
    elif "created_at" not in result:
        result["created_at"] = None

    if native_updated_at is not None:
        result["updated_at"] = native_updated_at
    elif "updated_at" not in result:
        result["updated_at"] = None

    return result


def get_data_protection_level(uid: str) -> str:
    profile = get_user_profile(uid)
    if profile is None:
        return "enhanced"
    return profile.get("data_protection_level") or "enhanced"


def get_agent_vm(uid: str) -> Optional[dict]:
    profile = get_user_profile(uid)
    if profile is None:
        return None
    agent_vm = profile.get("agentVm")
    return agent_vm if isinstance(agent_vm, dict) and agent_vm else None


def update_agent_vm(uid: str, ip: str | None, status: str) -> Optional[dict]:
    _ensure_user_profile(uid)

    engine = require_engine()
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                select agent_vm
                from public.user_profiles
                where id = cast(:uid as uuid)
                """
            ),
            {"uid": uid},
        ).first()
        current = row_to_dict(row) or {}
        agent_vm = decode_json_field(current.get("agent_vm"), {})
        if not isinstance(agent_vm, dict):
            agent_vm = {}

        agent_vm["status"] = status
        if ip is not None:
            agent_vm["ip"] = ip

        connection.execute(
            text(
                """
                update public.user_profiles
                set agent_vm = cast(:agent_vm as jsonb),
                    updated_at = timezone('utc', now())
                where id = cast(:uid as uuid)
                """
            ),
            {
                "uid": uid,
                "agent_vm": json.dumps(agent_vm),
            },
        )

    return agent_vm


def set_data_protection_level(uid: str, level: str) -> None:
    _ensure_user_profile(uid)

    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                update public.user_profiles
                set data_protection_level = :level,
                    updated_at = timezone('utc', now())
                where id = cast(:uid as uuid)
                """
            ),
            {"uid": uid, "level": level},
        )


def set_migration_status(uid: str, target_level: str) -> None:
    _ensure_user_profile(uid)

    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                update public.user_profiles
                set migration_status = cast(:migration_status as jsonb),
                    updated_at = timezone('utc', now())
                where id = cast(:uid as uuid)
                """
            ),
            {
                "uid": uid,
                "migration_status": json_param(
                    {
                        "target_level": target_level,
                        "status": "in_progress",
                    },
                    {},
                ),
            },
        )


def finalize_migration(uid: str, target_level: str) -> None:
    _ensure_user_profile(uid)

    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                update public.user_profiles
                set data_protection_level = :target_level,
                    migration_status = null,
                    updated_at = timezone('utc', now())
                where id = cast(:uid as uuid)
                """
            ),
            {"uid": uid, "target_level": target_level},
        )
