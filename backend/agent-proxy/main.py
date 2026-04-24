"""
agent-proxy — WebSocket proxy that bridges the mobile app to a user's agent VM.

Auth: backend access token in Authorization header (Bearer <token>) during WS upgrade.
Flow: validate token → fetch VM from the active user store → if stopped, restart → connect to VM WS → bidirectional pump.
History: fetches last 10 agent messages from the active backend store and prepends to prompt.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import google.auth
import google.auth.transport.requests
import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import database.chat as chat_db
import database.users as users_db
from providers.auth import AuthProviderError, get_auth_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
logger.info("[agent-proxy] starting up")

HISTORY_LIMIT = 10
GCE_PROJECT = "based-hardware"
VM_KEEPALIVE_INTERVAL = 120  # seconds — ping VM every 2 min during active WS


@app.get("/health")
def health():
    return {"status": "ok"}


def _get_user_context(uid: str) -> tuple:
    """Get agent VM info and data protection level from the active backend store."""
    return users_db.get_agent_vm(uid), users_db.get_data_protection_level(uid)


def _refresh_vm(uid: str) -> dict | None:
    """Re-read the VM info after a restart to get the latest IP/status."""
    return users_db.get_agent_vm(uid)


# --------------- GCE helpers ---------------


def _get_gce_access_token() -> str:
    """Get a GCE access token via Application Default Credentials."""
    creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


async def _check_gce_status(vm_name: str, zone: str) -> str:
    """Check the actual GCE instance status."""
    token = _get_gce_access_token()
    url = f"https://compute.googleapis.com/compute/v1/projects/{GCE_PROJECT}/zones/{zone}/instances/{vm_name}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return "UNKNOWN"
        return resp.json().get("status", "UNKNOWN")


async def _start_vm_and_wait(vm_name: str, zone: str) -> str:
    """Start a stopped/terminated GCE VM and return the new IP."""
    import time

    t0 = time.monotonic()
    token = _get_gce_access_token()
    start_url = (
        f"https://compute.googleapis.com/compute/v1/projects/{GCE_PROJECT}/zones/{zone}/instances/{vm_name}/start"
    )

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(start_url, headers={"Authorization": f"Bearer {token}"}, content=b"")
        if resp.status_code not in (200, 204):
            raise Exception(f"GCE start failed: {resp.status_code} {resp.text}")
        t_start = time.monotonic() - t0
        logger.info(f"[vm-start] {vm_name} start API call: {t_start:.1f}s")

        op_name = resp.json().get("name")
        if not op_name:
            raise Exception("Missing operation name in GCE start response")

        op_url = f"https://compute.googleapis.com/compute/v1/projects/{GCE_PROJECT}/zones/{zone}/operations/{op_name}"
        for i in range(24):
            await asyncio.sleep(5)
            token = _get_gce_access_token()
            status_resp = await client.get(op_url, headers={"Authorization": f"Bearer {token}"})
            status = status_resp.json()
            if status.get("status") == "DONE":
                if "error" in status:
                    raise Exception(f"GCE start operation failed: {status['error']}")
                t_op = time.monotonic() - t0
                logger.info(f"[vm-start] {vm_name} operation done after {i + 1} polls: {t_op:.1f}s")
                break

        # Poll for a valid external IP (may take a few seconds after operation completes)
        instance_url = (
            f"https://compute.googleapis.com/compute/v1/projects/{GCE_PROJECT}/zones/{zone}/instances/{vm_name}"
        )
        ip = None
        for attempt in range(6):
            token = _get_gce_access_token()
            inst_resp = await client.get(instance_url, headers={"Authorization": f"Bearer {token}"})
            instance = inst_resp.json()
            try:
                candidate = instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
                if candidate and candidate != "unknown":
                    ip = candidate
                    t_ip = time.monotonic() - t0
                    logger.info(f"[vm-start] {vm_name} got IP {ip} on attempt {attempt + 1}: {t_ip:.1f}s total")
                    break
            except (KeyError, IndexError):
                pass
            if attempt < 5:
                logger.info(f"[vm-start] {vm_name} no IP yet, retrying ({attempt + 1}/6)...")
                await asyncio.sleep(3)

        if not ip:
            t_fail = time.monotonic() - t0
            logger.error(f"[vm-start] {vm_name} failed to get IP after 6 attempts: {t_fail:.1f}s")
            ip = "unknown"

        return ip


def _update_agent_vm(uid: str, ip: str | None, status: str):
    """Update the user's agent VM metadata in the active backend store."""
    users_db.update_agent_vm(uid, ip, status)


async def _reset_vm(vm_name: str, zone: str):
    """Hard-reset a RUNNING VM whose agent process is unresponsive."""
    token = _get_gce_access_token()
    reset_url = (
        f"https://compute.googleapis.com/compute/v1/projects/{GCE_PROJECT}/zones/{zone}/instances/{vm_name}/reset"
    )
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(reset_url, headers={"Authorization": f"Bearer {token}"}, content=b"")
        if resp.status_code not in (200, 204):
            raise Exception(f"GCE reset failed: {resp.status_code} {resp.text}")
        logger.info(f"[agent-proxy] VM {vm_name} reset initiated")
        # Wait for the operation to complete
        op_name = resp.json().get("name")
        if op_name:
            op_url = (
                f"https://compute.googleapis.com/compute/v1/projects/{GCE_PROJECT}/zones/{zone}/operations/{op_name}"
            )
            for i in range(12):
                await asyncio.sleep(5)
                t = _get_gce_access_token()
                status_resp = await client.get(op_url, headers={"Authorization": f"Bearer {t}"})
                if status_resp.json().get("status") == "DONE":
                    break


async def _ensure_vm_running(uid: str, vm: dict, health_failed: bool = False) -> dict | None:
    """If VM is stopped, restart it and return updated VM info. Returns None on failure."""
    vm_name = vm.get("vmName")
    zone = vm.get("zone", "us-central1-a")
    fs_status = vm.get("status", "")

    if fs_status == "ready":
        # Verify it's actually running
        try:
            gce_status = await _check_gce_status(vm_name, zone)
        except Exception:
            return vm  # Can't check, assume it's fine

        if gce_status == "RUNNING":
            if health_failed:
                # VM is RUNNING but agent process is dead — hard reset
                logger.info(f"[agent-proxy] VM {vm_name} is RUNNING but unhealthy, resetting...")
                _update_agent_vm(uid, None, "provisioning")
                try:
                    await _reset_vm(vm_name, zone)
                    _update_agent_vm(uid, vm.get("ip"), "ready")
                    return _refresh_vm(uid)
                except Exception as e:
                    logger.error(f"[agent-proxy] Failed to reset VM {vm_name}: {e}")
                    _update_agent_vm(uid, None, "error")
                    return None
            return vm
        if gce_status not in ("TERMINATED", "STOPPED"):
            return vm  # STAGING, etc. — let it be

    # VM needs restart
    logger.info(f"[agent-proxy] VM {vm_name} needs restart, starting...")
    _update_agent_vm(uid, None, "provisioning")

    try:
        ip = await _start_vm_and_wait(vm_name, zone)
        _update_agent_vm(uid, ip, "ready")
        logger.info(f"[agent-proxy] VM {vm_name} restarted, ip={ip}")
        return _refresh_vm(uid)
    except Exception as e:
        logger.error(f"[agent-proxy] Failed to restart VM {vm_name}: {e}")
        _update_agent_vm(uid, None, "error")
        return None


async def _wait_for_vm_healthy(vm_ip: str, auth_token: str, timeout: float = 120) -> bool:
    """Poll the VM's /health endpoint until it responds OK."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=5) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(f"http://{vm_ip}:8080/health")
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(3)
    return False


# --------------- chat session helpers ---------------


def _get_or_create_chat_session(uid: str) -> dict:
    """Get or create the default (plugin_id=None) chat session."""
    session = chat_db.get_chat_session(uid, app_id=None)
    if session:
        return session

    session_data = {
        'id': str(uuid.uuid4()),
        'created_at': datetime.now(timezone.utc),
        'updated_at': datetime.now(timezone.utc),
        'plugin_id': None,
        'app_id': None,
        'message_ids': [],
        'file_ids': [],
        'message_count': 0,
        'starred': False,
        'preview': None,
        'title': 'Agent Chat',
    }
    return chat_db.add_chat_session(uid, session_data)


# --------------- message persistence ---------------


def _fetch_chat_history(uid: str, chat_session_id: str) -> list:
    """Fetch last N messages from the chat session, returned oldest-first."""
    messages = chat_db.get_messages(
        uid,
        limit=HISTORY_LIMIT,
        chat_session_id=chat_session_id,
        app_id=None,
    )
    history = [{'sender': item.get('sender', ''), 'text': item.get('text', '')} for item in messages]
    return list(reversed(history))


def _save_message(uid: str, text: str, sender: str, chat_session_id: str, data_protection_level: str):
    """Save a message with encryption metadata and chat session linking."""
    msg_id = str(uuid.uuid4())
    msg_data = {
        'id': msg_id,
        'text': text,
        'created_at': datetime.now(timezone.utc),
        'sender': sender,
        'plugin_id': None,
        'app_id': None,
        'type': 'text',
        'from_external_integration': False,
        'memories_id': [],
        'files_id': [],
        'chat_session_id': chat_session_id,
        'data_protection_level': data_protection_level,
    }
    chat_db.add_message(uid, msg_data)
    chat_db.add_message_to_chat_session(uid, chat_session_id, msg_id)


def _build_prompt_with_history(prompt: str, history: list) -> str:
    """Prepend conversation history to the current prompt."""
    if not history:
        return prompt

    lines = ["<conversation_history>"]
    for msg in history:
        role = "Human" if msg['sender'] == 'human' else "Assistant"
        lines.append(f"{role}: {msg['text']}")
    lines.append("</conversation_history>")
    lines.append("")
    lines.append(prompt)
    return "\n".join(lines)


@app.websocket("/v1/agent/ws")
async def agent_ws(websocket: WebSocket):
    # Validate backend access token from Authorization header
    auth_header = websocket.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.warning("[agent-proxy] WS rejected: missing Authorization header")
        await websocket.close(code=4001, reason="Missing Authorization header")
        return

    token = auth_header[7:].strip()
    try:
        claims = get_auth_provider().verify_access_token(token)
        uid = str(claims.get("uid") or claims["sub"])
    except (AuthProviderError, KeyError) as e:
        logger.warning(f"[agent-proxy] WS rejected: invalid token: {e}")
        await websocket.close(code=4001, reason="Invalid token")
        return

    # Look up the user's agent VM and data protection level
    vm, data_protection_level = _get_user_context(uid)
    if not vm:
        logger.warning(f"[agent-proxy] WS rejected: uid={uid} no VM")
        await websocket.close(code=4002, reason="No agent VM available")
        return

    # Accept WebSocket first so we can send status messages during VM startup
    await websocket.accept()

    vm_ip = vm.get("ip")
    vm_token = vm.get("authToken")

    # Fast path: if the active backend says ready with an IP, try connecting directly (skip GCE check).
    # Only fall back to GCE check + restart if the VM isn't reachable.
    if vm.get("status") == "ready" and vm_ip:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"http://{vm_ip}:8080/health")
                if resp.status_code != 200:
                    raise Exception(f"health returned {resp.status_code}")
        except Exception:
            # VM not reachable — check GCE and restart/reset if needed
            logger.info(f"[agent-proxy] uid={uid} VM {vm_ip} not reachable, checking GCE...")
            await websocket.send_text(json.dumps({"type": "status", "message": "Starting your agent VM..."}))
            vm = await _ensure_vm_running(uid, vm, health_failed=True)
            if not vm or vm.get("status") != "ready" or not vm.get("ip"):
                await websocket.send_text(json.dumps({"type": "error", "message": "Failed to start agent VM"}))
                await websocket.close(code=4002, reason="VM startup failed")
                return
            vm_ip = vm["ip"]
            vm_token = vm["authToken"]
            # Wait for VM to be healthy after restart
            healthy = await _wait_for_vm_healthy(vm_ip, vm_token)
            if not healthy:
                await websocket.send_text(json.dumps({"type": "error", "message": "Agent VM is not responding"}))
                await websocket.close(code=4003, reason="VM not healthy")
                return
    else:
        # No IP or not ready — must restart
        await websocket.send_text(json.dumps({"type": "status", "message": "Starting your agent VM..."}))
        vm = await _ensure_vm_running(uid, vm)
        if not vm or vm.get("status") != "ready" or not vm.get("ip"):
            await websocket.send_text(json.dumps({"type": "error", "message": "Failed to start agent VM"}))
            await websocket.close(code=4002, reason="VM startup failed")
            return
        vm_ip = vm["ip"]
        vm_token = vm["authToken"]
        healthy = await _wait_for_vm_healthy(vm_ip, vm_token)
        if not healthy:
            await websocket.send_text(json.dumps({"type": "error", "message": "Agent VM is not responding"}))
            await websocket.close(code=4003, reason="VM not healthy")
            return

    vm_uri = f"ws://{vm_ip}:8080/ws?token={vm_token}"

    # Get or create the default chat session so messages are linked properly
    chat_session = await asyncio.to_thread(_get_or_create_chat_session, uid)
    chat_session_id = chat_session['id']

    logger.info(f"[agent-proxy] uid={uid} connecting to vm={vm_ip}")

    try:
        async with websockets.connect(vm_uri, ping_interval=600, ping_timeout=600) as vm_ws:
            logger.info(f"[agent-proxy] uid={uid} connected")

            # Send the user access token to VM so it can fetch backend tools (calendar, gmail, etc.)
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(f"http://{vm_ip}:8080/auth?token={vm_token}", json={"accessToken": token})
                    logger.info(f"[agent-proxy] uid={uid} sent user token to VM")
            except Exception as e:
                logger.warning(f"[agent-proxy] uid={uid} failed to send user token: {e}")

            first_query_sent = False

            async def _save_ai_response(uid, text, session_id, protection_level):
                """Fire-and-forget AI response save — never blocks event forwarding."""
                try:
                    await asyncio.to_thread(_save_message, uid, text, 'ai', session_id, protection_level)
                    logger.info(f"[agent-proxy] uid={uid} saved AI response ({len(text)} chars)")
                except Exception as e:
                    logger.warning(f"[agent-proxy] uid={uid} failed to save AI response: {e}")

            async def phone_to_vm():
                nonlocal first_query_sent
                try:
                    async for msg in websocket.iter_text():
                        try:
                            data = json.loads(msg)
                            if data.get('type') == 'query':
                                prompt = data.get('prompt', '')
                                if not first_query_sent:
                                    # First query: fetch history and inject into prompt
                                    history = await asyncio.to_thread(_fetch_chat_history, uid, chat_session_id)
                                    data['prompt'] = _build_prompt_with_history(prompt, history)
                                    msg = json.dumps(data)
                                    first_query_sent = True
                                    logger.info(
                                        f"[agent-proxy] uid={uid} first query with {len(history)} history messages"
                                    )
                                else:
                                    # Subsequent queries: Claude session already has context
                                    logger.info(f"[agent-proxy] uid={uid} follow-up query (session has context)")
                                # Save user message in background — no need to block VM forwarding
                                asyncio.create_task(
                                    asyncio.to_thread(
                                        _save_message,
                                        uid,
                                        prompt,
                                        'human',
                                        chat_session_id,
                                        data_protection_level,
                                    )
                                )
                        except (json.JSONDecodeError, Exception) as e:
                            logger.warning(f"[agent-proxy] failed to process message: {e}")
                        await vm_ws.send(msg)
                except (WebSocketDisconnect, Exception):
                    pass

            async def vm_to_phone():
                response_text = ''
                try:
                    async for msg in vm_ws:
                        text = msg if isinstance(msg, str) else msg.decode()
                        await websocket.send_text(text)
                        # Collect response for saving
                        try:
                            event = json.loads(text)
                            evt_type = event.get('type')
                            evt_text = event.get('text', '') or event.get('content', '') or ''
                            if evt_type == 'text_delta':
                                response_text += evt_text
                            elif evt_type == 'result':
                                # Use result text as fallback if no deltas were collected
                                if evt_text and not response_text:
                                    response_text = evt_text
                                # Save per-query so each message gets its own history entry
                                if response_text.strip():
                                    _text = response_text.strip()
                                    asyncio.create_task(
                                        _save_ai_response(uid, _text, chat_session_id, data_protection_level)
                                    )
                                response_text = ''
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    pass
                finally:
                    # Save any unsaved partial response (connection dropped mid-query)
                    if response_text.strip():
                        # Use await here since we're in finally — connection is closing anyway
                        await _save_ai_response(uid, response_text.strip(), chat_session_id, data_protection_level)

            async def keepalive_pinger():
                """Periodically ping the VM to prevent idle auto-stop during active WS."""
                async with httpx.AsyncClient(timeout=5) as client:
                    while True:
                        await asyncio.sleep(VM_KEEPALIVE_INTERVAL)
                        try:
                            await client.post(f"http://{vm_ip}:8080/ping?token={vm_token}")
                        except Exception:
                            pass

            t1 = asyncio.create_task(phone_to_vm())
            t2 = asyncio.create_task(vm_to_phone())
            t3 = asyncio.create_task(keepalive_pinger())
            _, pending = await asyncio.wait([t1, t2, t3], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    except Exception as e:
        logger.error(f"[agent-proxy] uid={uid} vm_connect failed: {e}")
    finally:
        try:
            await websocket.close(code=1000, reason="Session ended")
        except Exception:
            pass
        logger.info(f"[agent-proxy] uid={uid} disconnected")
