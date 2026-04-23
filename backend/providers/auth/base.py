from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class AuthenticatedUser:
    uid: str
    email: Optional[str] = None
    email_verified: bool = False
    phone_number: Optional[str] = None
    display_name: Optional[str] = None
    photo_url: Optional[str] = None
    disabled: bool = False
    claims: Mapping[str, Any] = field(default_factory=dict)


class AuthProviderError(Exception):
    """Raised when the active auth provider rejects or cannot resolve a user."""


class AuthProvider(ABC):
    @abstractmethod
    def verify_access_token(self, token: str) -> Mapping[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_user(self, uid: str) -> Optional[AuthenticatedUser]:
        raise NotImplementedError

    @abstractmethod
    def delete_user(self, uid: str) -> None:
        raise NotImplementedError
