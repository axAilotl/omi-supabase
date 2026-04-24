import base64
import copy
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import text


class _Sentinel:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


SERVER_TIMESTAMP = _Sentinel("SERVER_TIMESTAMP")
DELETE_FIELD = _Sentinel("DELETE_FIELD")
_DELETE_RESULT = _Sentinel("_DELETE_RESULT")


@dataclass(frozen=True)
class Increment:
    value: int | float


@dataclass(frozen=True)
class ArrayUnion:
    values: Sequence[Any]


@dataclass(frozen=True)
class ArrayRemove:
    values: Sequence[Any]


@dataclass(frozen=True)
class FieldFilter:
    field_path: str
    op_string: str
    value: Any


@dataclass(frozen=True)
class BaseCompositeFilter:
    operator: str
    filters: Sequence[Any]


class Query:
    ASCENDING = "ASCENDING"
    DESCENDING = "DESCENDING"


def transactional(func):
    return func


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _deepcopy(value: Any) -> Any:
    return copy.deepcopy(value)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return value
    if isinstance(value, Enum):
        return value.value
    return value


def _encode_value(value: Any) -> Any:
    value = _normalize_scalar(value)
    if isinstance(value, datetime):
        return {"__omi_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__omi_type__": "date", "value": value.isoformat()}
    if isinstance(value, bytes):
        return {"__omi_type__": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _encode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _encode_value(value.model_dump())
    if hasattr(value, "dict") and callable(value.dict):
        return _encode_value(value.dict())
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        marker = value.get("__omi_type__")
        if marker == "datetime":
            return datetime.fromisoformat(value["value"])
        if marker == "date":
            return date.fromisoformat(value["value"])
        if marker == "bytes":
            return base64.b64decode(value["value"])
        return {key: _decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def _split_field_path(field_path: str) -> list[str]:
    return [part for part in field_path.split(".") if part]


def _get_nested(data: Any, field_path: str) -> Any:
    current = data
    for part in _split_field_path(field_path):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_nested(data: dict, field_path: str, value: Any) -> None:
    parts = _split_field_path(field_path)
    current = data
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _delete_nested(data: dict, field_path: str) -> None:
    parts = _split_field_path(field_path)
    current = data
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            return
        current = next_value
    current.pop(parts[-1], None)


def _apply_transform(existing: Any, value: Any) -> Any:
    if value is SERVER_TIMESTAMP:
        return _utc_now()
    if isinstance(value, Increment):
        base = existing or 0
        return base + value.value
    if isinstance(value, ArrayUnion):
        items = list(existing or [])
        for item in value.values:
            if item not in items:
                items.append(_deepcopy(item))
        return items
    if isinstance(value, ArrayRemove):
        existing_items = list(existing or [])
        return [item for item in existing_items if item not in value.values]
    return _resolve_value(value)


def _resolve_value(value: Any) -> Any:
    if value is SERVER_TIMESTAMP:
        return _utc_now()
    if isinstance(value, dict):
        return {key: _resolve_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item) for item in value]
    return _normalize_scalar(value)


def _merge_value(existing: Any, incoming: Any) -> Any:
    if incoming is DELETE_FIELD:
        return _DELETE_RESULT
    if incoming is SERVER_TIMESTAMP or isinstance(incoming, (Increment, ArrayUnion, ArrayRemove)):
        return _apply_transform(existing, incoming)
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = _deepcopy(existing)
        for key, value in incoming.items():
            next_value = _merge_value(merged.get(key), value)
            if next_value is _DELETE_RESULT:
                merged.pop(key, None)
            else:
                merged[key] = next_value
        return merged
    return _resolve_value(incoming)


def _apply_update(existing: dict | None, updates: dict) -> dict:
    result = _deepcopy(existing or {})
    for field_path, value in updates.items():
        if value is DELETE_FIELD:
            _delete_nested(result, field_path)
            continue
        current = _get_nested(result, field_path)
        _set_nested(result, field_path, _apply_transform(current, value))
    return result


def _select_field_paths(data: dict, field_paths: Sequence[str]) -> dict:
    selected: dict[str, Any] = {}
    for field_path in field_paths:
        value = _get_nested(data, field_path)
        if value is not None:
            _set_nested(selected, field_path, value)
    return selected


def _matches_single_filter(data: dict, filter_: FieldFilter) -> bool:
    actual = _get_nested(data, filter_.field_path)
    expected = filter_.value
    op = filter_.op_string

    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == "in":
        return actual in expected if isinstance(expected, (list, tuple, set)) else False
    if op == "array_contains":
        return isinstance(actual, list) and expected in actual
    if actual is None or expected is None:
        return False
    if op == ">=":
        return actual >= expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == "<":
        return actual < expected
    raise NotImplementedError(f"Unsupported Firestore filter operator: {op}")


def _matches_filter(data: dict, filter_: Any) -> bool:
    if isinstance(filter_, BaseCompositeFilter):
        operator = filter_.operator.upper()
        if operator != "AND":
            raise NotImplementedError(f"Unsupported Firestore composite operator: {filter_.operator}")
        return all(_matches_filter(data, item) for item in filter_.filters)
    return _matches_single_filter(data, filter_)


def _sortable_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (1, None)
    if isinstance(value, datetime):
        return (0, value.timestamp())
    if isinstance(value, date):
        return (0, value.toordinal())
    if isinstance(value, (int, float, str, bool)):
        return (0, value)
    return (0, json.dumps(_encode_value(value), sort_keys=True))


class DocumentSnapshot:
    def __init__(self, reference: "DocumentReference", data: dict | None):
        self.reference = reference
        self._data = _deepcopy(data) if data is not None else None
        self.id = reference.id
        self.exists = data is not None

    def to_dict(self) -> dict | None:
        return _deepcopy(self._data)

    def get(self, field_path: str, default: Any = None) -> Any:
        if self._data is None:
            return default
        value = _get_nested(self._data, field_path)
        return default if value is None else _deepcopy(value)


class Transaction:
    def __init__(self, client: "SupabaseFirestoreClient"):
        self._client = client

    def update(self, doc_ref: "DocumentReference", updates: dict) -> None:
        doc_ref.update(updates)

    def set(self, doc_ref: "DocumentReference", data: dict, merge: bool = False) -> None:
        doc_ref.set(data, merge=merge)

    def delete(self, doc_ref: "DocumentReference") -> None:
        doc_ref.delete()


class Batch:
    def __init__(self, client: "SupabaseFirestoreClient"):
        self._client = client
        self._operations: list[tuple[str, Any, dict | None, bool]] = []

    def set(self, doc_ref: "DocumentReference", data: dict, merge: bool = False) -> None:
        self._operations.append(("set", doc_ref, _deepcopy(data), merge))

    def update(self, doc_ref: "DocumentReference", updates: dict) -> None:
        self._operations.append(("update", doc_ref, _deepcopy(updates), False))

    def delete(self, doc_ref: "DocumentReference") -> None:
        self._operations.append(("delete", doc_ref, None, False))

    def commit(self) -> None:
        with self._client.engine.begin() as connection:
            for action, doc_ref, payload, merge in self._operations:
                if action == "set":
                    self._client._set_document(connection, doc_ref.path, payload or {}, merge=merge)
                elif action == "update":
                    self._client._update_document(connection, doc_ref.path, payload or {})
                elif action == "delete":
                    self._client._delete_document(connection, doc_ref.path)


class AggregationCountResult:
    def __init__(self, value: int):
        self.value = value


class AggregationQuery:
    def __init__(self, query: "_BaseQuery"):
        self._query = query

    def get(self):
        return [[AggregationCountResult(len(self._query._execute()))]]


class _BaseQuery:
    def __init__(
        self,
        client: "SupabaseFirestoreClient",
        *,
        collection_path: str | None = None,
        collection_id: str | None = None,
        filters: Sequence[Any] | None = None,
        orderings: Sequence[tuple[str, str]] | None = None,
        limit_count: int | None = None,
        offset_count: int = 0,
    ):
        self._client = client
        self._collection_path = collection_path
        self._collection_id = collection_id
        self._filters = list(filters or [])
        self._orderings = list(orderings or [])
        self._limit_count = limit_count
        self._offset_count = offset_count

    def where(self, field_path: str | None = None, op_string: str | None = None, value: Any = None, *, filter=None):
        if filter is None:
            if field_path is None or op_string is None:
                raise TypeError("where() requires either `filter=` or field path/operator/value")
            filter = FieldFilter(field_path, op_string, value)
        return self._clone(filters=[*self._filters, filter])

    def order_by(self, field_path: str, direction: str = Query.ASCENDING):
        return self._clone(orderings=[*self._orderings, (field_path, direction)])

    def limit(self, count: int):
        return self._clone(limit_count=count)

    def offset(self, count: int):
        return self._clone(offset_count=count)

    def select(self, field_paths: Sequence[str]):
        return self

    def count(self) -> AggregationQuery:
        return AggregationQuery(self)

    def stream(self, *args, **kwargs) -> Iterator[DocumentSnapshot]:
        return iter(self._execute())

    def get(self, *args, **kwargs) -> list[DocumentSnapshot]:
        return self._execute()

    def _clone(self, *, filters=None, orderings=None, limit_count=None, offset_count=None):
        return self.__class__(
            self._client,
            collection_path=self._collection_path,
            collection_id=self._collection_id,
            filters=self._filters if filters is None else filters,
            orderings=self._orderings if orderings is None else orderings,
            limit_count=self._limit_count if limit_count is None else limit_count,
            offset_count=self._offset_count if offset_count is None else offset_count,
        )

    def _execute(self) -> list[DocumentSnapshot]:
        snapshots = self._client._query_documents(self._collection_path, self._collection_id)
        filtered = []
        for snapshot in snapshots:
            data = snapshot.to_dict() or {}
            if all(_matches_filter(data, filter_) for filter_ in self._filters):
                filtered.append(snapshot)

        for field_path, direction in reversed(self._orderings):
            reverse = str(direction).upper() == Query.DESCENDING
            filtered.sort(
                key=lambda snapshot: (
                    _sortable_value((snapshot.to_dict() or {}).get(field_path))
                    if "." not in field_path
                    else _sortable_value(_get_nested(snapshot.to_dict() or {}, field_path))
                ),
                reverse=reverse,
            )

        if self._offset_count:
            filtered = filtered[self._offset_count :]
        if self._limit_count is not None:
            filtered = filtered[: self._limit_count]
        return filtered


class QueryReference(_BaseQuery):
    pass


class CollectionReference(_BaseQuery):
    def __init__(
        self,
        client: "SupabaseFirestoreClient",
        collection_path: str,
        *,
        collection_id: str | None = None,
        filters: Sequence[Any] | None = None,
        orderings: Sequence[tuple[str, str]] | None = None,
        limit_count: int | None = None,
        offset_count: int = 0,
    ):
        self.path = collection_path
        self.id = collection_id or collection_path.rsplit("/", 1)[-1]
        super().__init__(
            client,
            collection_path=collection_path,
            collection_id=collection_id,
            filters=filters,
            orderings=orderings,
            limit_count=limit_count,
            offset_count=offset_count,
        )

    def document(self, document_id: str | None = None) -> "DocumentReference":
        if not document_id:
            document_id = str(uuid.uuid4())
        return DocumentReference(self._client, f"{self.path}/{document_id}")

    def add(self, data: dict, document_id: str | None = None):
        doc_ref = self.document(document_id)
        doc_ref.set(data)
        return (None, doc_ref)

    def _clone(self, *, filters=None, orderings=None, limit_count=None, offset_count=None):
        return CollectionReference(
            self._client,
            self.path,
            collection_id=self._collection_id,
            filters=self._filters if filters is None else filters,
            orderings=self._orderings if orderings is None else orderings,
            limit_count=self._limit_count if limit_count is None else limit_count,
            offset_count=self._offset_count if offset_count is None else offset_count,
        )


class DocumentReference:
    def __init__(self, client: "SupabaseFirestoreClient", path: str):
        self._client = client
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def get(self, field_paths: Sequence[str] | None = None, transaction: Transaction | None = None):
        snapshot = self._client._get_document(self.path)
        if field_paths is None or not snapshot.exists:
            return snapshot
        return DocumentSnapshot(self, _select_field_paths(snapshot.to_dict() or {}, field_paths))

    def set(self, data: dict, merge: bool = False):
        with self._client.engine.begin() as connection:
            self._client._set_document(connection, self.path, data, merge=merge)

    def update(self, updates: dict):
        with self._client.engine.begin() as connection:
            self._client._update_document(connection, self.path, updates)

    def delete(self):
        with self._client.engine.begin() as connection:
            self._client._delete_document(connection, self.path)

    def collection(self, collection_id: str) -> CollectionReference:
        return CollectionReference(self._client, f"{self.path}/{collection_id}", collection_id=collection_id)

    def collections(self) -> list[CollectionReference]:
        return self._client._list_subcollections(self.path)


class SupabaseFirestoreClient:
    def __init__(self, engine):
        self.engine = engine

    def collection(self, collection_id: str) -> CollectionReference:
        return CollectionReference(self, collection_id, collection_id=collection_id)

    def collection_group(self, collection_id: str) -> QueryReference:
        return QueryReference(self, collection_id=collection_id)

    def batch(self) -> Batch:
        return Batch(self)

    def transaction(self) -> Transaction:
        return Transaction(self)

    def get_all(self, doc_refs: Iterable[DocumentReference]):
        return [doc_ref.get() for doc_ref in doc_refs]

    def _metadata_for_path(self, path: str) -> dict[str, str | None]:
        parts = path.split("/")
        if len(parts) < 2 or len(parts) % 2 != 0:
            raise ValueError(f"Invalid Firestore document path: {path}")
        collection_path = "/".join(parts[:-1])
        parent_path = "/".join(parts[:-2]) or None
        root_uid = parts[1] if len(parts) >= 2 and parts[0] == "users" else None
        return {
            "collection_path": collection_path,
            "collection_id": parts[-2],
            "doc_id": parts[-1],
            "parent_path": parent_path,
            "root_uid": root_uid,
        }

    def _fetch_row(self, connection, path: str):
        return connection.execute(
            text(
                """
                select path, collection_path, collection_id, doc_id, parent_path, root_uid, data
                from public.firestore_documents
                where path = :path
                """
            ),
            {"path": path},
        ).first()

    def _row_to_snapshot(self, row) -> DocumentSnapshot:
        path = row._mapping["path"]
        raw_data = row._mapping["data"]
        if isinstance(raw_data, str):
            raw_data = json.loads(raw_data)
        data = _decode_value(raw_data)
        return DocumentSnapshot(DocumentReference(self, path), data)

    def _get_document(self, path: str) -> DocumentSnapshot:
        with self.engine.connect() as connection:
            row = self._fetch_row(connection, path)
        if row is None:
            return DocumentSnapshot(DocumentReference(self, path), None)
        return self._row_to_snapshot(row)

    def _write_document(self, connection, path: str, data: dict) -> None:
        metadata = self._metadata_for_path(path)
        connection.execute(
            text(
                """
                insert into public.firestore_documents (
                    path,
                    collection_path,
                    collection_id,
                    doc_id,
                    parent_path,
                    root_uid,
                    data,
                    created_at,
                    updated_at
                )
                values (
                    :path,
                    :collection_path,
                    :collection_id,
                    :doc_id,
                    :parent_path,
                    :root_uid,
                    cast(:data as jsonb),
                    timezone('utc', now()),
                    timezone('utc', now())
                )
                on conflict (path) do update
                set collection_path = excluded.collection_path,
                    collection_id = excluded.collection_id,
                    doc_id = excluded.doc_id,
                    parent_path = excluded.parent_path,
                    root_uid = excluded.root_uid,
                    data = excluded.data,
                    updated_at = timezone('utc', now())
                """
            ),
            {
                "path": path,
                "collection_path": metadata["collection_path"],
                "collection_id": metadata["collection_id"],
                "doc_id": metadata["doc_id"],
                "parent_path": metadata["parent_path"],
                "root_uid": metadata["root_uid"],
                "data": json.dumps(_encode_value(data)),
            },
        )

    def _set_document(self, connection, path: str, data: dict, merge: bool = False) -> None:
        existing_row = self._fetch_row(connection, path)
        existing_data = None
        if existing_row is not None:
            raw = existing_row._mapping["data"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            existing_data = _decode_value(raw)
        if merge and existing_data is not None:
            merged = _deepcopy(existing_data)
            for key, value in data.items():
                next_value = _merge_value(merged.get(key), value)
                if next_value is _DELETE_RESULT:
                    merged.pop(key, None)
                else:
                    merged[key] = next_value
            data = merged
        elif merge:
            data = {key: _merge_value(None, value) for key, value in data.items()}
            data = {key: value for key, value in data.items() if value is not _DELETE_RESULT}
        else:
            data = _resolve_value(data)
        self._write_document(connection, path, data)

    def _update_document(self, connection, path: str, updates: dict) -> None:
        existing_row = self._fetch_row(connection, path)
        if existing_row is None:
            raise KeyError(f"Document does not exist: {path}")
        raw = existing_row._mapping["data"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        existing_data = _decode_value(raw)
        updated = _apply_update(existing_data, updates)
        self._write_document(connection, path, updated)

    def _delete_document(self, connection, path: str) -> None:
        connection.execute(text("delete from public.firestore_documents where path = :path"), {"path": path})

    def _query_documents(self, collection_path: str | None, collection_id: str | None) -> list[DocumentSnapshot]:
        if collection_path is None and collection_id is None:
            return []

        sql = """
            select path, collection_path, collection_id, doc_id, parent_path, root_uid, data
            from public.firestore_documents
        """
        params: dict[str, Any] = {}
        if collection_path is not None:
            sql += " where collection_path = :collection_path"
            params["collection_path"] = collection_path
        elif collection_id is not None:
            sql += " where collection_id = :collection_id"
            params["collection_id"] = collection_id

        with self.engine.connect() as connection:
            rows = connection.execute(text(sql), params).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    def _list_subcollections(self, parent_path: str) -> list[CollectionReference]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    select distinct collection_id
                    from public.firestore_documents
                    where parent_path = :parent_path
                    order by collection_id
                    """
                ),
                {"parent_path": parent_path},
            ).fetchall()
        return [CollectionReference(self, f"{parent_path}/{row[0]}", collection_id=row[0]) for row in rows]


class _FirestoreNamespace:
    Query = Query
    FieldFilter = FieldFilter
    ArrayUnion = ArrayUnion
    ArrayRemove = ArrayRemove
    Increment = Increment
    SERVER_TIMESTAMP = SERVER_TIMESTAMP
    DELETE_FIELD = DELETE_FIELD


firestore = _FirestoreNamespace()
