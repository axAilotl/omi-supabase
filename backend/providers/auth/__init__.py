from functools import lru_cache

from providers.auth.base import AuthProvider, AuthProviderError, AuthenticatedUser
from providers.auth.supabase import SupabaseAuthProvider as _SelectedAuthProvider


@lru_cache(maxsize=1)
def get_auth_provider() -> AuthProvider:
    return _SelectedAuthProvider()


__all__ = [
    "AuthenticatedUser",
    "AuthProvider",
    "AuthProviderError",
    "get_auth_provider",
]
