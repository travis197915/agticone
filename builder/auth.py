"""
JWT bridge — trust the access tokens issued by the Node `claims-corebackend`.

The Node service signs HS256 tokens whose payload is
    {"sub": "<user-id>", "email": "...", "role": "ADMIN" | "MEMBER", ...}

We don't touch the user database here — the Node service is the single
source of truth for accounts.  Django just verifies the signature with the
shared `JWT_SECRET` and exposes the decoded claims on `request.auth`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from rest_framework import authentication, exceptions


@dataclass
class CorebackendUser:
    """A user authenticated by the Node corebackend."""

    id: str
    email: str
    role: str  # "ADMIN" | "MEMBER"

    @property
    def is_authenticated(self) -> bool:  # required by DRF
        return True

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.email} ({self.role})"


def _secret() -> str:
    value = os.environ.get("JWT_SECRET")
    if not value or len(value) < 16:
        raise RuntimeError(
            "JWT_SECRET env var missing or too short (need ≥ 16 chars). "
            "Must match the secret used by claims-corebackend."
        )
    return value


class CorebackendJWTAuthentication(authentication.BaseAuthentication):
    """
    DRF authentication backend that accepts `Authorization: Bearer <jwt>`
    issued by the Node corebackend.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith(self.keyword + " "):
            return None  # no header → fall through to other auth backends
        token = header[len(self.keyword) + 1:].strip()
        if not token:
            return None
        try:
            payload = jwt.decode(token, _secret(), algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Token expired")
        except jwt.InvalidTokenError as exc:
            raise exceptions.AuthenticationFailed(f"Invalid token: {exc}")

        sub = payload.get("sub")
        if not sub:
            raise exceptions.AuthenticationFailed("Token missing 'sub' claim")

        user = CorebackendUser(
            id=str(sub),
            email=str(payload.get("email", "")),
            role=str(payload.get("role", "MEMBER")),
        )
        return (user, payload)

    def authenticate_header(self, _request) -> str:
        return self.keyword
