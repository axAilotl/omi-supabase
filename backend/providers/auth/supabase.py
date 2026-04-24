import os
from functools import lru_cache
from typing import Mapping, Optional

import httpx
import jwt

from providers.auth.base import AuthProvider, AuthProviderError, AuthenticatedUser

_JWT_SECRET_ENV_KEYS = ("SUPABASE_JWT_SECRET", "GOTRUE_JWT_SECRET", "JWT_SECRET")
_JWKS_URL_ENV_KEYS = ("SUPABASE_JWKS_URL", "GOTRUE_JWKS_URL")
_SERVICE_ROLE_ENV_KEYS = ("SUPABASE_SERVICE_ROLE_KEY", "SERVICE_ROLE_KEY", "SECRET_KEY")


def _resolve_jwt_secret() -> Optional[str]:
    for env_key in _JWT_SECRET_ENV_KEYS:
        value = os.getenv(env_key)
        if value:
            return value
    return None


def _resolve_auth_url() -> Optional[str]:
    if auth_url := os.getenv("SUPABASE_AUTH_URL"):
        return auth_url.rstrip("/")
    if supabase_url := os.getenv("SUPABASE_URL"):
        return f"{supabase_url.rstrip('/')}/auth/v1"
    return None


def _resolve_jwks_url() -> Optional[str]:
    for env_key in _JWKS_URL_ENV_KEYS:
        if value := os.getenv(env_key):
            return value.rstrip("/")
    if auth_url := _resolve_auth_url():
        return f"{auth_url}/.well-known/jwks.json"
    return None


def _resolve_service_role_key() -> Optional[str]:
    for env_key in _SERVICE_ROLE_ENV_KEYS:
        value = os.getenv(env_key)
        if value:
            return value
    return None


@lru_cache(maxsize=4)
def _get_jwk_client(jwks_url: str):
    return jwt.PyJWKClient(jwks_url)


class SupabaseAuthProvider(AuthProvider):
    def __init__(self):
        self._jwt_secret = _resolve_jwt_secret()
        self._jwt_audience = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
        self._jwt_issuer = os.getenv("SUPABASE_JWT_ISSUER") or os.getenv("GOTRUE_JWT_ISSUER") or _resolve_auth_url()
        self._auth_url = _resolve_auth_url()
        self._jwks_url = _resolve_jwks_url()
        self._service_role_key = _resolve_service_role_key()

    def verify_access_token(self, token: str) -> Mapping[str, object]:
        try:
            unverified_header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise AuthProviderError(str(exc)) from exc

        algorithm = unverified_header.get("alg") or "HS256"

        if algorithm.startswith("HS"):
            if not self._jwt_secret:
                raise AuthProviderError(
                    "Supabase auth is enabled but no JWT secret is configured. "
                    "Set SUPABASE_JWT_SECRET or GOTRUE_JWT_SECRET."
                )
            key = self._jwt_secret
        else:
            if not self._jwks_url:
                raise AuthProviderError(
                    "Supabase auth uses asymmetric JWTs but no JWKS URL is configured. "
                    "Set SUPABASE_JWKS_URL or SUPABASE_AUTH_URL/SUPABASE_URL."
                )
            try:
                key = _get_jwk_client(self._jwks_url).get_signing_key_from_jwt(token).key
            except Exception as exc:
                raise AuthProviderError(str(exc)) from exc

        decode_kwargs = {
            "algorithms": [algorithm],
            "audience": self._jwt_audience,
            "options": {"require": ["sub"]},
        }
        if self._jwt_issuer:
            decode_kwargs["issuer"] = self._jwt_issuer

        try:
            payload = jwt.decode(token, key, **decode_kwargs)
        except Exception as exc:
            raise AuthProviderError(str(exc)) from exc

        if not payload.get("sub"):
            raise AuthProviderError("Supabase token is missing the required 'sub' claim")
        payload.setdefault("uid", payload["sub"])
        return payload

    def get_user(self, uid: str) -> Optional[AuthenticatedUser]:
        if not uid:
            return None
        return AuthenticatedUser(uid=uid)

    def delete_user(self, uid: str) -> None:
        if not uid:
            raise AuthProviderError("Supabase user deletion requires a uid")
        if not self._service_role_key:
            raise AuthProviderError(
                "Supabase user deletion requires SUPABASE_SERVICE_ROLE_KEY, SERVICE_ROLE_KEY, or SECRET_KEY"
            )
        if not self._auth_url:
            raise AuthProviderError("Supabase user deletion requires SUPABASE_AUTH_URL or SUPABASE_URL")

        response = httpx.delete(
            f"{self._auth_url}/admin/users/{uid}",
            headers={
                "Authorization": f"Bearer {self._service_role_key}",
                "apikey": self._service_role_key,
            },
            timeout=30.0,
        )
        if response.status_code in (200, 204, 404):
            return
        raise AuthProviderError(
            f"Supabase user deletion failed with status={response.status_code}: {response.text[:200]}"
        )
