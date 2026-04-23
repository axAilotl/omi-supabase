import json
import logging
import os
import pathlib
import time
import uuid
from typing import Optional
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from utils.http_client import get_auth_client
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/auth",
    tags=["authentication"],
)

templates_path = pathlib.Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

_AUTH_SESSION_TTL_SECONDS = 300
_AUTH_CODE_TTL_SECONDS = 300
_auth_sessions: dict[str, dict[str, object]] = {}
_auth_codes: dict[str, dict[str, object]] = {}


def _store_with_ttl(store: dict[str, dict[str, object]], key: str, data: object, ttl_seconds: int) -> None:
    store[key] = {"data": data, "expires": time.time() + ttl_seconds}


def _get_with_ttl(store: dict[str, dict[str, object]], key: str) -> Optional[object]:
    record = store.get(key)
    if not record:
        return None
    expires_at = record.get("expires", 0)
    if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
        store.pop(key, None)
        return None
    return record.get("data")


def _delete_record(store: dict[str, dict[str, object]], key: str) -> None:
    store.pop(key, None)


def _resolve_base_api_url() -> str:
    base_api_url = os.getenv("BASE_API_URL")
    if not base_api_url:
        raise HTTPException(status_code=500, detail="BASE_API_URL not configured")
    return base_api_url.rstrip("/")


def _resolve_supabase_auth_url() -> str:
    auth_url = os.getenv("SUPABASE_AUTH_URL")
    if auth_url:
        return auth_url.rstrip("/")
    supabase_url = os.getenv("SUPABASE_URL")
    if supabase_url:
        return f"{supabase_url.rstrip('/')}/auth/v1"
    raise HTTPException(status_code=500, detail="SUPABASE_AUTH_URL or SUPABASE_URL not configured")


def _resolve_supabase_anon_key() -> str:
    for env_key in ("SUPABASE_ANON_KEY", "NEXT_PUBLIC_SUPABASE_ANON_KEY"):
        value = os.getenv(env_key)
        if value:
            return value
    raise HTTPException(status_code=500, detail="SUPABASE_ANON_KEY not configured")


@router.get("/authorize")
async def auth_authorize(
    request: Request,
    provider: str,
    redirect_uri: str,
    state: Optional[str] = None,
):
    if provider not in ["google", "apple"]:
        raise HTTPException(status_code=400, detail="Unsupported provider")

    session_id = str(uuid.uuid4())
    _store_with_ttl(
        _auth_sessions,
        session_id,
        {
            "provider": provider,
            "redirect_uri": redirect_uri,
            "state": state,
        },
        _AUTH_SESSION_TTL_SECONDS,
    )

    if provider == "google":
        return await _google_auth_redirect(session_id)
    return await _apple_auth_redirect(session_id)


@router.get("/callback/google")
async def auth_callback_google(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if error:
        raise HTTPException(status_code=400, detail=f"Auth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    session_data = _get_with_ttl(_auth_sessions, state)
    if not isinstance(session_data, dict):
        raise HTTPException(status_code=400, detail="Invalid auth session")

    oauth_credentials = await _exchange_provider_code_for_oauth_credentials("google", code, session_data)

    auth_code = str(uuid.uuid4())
    _store_with_ttl(_auth_codes, auth_code, oauth_credentials, _AUTH_CODE_TTL_SECONDS)

    return templates.TemplateResponse(
        "auth_callback.html",
        {
            "request": request,
            "code": auth_code,
            "state": session_data.get("state") or "",
            "redirect_uri": session_data.get("redirect_uri", "omi://auth/callback"),
        },
    )


@router.post("/callback/apple")
async def auth_callback_apple_post(
    request: Request,
    code: str = Form(...),
    state: str = Form(...),
    error: Optional[str] = Form(None),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Auth error: {error}")

    session_data = _get_with_ttl(_auth_sessions, state)
    if not isinstance(session_data, dict):
        raise HTTPException(status_code=400, detail="Invalid auth session")

    oauth_credentials = await _exchange_provider_code_for_oauth_credentials("apple", code, session_data)

    auth_code = str(uuid.uuid4())
    _store_with_ttl(_auth_codes, auth_code, oauth_credentials, _AUTH_CODE_TTL_SECONDS)

    return templates.TemplateResponse(
        "auth_callback.html",
        {
            "request": request,
            "code": auth_code,
            "state": session_data.get("state") or "",
            "redirect_uri": session_data.get("redirect_uri", "omi://auth/callback"),
        },
    )


@router.post("/token")
async def auth_token(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
):
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant type")

    oauth_credentials_json = _get_with_ttl(_auth_codes, code)
    if not isinstance(oauth_credentials_json, str):
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    _delete_record(_auth_codes, code)

    try:
        oauth_credentials = json.loads(oauth_credentials_json)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth credentials") from exc

    expected_redirect_uri = oauth_credentials.get("redirect_uri")
    if expected_redirect_uri and expected_redirect_uri != redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri does not match the original authorization request")

    session = await _exchange_provider_tokens_for_supabase_session(oauth_credentials)
    session["provider"] = oauth_credentials.get("provider")
    session["provider_id"] = oauth_credentials.get("provider_id")
    session["id_token"] = oauth_credentials.get("id_token")
    session["provider_access_token"] = oauth_credentials.get("access_token")
    return session


@router.post("/refresh")
async def refresh_session(refresh_token: str = Form(...)):
    auth_url = _resolve_supabase_auth_url()
    anon_key = _resolve_supabase_anon_key()
    client = get_auth_client()
    response = await client.post(
        f"{auth_url}/token?grant_type=refresh_token",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
        },
        json={
            "refresh_token": refresh_token,
        },
    )
    if response.status_code != 200:
        logger.error("Supabase token refresh failed: %s", sanitize(response.text))
        raise HTTPException(status_code=400, detail="Failed to refresh Supabase session")
    return response.json()


async def _google_auth_redirect(session_id: str):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID not configured")

    callback_url = f"{_resolve_base_api_url()}/v1/auth/callback/google"
    google_auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={quote(client_id)}&"
        f"redirect_uri={quote(callback_url)}&"
        f"response_type=code&"
        f"scope={quote('openid email profile')}&"
        f"state={quote(session_id)}"
    )
    return RedirectResponse(url=google_auth_url)


async def _apple_auth_redirect(session_id: str):
    client_id = os.getenv("APPLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="APPLE_CLIENT_ID not configured")

    callback_url = f"{_resolve_base_api_url()}/v1/auth/callback/apple"
    apple_auth_url = (
        f"https://appleid.apple.com/auth/authorize?"
        f"client_id={client_id}&"
        f"redirect_uri={callback_url}&"
        f"response_type=code&"
        f"scope=name email&"
        f"response_mode=form_post&"
        f"state={session_id}"
    )
    return RedirectResponse(url=apple_auth_url)


async def _exchange_provider_code_for_oauth_credentials(provider: str, code: str, session_data: dict) -> str:
    if provider == "google":
        return await _exchange_google_code_for_oauth_credentials(code, session_data)
    if provider == "apple":
        return await _exchange_apple_code_for_oauth_credentials(code, session_data)
    raise HTTPException(status_code=400, detail="Unsupported provider")


async def _exchange_google_code_for_oauth_credentials(code: str, session_data: dict) -> str:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not all([client_id, client_secret]):
        raise HTTPException(status_code=500, detail="Google OAuth not properly configured")

    callback_url = f"{_resolve_base_api_url()}/v1/auth/callback/google"

    client = get_auth_client()
    token_response = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": callback_url,
            "grant_type": "authorization_code",
        },
    )
    if token_response.status_code != 200:
        logger.error("Google token exchange failed: %s", sanitize(token_response.text))
        raise HTTPException(status_code=400, detail="Failed to exchange Google code")

    token_json = token_response.json()
    id_token = token_json.get("id_token")
    access_token = token_json.get("access_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="Invalid Google token response")

    return json.dumps(
        {
            "provider": "google",
            "id_token": id_token,
            "access_token": access_token,
            "provider_id": "google",
            "redirect_uri": session_data.get("redirect_uri"),
        }
    )


async def _exchange_apple_code_for_oauth_credentials(code: str, session_data: dict) -> str:
    client_id = os.getenv("APPLE_CLIENT_ID")
    team_id = os.getenv("APPLE_TEAM_ID")
    key_id = os.getenv("APPLE_KEY_ID")
    private_key_content = os.getenv("APPLE_PRIVATE_KEY")
    if not all([client_id, team_id, key_id, private_key_content]):
        raise HTTPException(status_code=500, detail="Apple authentication not properly configured")

    client_secret = _generate_apple_client_secret(client_id, team_id, key_id, private_key_content)
    callback_url = f"{_resolve_base_api_url()}/v1/auth/callback/apple"

    client = get_auth_client()
    token_response = await client.post(
        "https://appleid.apple.com/auth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": callback_url,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if token_response.status_code != 200:
        logger.error("Apple token exchange failed: %s", sanitize(token_response.text))
        raise HTTPException(status_code=400, detail="Failed to exchange Apple authorization code")

    token_json = token_response.json()
    id_token = token_json.get("id_token")
    access_token = token_json.get("access_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="No ID token received from Apple")

    return json.dumps(
        {
            "provider": "apple",
            "id_token": id_token,
            "access_token": access_token,
            "provider_id": "apple",
            "redirect_uri": session_data.get("redirect_uri"),
        }
    )


async def _exchange_provider_tokens_for_supabase_session(oauth_credentials: dict) -> dict:
    auth_url = _resolve_supabase_auth_url()
    anon_key = _resolve_supabase_anon_key()
    provider = oauth_credentials.get("provider")
    id_token = oauth_credentials.get("id_token")
    access_token = oauth_credentials.get("access_token")
    nonce = oauth_credentials.get("nonce")

    if provider not in ("google", "apple"):
        raise HTTPException(status_code=400, detail="Unsupported provider")
    if not id_token:
        raise HTTPException(status_code=400, detail="Missing provider id_token")

    payload = {
        "provider": provider,
        "id_token": id_token,
    }
    if access_token:
        payload["access_token"] = access_token
    if nonce:
        payload["nonce"] = nonce

    client = get_auth_client()
    response = await client.post(
        f"{auth_url}/token?grant_type=id_token",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
        },
        json=payload,
    )

    if response.status_code != 200:
        logger.error("Supabase sign in with id_token failed: %s", sanitize(response.text))
        raise HTTPException(status_code=400, detail="Failed to create Supabase session")

    return response.json()


def _generate_apple_client_secret(client_id: str, team_id: str, key_id: str, private_key_content: str) -> str:
    private_key = serialization.load_pem_private_key(
        private_key_content.encode("utf-8"),
        password=None,
    )

    now = int(time.time())
    payload = {
        "iss": team_id,
        "iat": now,
        "exp": now + 3600,
        "aud": "https://appleid.apple.com",
        "sub": client_id,
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})
