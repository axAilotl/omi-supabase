import datetime
import io
import json
import mimetypes
import os
import struct
import wave
from concurrent.futures import as_completed
from email.utils import parsedate_to_datetime
from typing import List
from urllib.parse import quote, unquote, urlparse

import httpx

try:
    import opuslib
except ImportError:
    opuslib = None

from database import users as users_db
from database.redis_db import cache_signed_url, get_cached_signed_url
from utils import encryption
from utils.executors import storage_executor
import logging

logger = logging.getLogger(__name__)

# Opus encoding constants
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_DURATION_MS = 20
OPUS_FRAME_SIZE = OPUS_SAMPLE_RATE * OPUS_FRAME_DURATION_MS // 1000

PRIVATE_CLOUD_EXTENSIONS = ['.batch.enc', '.batch.bin', '.opus.enc', '.opus', '.enc', '.bin']

speech_profiles_bucket = os.getenv('BUCKET_SPEECH_PROFILES', 'speech-profiles')
postprocessing_audio_bucket = os.getenv('BUCKET_POSTPROCESSING', 'postprocessing')
memories_recordings_bucket = os.getenv('BUCKET_MEMORIES_RECORDINGS', 'memories-recordings')
private_cloud_sync_bucket = os.getenv('BUCKET_PRIVATE_CLOUD_SYNC', 'private-cloud-sync')
syncing_local_bucket = os.getenv('BUCKET_TEMPORAL_SYNC_LOCAL', 'temporal-sync-local')
omi_apps_bucket = os.getenv('BUCKET_PLUGINS_LOGOS', 'plugins-logos')
app_thumbnails_bucket = os.getenv('BUCKET_APP_THUMBNAILS', 'app-thumbnails')
chat_files_bucket = os.getenv('BUCKET_CHAT_FILES', 'chat-files')
desktop_updates_bucket = os.getenv('BUCKET_DESKTOP_UPDATES', 'desktop-updates')

_PUBLIC_BUCKETS = {omi_apps_bucket, app_thumbnails_bucket, chat_files_bucket}
_ENSURED_BUCKETS: dict[str, bool] = {}
_REQUEST_TIMEOUT_SECONDS = 120.0


def _require_opuslib() -> None:
    if opuslib is None:
        raise RuntimeError('opuslib is required for Opus chunk encoding/decoding')


def _resolve_storage_base_url() -> str:
    if storage_url := os.getenv('SUPABASE_STORAGE_URL'):
        return storage_url.rstrip('/')
    if supabase_url := os.getenv('SUPABASE_URL'):
        return f"{supabase_url.rstrip('/')}/storage/v1"
    raise RuntimeError('Supabase storage requires SUPABASE_STORAGE_URL or SUPABASE_URL')


def _resolve_service_role_key() -> str:
    for env_key in ('SUPABASE_SERVICE_ROLE_KEY', 'SERVICE_ROLE_KEY', 'SECRET_KEY'):
        if value := os.getenv(env_key):
            return value
    raise RuntimeError('Supabase storage requires SUPABASE_SERVICE_ROLE_KEY, SERVICE_ROLE_KEY, or SECRET_KEY')


def _default_headers(content_type: str | None = None, upsert: bool = False, cache_control: str | None = None) -> dict:
    service_role_key = _resolve_service_role_key()
    headers = {
        'Authorization': f'Bearer {service_role_key}',
        'apikey': service_role_key,
    }
    if content_type:
        headers['Content-Type'] = content_type
    if upsert:
        headers['x-upsert'] = 'true'
    if cache_control:
        headers['cache-control'] = cache_control
    return headers


def _request(
    method: str,
    api_path: str,
    *,
    content_type: str | None = None,
    upsert: bool = False,
    cache_control: str | None = None,
    allow_not_found: bool = False,
    expected_statuses: tuple[int, ...] = (200,),
    headers: dict | None = None,
    **kwargs,
):
    request_headers = _default_headers(content_type=content_type, upsert=upsert, cache_control=cache_control)
    if headers:
        request_headers.update(headers)

    response = httpx.request(
        method,
        f"{_resolve_storage_base_url()}{api_path}",
        headers=request_headers,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        **kwargs,
    )

    if allow_not_found and response.status_code in (400, 404):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 404 or str(payload.get('statusCode')) == '404':
            return None
    if response.status_code not in expected_statuses:
        response.raise_for_status()
    return response


def _bucket_path(bucket_name: str) -> str:
    return quote(bucket_name, safe='')


def _object_path(path: str) -> str:
    return quote(path.lstrip('/'), safe='/')


def _object_api_path(bucket_name: str, path: str) -> str:
    return f"/object/{_bucket_path(bucket_name)}/{_object_path(path)}"


def _public_object_url(bucket_name: str, path: str) -> str:
    return f"{_resolve_storage_base_url()}/object/public/{_bucket_path(bucket_name)}/{_object_path(path)}"


def _is_public_bucket(bucket_name: str) -> bool:
    return bucket_name in _PUBLIC_BUCKETS


def _ensure_bucket(bucket_name: str, public: bool | None = None) -> None:
    public = _is_public_bucket(bucket_name) if public is None else public
    if _ENSURED_BUCKETS.get(bucket_name) == public:
        return

    response = _request(
        'POST',
        '/bucket',
        content_type='application/json',
        expected_statuses=(200, 201, 400, 409),
        json={'id': bucket_name, 'name': bucket_name, 'public': public},
    )
    if response.status_code not in (200, 201, 400, 409):
        response.raise_for_status()
    _ENSURED_BUCKETS[bucket_name] = public


def _list_objects(bucket_name: str, prefix: str) -> list[dict]:
    _ensure_bucket(bucket_name)
    normalized_prefix = prefix.strip('/')
    items: list[dict] = []
    offset = 0

    while True:
        response = _request(
            'POST',
            f"/object/list/{_bucket_path(bucket_name)}",
            content_type='application/json',
            json={'prefix': normalized_prefix, 'limit': 1000, 'offset': offset},
        )
        payload = response.json()
        if not payload:
            break

        for item in payload:
            name = item.get('name') or ''
            full_path = f'{normalized_prefix}/{name}' if normalized_prefix else name
            items.append({**item, 'path': full_path})

        if len(payload) < 1000:
            break
        offset += len(payload)

    return items


def _path_exists(bucket_name: str, path: str) -> bool:
    _ensure_bucket(bucket_name)
    response = _request('GET', _object_api_path(bucket_name, path), allow_not_found=True)
    return response is not None


def _get_last_modified(bucket_name: str, path: str) -> datetime.datetime | None:
    _ensure_bucket(bucket_name)
    response = _request('GET', _object_api_path(bucket_name, path), allow_not_found=True)
    if response is None:
        return None

    last_modified = response.headers.get('last-modified')
    if not last_modified:
        return None
    parsed = parsedate_to_datetime(last_modified)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _upload_bytes(
    bucket_name: str,
    path: str,
    data: bytes,
    *,
    content_type: str = 'application/octet-stream',
    cache_control: str | None = None,
    public: bool | None = None,
) -> None:
    _ensure_bucket(bucket_name, public=public)
    _request(
        'POST',
        _object_api_path(bucket_name, path),
        content_type=content_type,
        upsert=True,
        cache_control=cache_control,
        expected_statuses=(200,),
        content=data,
    )


def _upload_file(
    bucket_name: str,
    path: str,
    file_path: str,
    *,
    content_type: str | None = None,
    cache_control: str | None = None,
    public: bool | None = None,
) -> None:
    with open(file_path, 'rb') as file_handle:
        data = file_handle.read()
    _upload_bytes(
        bucket_name,
        path,
        data,
        content_type=content_type or mimetypes.guess_type(file_path)[0] or 'application/octet-stream',
        cache_control=cache_control,
        public=public,
    )


def _download_bytes(bucket_name: str, path: str) -> bytes:
    _ensure_bucket(bucket_name)
    response = _request('GET', _object_api_path(bucket_name, path))
    return response.content


def _download_to_filename(bucket_name: str, path: str, destination_file_path: str) -> None:
    os.makedirs(os.path.dirname(destination_file_path) or '.', exist_ok=True)
    with open(destination_file_path, 'wb') as destination:
        destination.write(_download_bytes(bucket_name, path))


def _delete_object(bucket_name: str, path: str) -> bool:
    _ensure_bucket(bucket_name)
    response = _request('DELETE', _object_api_path(bucket_name, path), allow_not_found=True)
    return response is not None


def _delete_prefix(bucket_name: str, prefix: str) -> None:
    for item in _list_objects(bucket_name, prefix):
        item_path = item.get('path')
        if not item_path:
            continue
        if item.get('id') is None:
            _delete_prefix(bucket_name, item_path)
            continue
        _delete_object(bucket_name, item_path)


def _get_signed_url(bucket_name: str, path: str, minutes: int) -> str:
    cache_key = f'{bucket_name}/{path}'
    if cached := get_cached_signed_url(cache_key):
        return cached

    _ensure_bucket(bucket_name)
    response = _request(
        'POST',
        f"/object/sign/{_bucket_path(bucket_name)}/{_object_path(path)}",
        content_type='application/json',
        json={'expiresIn': minutes * 60},
    )
    signed_path = response.json()['signedURL']
    signed_url = signed_path if signed_path.startswith('http') else f"{_resolve_storage_base_url()}{signed_path}"
    cache_signed_url(cache_key, signed_url, minutes * 60)
    return signed_url


# *******************************************
# ************* SPEECH PROFILE **************
# *******************************************
def upload_profile_audio(file_path: str, uid: str):
    path = f'{uid}/speech_profile.wav'
    _upload_file(speech_profiles_bucket, path, file_path, content_type='audio/wav')
    return _get_signed_url(speech_profiles_bucket, path, 60)


def get_user_has_speech_profile(uid: str, max_age_days: int = None) -> bool:
    path = f'{uid}/speech_profile.wav'
    if not _path_exists(speech_profiles_bucket, path):
        return False

    if max_age_days is not None:
        last_modified = _get_last_modified(speech_profiles_bucket, path)
        if last_modified is None:
            return False
        age = datetime.datetime.now(datetime.timezone.utc) - last_modified
        if age.days > max_age_days:
            return False

    return True


def get_profile_audio_if_exists(uid: str, download: bool = True) -> str:
    path = f'{uid}/speech_profile.wav'
    if not _path_exists(speech_profiles_bucket, path):
        return None

    if download:
        file_path = f'_temp/{uid}_speech_profile.wav'
        _download_to_filename(speech_profiles_bucket, path, file_path)
        return file_path

    return _get_signed_url(speech_profiles_bucket, path, 60)


def delete_additional_profile_audio(uid: str, file_name: str) -> None:
    _delete_object(speech_profiles_bucket, f'{uid}/additional_profile_recordings/{file_name}')


def get_additional_profile_recordings(uid: str, download: bool = False) -> List[str]:
    prefix = f'{uid}/additional_profile_recordings'
    items = [item for item in _list_objects(speech_profiles_bucket, prefix) if item.get('id') is not None]
    if download:
        paths = []
        for item in items:
            file_name = item['path'].split('/')[-1]
            file_path = f'_temp/{uid}_{file_name}'
            _download_to_filename(speech_profiles_bucket, item['path'], file_path)
            paths.append(file_path)
        return paths

    return [_get_signed_url(speech_profiles_bucket, item['path'], 60) for item in items]


# ********************************************
# ************* PEOPLE PROFILES **************
# ********************************************
def delete_user_person_speech_sample(uid: str, person_id: str, file_name: str) -> None:
    _delete_object(speech_profiles_bucket, f'{uid}/people_profiles/{person_id}/{file_name}')


def delete_user_person_speech_samples(uid: str, person_id: str) -> None:
    _delete_prefix(speech_profiles_bucket, f'{uid}/people_profiles/{person_id}')


def upload_person_speech_sample_from_bytes(
    audio_bytes: bytes,
    uid: str,
    person_id: str,
    sample_rate: int = 16000,
) -> str:
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_bytes)

    filename = f"{os.urandom(16).hex()}.wav"
    path = f'{uid}/people_profiles/{person_id}/{filename}'
    _upload_bytes(speech_profiles_bucket, path, wav_buffer.getvalue(), content_type='audio/wav')
    return path


def get_user_people_ids(uid: str) -> List[str]:
    prefix = f'{uid}/people_profiles'
    people_ids = set()
    for item in _list_objects(speech_profiles_bucket, prefix):
        name = item.get('name') or ''
        if name:
            people_ids.add(name.split('/', 1)[0])
    return list(people_ids)


def get_user_person_speech_samples(uid: str, person_id: str, download: bool = False) -> List[str]:
    prefix = f'{uid}/people_profiles/{person_id}'
    items = [item for item in _list_objects(speech_profiles_bucket, prefix) if item.get('id') is not None]
    if download:
        paths = []
        for item in items:
            file_name = item['path'].split('/')[-1]
            file_path = f'_temp/{uid}_person_{file_name}'
            _download_to_filename(speech_profiles_bucket, item['path'], file_path)
            paths.append(file_path)
        return paths

    return [_get_signed_url(speech_profiles_bucket, item['path'], 60) for item in items]


def get_speech_sample_signed_urls(paths: List[str]) -> List[str]:
    return [_get_signed_url(speech_profiles_bucket, path, 60) for path in paths]


# ********************************************
# ************* POST PROCESSING **************
# ********************************************
def upload_postprocessing_audio(file_path: str):
    _upload_file(postprocessing_audio_bucket, file_path, file_path)
    return _get_signed_url(postprocessing_audio_bucket, file_path, 60)


def delete_postprocessing_audio(file_path: str):
    _delete_object(postprocessing_audio_bucket, file_path)


# ***********************************
# ************* SDCARD **************
# ***********************************
def upload_sdcard_audio(file_path: str):
    path = f'sdcard/{file_path}'
    _upload_file(postprocessing_audio_bucket, path, file_path)
    return _get_signed_url(postprocessing_audio_bucket, path, 60)


def download_postprocessing_audio(file_path: str, destination_file_path: str):
    _download_to_filename(postprocessing_audio_bucket, file_path, destination_file_path)


# ************************************************
# *********** CONVERSATIONS RECORDINGS ***********
# ************************************************
def upload_conversation_recording(file_path: str, uid: str, conversation_id: str):
    path = f'{uid}/{conversation_id}.wav'
    _upload_file(memories_recordings_bucket, path, file_path, content_type='audio/wav')
    return _get_signed_url(memories_recordings_bucket, path, 60)


def get_conversation_recording_if_exists(uid: str, memory_id: str) -> str:
    logger.info(f'get_conversation_recording_if_exists {uid} {memory_id}')
    path = f'{uid}/{memory_id}.wav'
    if not _path_exists(memories_recordings_bucket, path):
        return None
    file_path = f'_temp/{memory_id}.wav'
    _download_to_filename(memories_recordings_bucket, path, file_path)
    return file_path


def delete_all_conversation_recordings(uid: str):
    if not uid:
        return
    _delete_prefix(memories_recordings_bucket, uid)


# ********************************************
# ************* SYNCING FILES ****************
# ********************************************
def get_syncing_file_temporal_url(file_path: str):
    _upload_file(syncing_local_bucket, file_path, file_path)
    return _get_signed_url(syncing_local_bucket, file_path, 15)


def get_syncing_file_temporal_signed_url(file_path: str):
    _upload_file(syncing_local_bucket, file_path, file_path)
    return _get_signed_url(syncing_local_bucket, file_path, 15)


def delete_syncing_temporal_file(file_path: str):
    _delete_object(syncing_local_bucket, file_path)


# ************************************************
# *********** PRIVATE CLOUD SYNC *****************
# ************************************************
def encode_pcm_to_opus(pcm_data: bytes, sample_rate: int = OPUS_SAMPLE_RATE, channels: int = OPUS_CHANNELS) -> bytes:
    _require_opuslib()
    encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
    frame_size = sample_rate * OPUS_FRAME_DURATION_MS // 1000
    bytes_per_frame = frame_size * channels * 2

    packets = []
    offset = 0
    while offset + bytes_per_frame <= len(pcm_data):
        frame = pcm_data[offset : offset + bytes_per_frame]
        packets.append(encoder.encode(frame, frame_size))
        offset += bytes_per_frame

    if offset < len(pcm_data):
        remaining = pcm_data[offset:]
        padded = remaining + b'\x00' * (bytes_per_frame - len(remaining))
        packets.append(encoder.encode(padded, frame_size))

    output = struct.pack('<I', len(packets))
    output += struct.pack('<I', len(pcm_data))
    for packet in packets:
        output += struct.pack('<H', len(packet)) + packet
    return output


def decode_opus_to_pcm(opus_data: bytes, sample_rate: int = OPUS_SAMPLE_RATE, channels: int = OPUS_CHANNELS) -> bytes:
    _require_opuslib()
    if len(opus_data) < 8:
        raise ValueError(f"Opus data too short: {len(opus_data)} bytes (need at least 8 for header)")

    decoder = opuslib.Decoder(sample_rate, channels)
    frame_size = sample_rate * OPUS_FRAME_DURATION_MS // 1000

    offset = 0
    packet_count = struct.unpack_from('<I', opus_data, offset)[0]
    offset += 4
    original_pcm_len = struct.unpack_from('<I', opus_data, offset)[0]
    offset += 4

    pcm_parts = []
    for packet_index in range(packet_count):
        if offset + 2 > len(opus_data):
            raise ValueError(
                f"Truncated Opus data: expected packet {packet_index}/{packet_count} length at offset {offset}"
            )
        packet_len = struct.unpack_from('<H', opus_data, offset)[0]
        offset += 2
        if offset + packet_len > len(opus_data):
            raise ValueError(
                f"Truncated Opus data: packet {packet_index} needs {packet_len} bytes at offset {offset}, "
                f"only {len(opus_data) - offset} available"
            )
        packet_data = opus_data[offset : offset + packet_len]
        offset += packet_len
        pcm_parts.append(decoder.decode(packet_data, frame_size))

    result = b''.join(pcm_parts)
    if 0 < original_pcm_len < len(result):
        result = result[:original_pcm_len]
    return result


def _get_extension_for_path(path: str) -> str:
    if path.endswith('.batch.enc'):
        return 'batch.enc'
    if path.endswith('.batch.bin'):
        return 'batch.bin'
    if path.endswith('.opus.enc'):
        return 'opus.enc'
    if path.endswith('.opus'):
        return 'opus'
    if path.endswith('.enc'):
        return 'enc'
    if path.endswith('.bin'):
        return 'bin'
    return 'bin'


def _strip_extension(filename: str) -> str:
    for extension in ('.batch.enc', '.batch.bin', '.opus.enc', '.opus', '.enc', '.bin'):
        if filename.endswith(extension):
            return filename[: -len(extension)]
    return filename.rsplit('.', 1)[0]


def upload_audio_chunk(
    chunk_data: bytes, uid: str, conversation_id: str, timestamp: float, data_protection_level: str = None
) -> str:
    protection_level = (
        data_protection_level if data_protection_level is not None else users_db.get_data_protection_level(uid)
    )
    formatted_timestamp = f'{timestamp:.3f}'
    upload_data = encode_pcm_to_opus(chunk_data)

    if protection_level == 'enhanced':
        encrypted_chunk = encryption.encrypt_audio_chunk(upload_data, uid)
        path = f'chunks/{uid}/{conversation_id}/{formatted_timestamp}.opus.enc'
        _upload_bytes(private_cloud_sync_bucket, path, encrypted_chunk)
    else:
        path = f'chunks/{uid}/{conversation_id}/{formatted_timestamp}.opus'
        _upload_bytes(private_cloud_sync_bucket, path, upload_data)

    del upload_data
    return path


def upload_audio_chunks_batch(
    chunks: List[dict],
    uid: str,
    conversation_id: str,
    data_protection_level: str = None,
) -> List[str]:
    if not chunks:
        return []

    sorted_chunks = sorted(chunks, key=lambda chunk: chunk['timestamp'])
    protection_level = (
        data_protection_level if data_protection_level is not None else users_db.get_data_protection_level(uid)
    )

    first_ts = f'{sorted_chunks[0]["timestamp"]:.3f}'
    last_ts = f'{sorted_chunks[-1]["timestamp"]:.3f}'
    batch_name = f'{first_ts}-{last_ts}' if len(sorted_chunks) > 1 else first_ts

    if protection_level == 'enhanced':
        path = f'chunks/{uid}/{conversation_id}/{batch_name}.batch.enc'
        buffer = io.BytesIO()
        for chunk in sorted_chunks:
            buffer.write(encryption.encrypt_audio_chunk(chunk['data'], uid))
        payload = buffer.getvalue()
    else:
        path = f'chunks/{uid}/{conversation_id}/{batch_name}.batch.bin'
        payload = b''.join(chunk['data'] for chunk in sorted_chunks)

    _upload_bytes(private_cloud_sync_bucket, path, payload)
    return [path]


def delete_audio_chunks(uid: str, conversation_id: str, timestamps: List[float]) -> None:
    deleted_batch_paths = set()

    for timestamp in timestamps:
        formatted_timestamp = f'{timestamp:.3f}'
        for extension in PRIVATE_CLOUD_EXTENSIONS:
            if extension in ('.batch.enc', '.batch.bin'):
                continue
            chunk_path = f'chunks/{uid}/{conversation_id}/{formatted_timestamp}{extension}'
            _delete_object(private_cloud_sync_bucket, chunk_path)

        for batch_extension in ('.batch.enc', '.batch.bin'):
            batch_path = f'chunks/{uid}/{conversation_id}/{formatted_timestamp}{batch_extension}'
            if batch_path not in deleted_batch_paths and _delete_object(private_cloud_sync_bucket, batch_path):
                deleted_batch_paths.add(batch_path)

    ts_set = {f'{timestamp:.3f}' for timestamp in timestamps}
    prefix = f'chunks/{uid}/{conversation_id}'
    for item in _list_objects(private_cloud_sync_bucket, prefix):
        path = item.get('path') or ''
        if path in deleted_batch_paths or item.get('id') is None:
            continue
        filename = path.split('/')[-1]
        if '.batch.' not in filename:
            continue
        timestamp_str = _strip_extension(filename)
        if '-' in timestamp_str and timestamp_str.split('-', 1)[0] in ts_set:
            _delete_object(private_cloud_sync_bucket, path)
            deleted_batch_paths.add(path)


def list_audio_chunks(uid: str, conversation_id: str) -> List[dict]:
    prefix = f'chunks/{uid}/{conversation_id}'
    chunks = []
    for item in _list_objects(private_cloud_sync_bucket, prefix):
        if item.get('id') is None:
            continue
        filename = (item.get('path') or '').split('/')[-1]
        if not any(filename.endswith(extension) for extension in PRIVATE_CLOUD_EXTENSIONS):
            continue
        try:
            timestamp_str = _strip_extension(filename)
            is_batch = '.batch.' in filename
            timestamp = float(timestamp_str.split('-', 1)[0] if is_batch and '-' in timestamp_str else timestamp_str)
            metadata = item.get('metadata') or {}
            chunks.append(
                {
                    'timestamp': timestamp,
                    'path': item['path'],
                    'size': metadata.get('size') or metadata.get('contentLength') or 0,
                    'is_batch': is_batch,
                }
            )
        except ValueError:
            continue
    return sorted(chunks, key=lambda chunk: chunk['timestamp'])


def delete_conversation_audio_files(uid: str, conversation_id: str) -> None:
    _delete_prefix(private_cloud_sync_bucket, f'chunks/{uid}/{conversation_id}')
    _delete_prefix(private_cloud_sync_bucket, f'audio/{uid}/{conversation_id}')
    _delete_prefix(private_cloud_sync_bucket, f'merged/{uid}/{conversation_id}')


def download_audio_chunks_and_merge(
    uid: str,
    conversation_id: str,
    timestamps: List[float],
    fill_gaps: bool = True,
    sample_rate: int = 16000,
) -> bytes:
    actual_chunks = list_audio_chunks(uid, conversation_id)
    ts_set = {round(timestamp, 3) for timestamp in timestamps}

    batch_paths = {}
    ts_to_batch_path = {}
    for chunk in actual_chunks:
        if chunk.get('is_batch'):
            path = chunk['path']
            batch_paths[path] = chunk
            filename = path.split('/')[-1]
            timestamp_str = _strip_extension(filename)
            if '-' in timestamp_str:
                batch_start, batch_end = timestamp_str.split('-', 1)
                batch_start_ts = float(batch_start)
                batch_end_ts = float(batch_end)
            else:
                batch_start_ts = batch_end_ts = float(timestamp_str)
            for timestamp in timestamps:
                rounded = round(timestamp, 3)
                if batch_start_ts <= rounded <= batch_end_ts:
                    ts_to_batch_path[rounded] = path

    def _download_and_decode_blob(path: str) -> bytes | None:
        extension = _get_extension_for_path(path)
        encrypted = extension in ('opus.enc', 'enc', 'batch.enc')
        is_opus = extension in ('opus.enc', 'opus')

        try:
            chunk_data = _download_bytes(private_cloud_sync_bucket, path)
        except Exception:
            return None

        try:
            raw_data = encryption.decrypt_audio_file(chunk_data, uid) if encrypted else chunk_data
            pcm_data = decode_opus_to_pcm(raw_data, sample_rate=sample_rate) if is_opus else raw_data
            if is_opus:
                del raw_data
            return pcm_data
        except Exception as exc:
            logger.warning(f"Failed to decode/decrypt {path}: {exc}")
            return None

    def download_single_chunk(timestamp: float) -> tuple[float, bytes | None]:
        formatted_timestamp = f'{timestamp:.3f}'
        extensions_to_try = [
            ('opus.enc', True, True),
            ('enc', True, False),
            ('opus', False, True),
            ('bin', False, False),
        ]

        for extension, encrypted, opus_encoded in extensions_to_try:
            chunk_path = f'chunks/{uid}/{conversation_id}/{formatted_timestamp}.{extension}'
            try:
                chunk_data = _download_bytes(private_cloud_sync_bucket, chunk_path)
            except Exception:
                continue

            try:
                raw_data = encryption.decrypt_audio_file(chunk_data, uid) if encrypted else chunk_data
                pcm_data = decode_opus_to_pcm(raw_data, sample_rate=sample_rate) if opus_encoded else raw_data
                if opus_encoded:
                    del raw_data
                return (timestamp, pcm_data)
            except Exception as exc:
                logger.warning(
                    f"Failed to decode/decrypt {extension} chunk at {formatted_timestamp}: {exc}, trying next format"
                )
                continue

        logger.warning(f"Warning: Chunk not found for timestamp {formatted_timestamp}")
        return (timestamp, None)

    chunk_results = {}
    individual_timestamps = [timestamp for timestamp in timestamps if round(timestamp, 3) not in ts_to_batch_path]
    unique_batch_paths = set(ts_to_batch_path.values())

    individual_futures = {
        storage_executor.submit(download_single_chunk, timestamp): timestamp for timestamp in individual_timestamps
    }
    batch_futures = {storage_executor.submit(_download_and_decode_blob, path): path for path in unique_batch_paths}

    for future in as_completed(individual_futures):
        timestamp, pcm_data = future.result()
        if pcm_data is not None:
            chunk_results[timestamp] = pcm_data

    for future in as_completed(batch_futures):
        path = batch_futures[future]
        pcm_data = future.result()
        if pcm_data is not None:
            chunk_results[batch_paths[path]['timestamp']] = pcm_data

    merged_data = bytearray()

    if fill_gaps and timestamps and chunk_results:
        sorted_timestamps = sorted(timestamps)
        current_time = sorted_timestamps[0]
        for timestamp in sorted_timestamps:
            if timestamp not in chunk_results:
                continue

            pcm_data = chunk_results[timestamp]
            gap_seconds = timestamp - current_time
            if gap_seconds > 0:
                gap_samples = int(gap_seconds * sample_rate)
                silence_bytes = bytes(gap_samples * 2)
                merged_data.extend(silence_bytes)
                logger.info(f"Filled {gap_seconds:.3f}s gap ({len(silence_bytes)} bytes) before chunk at {timestamp}")

            merged_data.extend(pcm_data)
            current_time = timestamp + (len(pcm_data) / (sample_rate * 2))
    else:
        for timestamp in timestamps:
            if timestamp in chunk_results:
                merged_data.extend(chunk_results[timestamp])

    chunk_results.clear()
    if not merged_data:
        raise FileNotFoundError(f"No chunks found for conversation {conversation_id}")
    return bytes(merged_data)


def get_cached_merged_audio_path(uid: str, conversation_id: str, audio_file_id: str) -> str:
    return f'merged/{uid}/{conversation_id}/{audio_file_id}.wav'


def get_or_create_merged_audio(
    uid: str,
    conversation_id: str,
    audio_file_id: str,
    timestamps: List[float],
    pcm_to_wav_func,
    fill_gaps: bool = True,
    sample_rate: int = 16000,
) -> tuple[bytes, bool]:
    cache_path = get_cached_merged_audio_path(uid, conversation_id, audio_file_id)
    if _path_exists(private_cloud_sync_bucket, cache_path):
        logger.info(f"Serving merged audio from cache: {cache_path}")
        return _download_bytes(private_cloud_sync_bucket, cache_path), True

    logger.info(f"Cache miss, merging audio for: {cache_path}")
    pcm_data = download_audio_chunks_and_merge(
        uid, conversation_id, timestamps, fill_gaps=fill_gaps, sample_rate=sample_rate
    )
    wav_data = pcm_to_wav_func(pcm_data)
    del pcm_data

    def _upload_to_cache():
        try:
            _upload_bytes(private_cloud_sync_bucket, cache_path, wav_data, content_type='audio/wav')
            logger.info(f"Cached merged audio at: {cache_path}")
        except Exception as exc:
            logger.error(f"Error uploading audio cache: {exc}")

    storage_executor.submit(_upload_to_cache)
    return wav_data, False


def get_merged_audio_signed_url(uid: str, conversation_id: str, audio_file_id: str) -> str | None:
    cache_path = get_cached_merged_audio_path(uid, conversation_id, audio_file_id)
    if not _path_exists(private_cloud_sync_bucket, cache_path):
        return None
    return _get_signed_url(private_cloud_sync_bucket, cache_path, 60)


def delete_cached_merged_audio(uid: str, conversation_id: str) -> None:
    _delete_prefix(private_cloud_sync_bucket, f'merged/{uid}/{conversation_id}')


def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return wav_buffer.getvalue()


def precache_conversation_audio(
    uid: str, conversation_id: str, audio_files: list, fill_gaps: bool = True, sample_rate: int = 16000
) -> None:
    if not audio_files:
        return

    def _precache_all():

        def _cache_single(audio_file):
            try:
                audio_file_id = audio_file.get('id')
                timestamps = audio_file.get('chunk_timestamps')
                if not audio_file_id or not timestamps:
                    return
                get_or_create_merged_audio(
                    uid=uid,
                    conversation_id=conversation_id,
                    audio_file_id=audio_file_id,
                    timestamps=timestamps,
                    pcm_to_wav_func=_pcm_to_wav,
                    fill_gaps=fill_gaps,
                    sample_rate=sample_rate,
                )
            except Exception as exc:
                logger.error(f"[PRECACHE] Error caching audio file {audio_file.get('id')}: {exc}")

        futures = [storage_executor.submit(_cache_single, audio_file) for audio_file in audio_files]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    storage_executor.submit(_precache_all)


# **********************************
# ************* UTILS **************
# **********************************
def download_blob_bytes(bucket_name: str, path: str) -> bytes:
    return _download_bytes(bucket_name, path)


def delete_blob(bucket_name: str, path: str) -> bool:
    return _delete_object(bucket_name, path)


def download_speech_profile_bytes(path: str) -> bytes:
    return download_blob_bytes(speech_profiles_bucket, path)


def delete_speech_profile_blob(path: str) -> bool:
    return delete_blob(speech_profiles_bucket, path)


def upload_app_logo(file_path: str, app_id: str):
    path = f'{app_id}.png'
    _upload_file(
        omi_apps_bucket, path, file_path, content_type='image/png', cache_control='public, no-cache', public=True
    )
    return _public_object_url(omi_apps_bucket, path)


def delete_app_logo(img_url: str):
    parsed = urlparse(img_url)
    public_prefix = f"/object/public/{omi_apps_bucket}/"
    if public_prefix in parsed.path:
        path = unquote(parsed.path.split(public_prefix, 1)[1])
    else:
        path = f"{img_url.split('/')[-1]}"
    logger.info(f'delete_app_logo {path}')
    _delete_object(omi_apps_bucket, path)


def upload_app_thumbnail(file_path: str, thumbnail_id: str) -> str:
    path = f'{thumbnail_id}.jpg'
    _upload_file(
        app_thumbnails_bucket,
        path,
        file_path,
        content_type='image/jpeg',
        cache_control='public, no-cache',
        public=True,
    )
    return _public_object_url(app_thumbnails_bucket, path)


def get_app_thumbnail_url(thumbnail_id: str) -> str:
    return _public_object_url(app_thumbnails_bucket, f'{thumbnail_id}.jpg')


# **********************************
# ************* CHAT FILES *********
# **********************************
def upload_multi_chat_files(files_name: List[str], uid: str) -> dict:
    uploaded = {}
    for name in files_name:
        try:
            object_path = f'{uid}/{name}'
            _upload_file(
                chat_files_bucket,
                object_path,
                f'./{name}',
                cache_control='public, no-cache',
                public=True,
            )
            uploaded[name] = _public_object_url(chat_files_bucket, object_path)
        except Exception as exc:
            logger.error("Failed to upload %s due to exception: %s", name, exc)
    return uploaded


# **************************************************
# ************* DESKTOP UPDATES ********************
# **************************************************
def get_desktop_update_signed_url(blob_path: str, expiration_hours: int = 1) -> str:
    return _get_signed_url(desktop_updates_bucket, blob_path, expiration_hours * 60)
