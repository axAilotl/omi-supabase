import copy
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import text

from database import users as users_db
from models.chat import Message
from utils import encryption

from .common import decode_json_field, json_param, require_engine, row_to_dict


def _decrypt_message(message_data: dict, uid: str) -> dict:
    data = copy.deepcopy(message_data)
    if data.get("data_protection_level") == "enhanced" and isinstance(data.get("text"), str):
        try:
            data["text"] = encryption.decrypt(data["text"], uid)
        except Exception:
            pass
    return data


def _sync_app_and_plugin_id(data: dict) -> dict:
    synced = dict(data)
    app_id = synced.get("app_id")
    plugin_id = synced.get("plugin_id")

    if app_id is not None and plugin_id is None:
        synced["plugin_id"] = app_id
    elif plugin_id is not None and app_id is None:
        synced["app_id"] = plugin_id

    return synced


def _normalize_session_row(row) -> dict | None:
    data = row_to_dict(row)
    if data is None:
        return None

    result = {
        "id": data["id"],
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "title": data.get("title"),
        "preview": data.get("preview"),
        "message_count": data.get("message_count", 0),
        "starred": data.get("starred", False),
        "app_id": data.get("app_id"),
        "plugin_id": data.get("plugin_id"),
        "openai_thread_id": data.get("openai_thread_id"),
        "openai_assistant_id": data.get("openai_assistant_id"),
        "message_ids": decode_json_field(data.get("message_ids"), []),
        "file_ids": decode_json_field(data.get("file_ids"), []),
    }
    return _sync_app_and_plugin_id(result)


def _normalize_message_row(row, uid: str | None = None) -> dict | None:
    data = row_to_dict(row)
    if data is None:
        return None

    result = {
        "id": data["id"],
        "text": data.get("text", ""),
        "created_at": data.get("created_at"),
        "sender": data.get("sender"),
        "type": data.get("type"),
        "app_id": data.get("app_id"),
        "plugin_id": data.get("plugin_id"),
        "from_external_integration": data.get("from_external_integration", False),
        "memories_id": decode_json_field(data.get("memories_id"), []),
        "files_id": decode_json_field(data.get("files_id"), []),
        "chat_session_id": data.get("chat_session_id"),
        "data_protection_level": data.get("data_protection_level"),
        "reported": data.get("reported", False),
        "report_reason": data.get("report_reason"),
        "metadata": data.get("metadata"),
        "rating": data.get("rating"),
        "langsmith_run_id": data.get("langsmith_run_id"),
        "prompt_name": data.get("prompt_name"),
        "prompt_commit": data.get("prompt_commit"),
        "chart_data": decode_json_field(data.get("chart_data"), None),
    }
    result = _sync_app_and_plugin_id(result)
    if uid:
        result = _decrypt_message(result, uid)
    return result


def add_chat_session(uid: str, chat_session_data: dict):
    engine = require_engine()
    session_data = _sync_app_and_plugin_id(dict(chat_session_data))
    created_at = session_data.get("created_at") or datetime.now(timezone.utc)
    updated_at = session_data.get("updated_at") or created_at

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                insert into public.chat_sessions (
                    id,
                    user_id,
                    title,
                    preview,
                    message_count,
                    starred,
                    created_at,
                    updated_at,
                    app_id,
                    plugin_id,
                    openai_thread_id,
                    openai_assistant_id,
                    message_ids,
                    file_ids
                ) values (
                    cast(:id as uuid),
                    cast(:uid as uuid),
                    :title,
                    :preview,
                    :message_count,
                    :starred,
                    :created_at,
                    :updated_at,
                    :app_id,
                    :plugin_id,
                    :openai_thread_id,
                    :openai_assistant_id,
                    cast(:message_ids as jsonb),
                    cast(:file_ids as jsonb)
                )
                """
            ),
            {
                "id": session_data["id"],
                "uid": uid,
                "title": session_data.get("title"),
                "preview": session_data.get("preview"),
                "message_count": session_data.get("message_count", 0),
                "starred": session_data.get("starred", False),
                "created_at": created_at,
                "updated_at": updated_at,
                "app_id": session_data.get("app_id"),
                "plugin_id": session_data.get("plugin_id"),
                "openai_thread_id": session_data.get("openai_thread_id"),
                "openai_assistant_id": session_data.get("openai_assistant_id"),
                "message_ids": json_param(session_data.get("message_ids"), []),
                "file_ids": json_param(session_data.get("file_ids"), []),
            },
        )

    session_data["created_at"] = created_at
    session_data["updated_at"] = updated_at
    session_data.setdefault("message_ids", [])
    session_data.setdefault("file_ids", [])
    return session_data


def create_chat_session(uid: str, title: str = None, app_id: str = None) -> dict:
    now = datetime.now(timezone.utc)
    return add_chat_session(
        uid,
        {
            "id": str(uuid.uuid4()),
            "title": title or "New Chat",
            "preview": None,
            "created_at": now,
            "updated_at": now,
            "app_id": app_id,
            "plugin_id": app_id,
            "message_count": 0,
            "starred": False,
            "message_ids": [],
            "file_ids": [],
        },
    )


def get_chat_session(uid: str, app_id: Optional[str] = None):
    engine = require_engine()
    query = """
        select *
        from public.chat_sessions
        where user_id = cast(:uid as uuid)
          and plugin_id is null
        order by updated_at desc, created_at desc
        limit 1
    """
    if app_id is not None:
        query = """
            select *
            from public.chat_sessions
            where user_id = cast(:uid as uuid)
              and plugin_id = :app_id
            order by updated_at desc, created_at desc
            limit 1
        """

    with engine.connect() as connection:
        row = connection.execute(text(query), {"uid": uid, "app_id": app_id}).first()

    return _normalize_session_row(row)


def get_chat_session_by_id(uid: str, chat_session_id: str):
    engine = require_engine()
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                select *
                from public.chat_sessions
                where user_id = cast(:uid as uuid)
                  and id = cast(:chat_session_id as uuid)
                """
            ),
            {"uid": uid, "chat_session_id": chat_session_id},
        ).first()

    return _normalize_session_row(row)


def acquire_chat_session(uid: str, app_id: str = None) -> str:
    chat_session = get_chat_session(uid, app_id=app_id)
    if chat_session:
        return chat_session["id"]
    return create_chat_session(uid, app_id=app_id)["id"]


def get_chat_sessions(
    uid: str, app_id: str = None, limit: int = 50, offset: int = 0, starred: bool = None
) -> List[dict]:
    engine = require_engine()

    predicates = ["user_id = cast(:uid as uuid)"]
    params = {"uid": uid, "limit": limit, "offset": offset}
    if app_id is None:
        predicates.append("plugin_id is null")
    else:
        predicates.append("plugin_id = :app_id")
        params["app_id"] = app_id
    if starred is not None:
        predicates.append("starred = :starred")
        params["starred"] = starred

    query = f"""
        select *
        from public.chat_sessions
        where {" and ".join(predicates)}
        order by updated_at desc, created_at desc
        limit :limit
        offset :offset
    """
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).fetchall()
    return [_normalize_session_row(row) for row in rows]


def update_chat_session(uid: str, session_id: str, title: str = None, starred: bool = None) -> Optional[dict]:
    updates = []
    params = {"uid": uid, "session_id": session_id}
    if title is not None:
        updates.append("title = :title")
        params["title"] = title
    if starred is not None:
        updates.append("starred = :starred")
        params["starred"] = starred

    if not updates:
        return get_chat_session_by_id(uid, session_id)

    updates.append("updated_at = timezone('utc', now())")
    engine = require_engine()
    with engine.begin() as connection:
        row = connection.execute(
            text(
                f"""
                update public.chat_sessions
                set {", ".join(updates)}
                where user_id = cast(:uid as uuid)
                  and id = cast(:session_id as uuid)
                returning *
                """
            ),
            params,
        ).first()

    return _normalize_session_row(row)


def delete_chat_session(uid, chat_session_id, cascade_messages: bool = False):
    engine = require_engine()
    with engine.begin() as connection:
        if cascade_messages:
            connection.execute(
                text(
                    """
                    delete from public.chat_messages
                    where user_id = cast(:uid as uuid)
                      and chat_session_id = cast(:chat_session_id as uuid)
                    """
                ),
                {"uid": uid, "chat_session_id": chat_session_id},
            )
        connection.execute(
            text(
                """
                delete from public.chat_sessions
                where user_id = cast(:uid as uuid)
                  and id = cast(:chat_session_id as uuid)
                """
            ),
            {"uid": uid, "chat_session_id": chat_session_id},
        )


def _bump_session_metadata(
    connection, uid: str, chat_session_id: str, created_at: datetime, preview: str | None
) -> None:
    connection.execute(
        text(
            """
            update public.chat_sessions
            set message_count = coalesce(message_count, 0) + 1,
                preview = :preview,
                updated_at = :updated_at
            where user_id = cast(:uid as uuid)
              and id = cast(:chat_session_id as uuid)
            """
        ),
        {
            "uid": uid,
            "chat_session_id": chat_session_id,
            "preview": preview,
            "updated_at": created_at,
        },
    )


def add_message(uid: str, message_data: dict):
    engine = require_engine()
    data = _sync_app_and_plugin_id(dict(message_data))
    data.pop("memories", None)
    data.pop("files", None)

    created_at = data.get("created_at") or datetime.now(timezone.utc)
    data_protection_level = data.get("data_protection_level") or users_db.get_data_protection_level(uid)
    text_value = data.get("text", "")
    if data_protection_level == "enhanced" and isinstance(text_value, str):
        text_value = encryption.encrypt(text_value, uid)

    chart_data = data.get("chart_data")
    if hasattr(chart_data, "model_dump"):
        chart_data = chart_data.model_dump()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                insert into public.chat_messages (
                    id,
                    user_id,
                    chat_session_id,
                    text,
                    created_at,
                    sender,
                    app_id,
                    plugin_id,
                    from_external_integration,
                    type,
                    memories_id,
                    files_id,
                    data_protection_level,
                    reported,
                    report_reason,
                    metadata,
                    rating,
                    langsmith_run_id,
                    prompt_name,
                    prompt_commit,
                    chart_data
                ) values (
                    cast(:id as uuid),
                    cast(:uid as uuid),
                    cast(nullif(:chat_session_id, '') as uuid),
                    :text,
                    :created_at,
                    :sender,
                    :app_id,
                    :plugin_id,
                    :from_external_integration,
                    :type,
                    cast(:memories_id as jsonb),
                    cast(:files_id as jsonb),
                    :data_protection_level,
                    :reported,
                    :report_reason,
                    :metadata,
                    :rating,
                    :langsmith_run_id,
                    :prompt_name,
                    :prompt_commit,
                    cast(:chart_data as jsonb)
                )
                """
            ),
            {
                "id": data.get("id") or str(uuid.uuid4()),
                "uid": uid,
                "chat_session_id": data.get("chat_session_id") or "",
                "text": text_value,
                "created_at": created_at,
                "sender": data.get("sender"),
                "app_id": data.get("app_id"),
                "plugin_id": data.get("plugin_id"),
                "from_external_integration": data.get("from_external_integration", False),
                "type": data.get("type"),
                "memories_id": json_param(data.get("memories_id"), []),
                "files_id": json_param(data.get("files_id"), []),
                "data_protection_level": data_protection_level,
                "reported": data.get("reported", False),
                "report_reason": data.get("report_reason"),
                "metadata": data.get("metadata"),
                "rating": data.get("rating"),
                "langsmith_run_id": data.get("langsmith_run_id"),
                "prompt_name": data.get("prompt_name"),
                "prompt_commit": data.get("prompt_commit"),
                "chart_data": json_param(chart_data, None),
            },
        )
        if data.get("chat_session_id"):
            _bump_session_metadata(connection, uid, data["chat_session_id"], created_at, data.get("text", "")[:100])

    data["created_at"] = created_at
    data["data_protection_level"] = data_protection_level
    return data


def add_message_to_chat_session(uid: str, chat_session_id: str, message_id: str):
    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                update public.chat_sessions
                set message_ids = coalesce(message_ids, '[]'::jsonb) || jsonb_build_array(:message_id),
                    updated_at = timezone('utc', now())
                where user_id = cast(:uid as uuid)
                  and id = cast(:chat_session_id as uuid)
                """
            ),
            {"uid": uid, "chat_session_id": chat_session_id, "message_id": message_id},
        )


def add_files_to_chat_session(uid: str, chat_session_id: str, file_ids: List[str]):
    if not file_ids:
        return

    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                update public.chat_sessions
                set file_ids = coalesce(file_ids, '[]'::jsonb) || cast(:file_ids as jsonb),
                    updated_at = timezone('utc', now())
                where user_id = cast(:uid as uuid)
                  and id = cast(:chat_session_id as uuid)
                """
            ),
            {"uid": uid, "chat_session_id": chat_session_id, "file_ids": json_param(file_ids, [])},
        )


def update_chat_session_openai_ids(uid: str, chat_session_id: str, thread_id: str, assistant_id: str):
    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                update public.chat_sessions
                set openai_thread_id = coalesce(:thread_id, openai_thread_id),
                    openai_assistant_id = coalesce(:assistant_id, openai_assistant_id),
                    updated_at = timezone('utc', now())
                where user_id = cast(:uid as uuid)
                  and id = cast(:chat_session_id as uuid)
                """
            ),
            {
                "uid": uid,
                "chat_session_id": chat_session_id,
                "thread_id": thread_id,
                "assistant_id": assistant_id,
            },
        )


def get_messages(
    uid: str,
    limit: int = 20,
    offset: int = 0,
    include_conversations: bool = False,
    app_id: Optional[str] = None,
    chat_session_id: Optional[str] = None,
):
    del include_conversations

    engine = require_engine()
    predicates = ["user_id = cast(:uid as uuid)"]
    params = {"uid": uid, "limit": limit, "offset": offset}

    if chat_session_id:
        predicates.append("chat_session_id = cast(:chat_session_id as uuid)")
        params["chat_session_id"] = chat_session_id
    elif app_id is None:
        predicates.append("plugin_id is null")
    else:
        predicates.append("plugin_id = :app_id")
        params["app_id"] = app_id

    query = f"""
        select *
        from public.chat_messages
        where {" and ".join(predicates)}
          and coalesce(reported, false) = false
        order by created_at desc
        limit :limit
        offset :offset
    """
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).fetchall()

    return [_normalize_message_row(row, uid=uid) for row in rows]


def get_message_count(uid: str) -> int:
    engine = require_engine()
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                select count(*)
                from public.chat_messages
                where user_id = cast(:uid as uuid)
                """
            ),
            {"uid": uid},
        ).first()
    return int(row[0]) if row else 0


def iter_all_messages(uid: str, batch_size: int = 1000):
    offset = 0
    while True:
        batch = get_messages(uid, limit=batch_size, offset=offset)
        if not batch:
            break
        yield from batch
        if len(batch) < batch_size:
            break
        offset += batch_size


def get_message(uid: str, message_id: str) -> tuple[Message, str] | None:
    engine = require_engine()
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                select *
                from public.chat_messages
                where user_id = cast(:uid as uuid)
                  and id = cast(:message_id as uuid)
                limit 1
                """
            ),
            {"uid": uid, "message_id": message_id},
        ).first()

    message_data = _normalize_message_row(row, uid=uid)
    if message_data is None:
        return None
    return Message(**message_data), message_data["id"]


def report_message(uid: str, msg_doc_id: str):
    engine = require_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                update public.chat_messages
                set reported = true
                where user_id = cast(:uid as uuid)
                  and id = cast(:message_id as uuid)
                """
            ),
            {"uid": uid, "message_id": msg_doc_id},
        )
    return {"message": "Message reported"}


def update_message_rating(uid: str, message_id: str, rating: int | None):
    engine = require_engine()
    with engine.begin() as connection:
        result = connection.execute(
            text(
                """
                update public.chat_messages
                set rating = :rating
                where user_id = cast(:uid as uuid)
                  and id = cast(:message_id as uuid)
                """
            ),
            {"uid": uid, "message_id": message_id, "rating": rating},
        )
    return result.rowcount > 0


def delete_messages(uid: str, app_id: str = None, session_id: str = None) -> int:
    engine = require_engine()
    predicates = ["user_id = cast(:uid as uuid)"]
    params = {"uid": uid}
    if session_id:
        predicates.append("chat_session_id = cast(:session_id as uuid)")
        params["session_id"] = session_id
    elif app_id is None:
        predicates.append("plugin_id is null")
    else:
        predicates.append("plugin_id = :app_id")
        params["app_id"] = app_id

    with engine.begin() as connection:
        result = connection.execute(
            text(
                f"""
                delete from public.chat_messages
                where {" and ".join(predicates)}
                """
            ),
            params,
        )
    return result.rowcount


def clear_chat(uid: str, app_id: Optional[str] = None, chat_session_id: Optional[str] = None):
    deleted = delete_messages(uid, app_id=app_id, session_id=chat_session_id)
    return {"message": f"Deleted {deleted} messages"}


def save_message(
    uid: str, text: str, sender: str, app_id: str = None, session_id: str = None, metadata: str = None
) -> dict:
    now = datetime.now(timezone.utc)
    if not session_id:
        session_id = acquire_chat_session(uid, app_id=app_id)

    message = {
        "id": str(uuid.uuid4()),
        "text": text,
        "created_at": now,
        "sender": sender,
        "type": "text",
        "app_id": app_id,
        "plugin_id": app_id,
        "session_id": session_id,
        "chat_session_id": session_id,
        "from_external_integration": False,
        "rating": None,
        "reported": False,
        "memories_id": [],
        "metadata": metadata,
    }
    add_message(uid, message)
    add_message_to_chat_session(uid, session_id, message["id"])
    return {"id": message["id"], "created_at": now.isoformat()}
