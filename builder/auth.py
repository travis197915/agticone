"""
JWT bridge — trust the access tokens issued by the Node `claims-corebackend`.

The Node service signs HS256 tokens whose payload is
    {"sub": "<user-id>", "email": "...", "role": "ADMIN" | "AUDITOR",
     "permissions": ["claims:review:approve", ...]}

We don't touch the user database here — the Node service is the single
source of truth for accounts *and* for the Role/Permission model. Django
verifies the signature with the shared `JWT_SECRET` and reads `permissions`
straight off the token — no callback to Node, no DB read. This is also the
*only* enforcement for requests the audit-review-dashboard sends directly to
Django, bypassing Node's proxy entirely (see `HasPermission` below).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import jwt
from rest_framework import authentication, exceptions
from rest_framework.permissions import BasePermission

WILDCARD_PERMISSION = "*"


@dataclass
class CorebackendUser:
    """A user authenticated by the Node corebackend."""

    id: str
    email: str
    role: str  # role name, e.g. "ADMIN" | "AUDITOR" — informational only
    permissions: list[str] = field(default_factory=list)

    @property
    def is_authenticated(self) -> bool:  # required by DRF
        return True

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    def has_permission(self, key: str) -> bool:
        return WILDCARD_PERMISSION in self.permissions or key in self.permissions

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
            permissions=list(payload.get("permissions", [])),
        )
        return (user, payload)

    def authenticate_header(self, _request) -> str:
        return self.keyword


def HasPermission(key: str):  # noqa: N802 — factory named like the class it returns
    """
    DRF permission class factory — `permission_classes = [HasPermission("claims:read")]`.

    Required in Phase 1, not optional: several endpoints (claim/run review
    actions, claim reads) are called directly by the audit-review-dashboard,
    bypassing Node's `requirePermission` proxy-layer check entirely. For
    those requests this is the *only* enforcement that will ever run, so the
    permission key here must match the key Node's middleware uses for the
    same logical action.
    """

    class _HasPermission(BasePermission):
        def has_permission(self, request, _view) -> bool:
            user = request.user
            return isinstance(user, CorebackendUser) and user.has_permission(key)

    _HasPermission.__name__ = f"HasPermission[{key}]"
    return _HasPermission
