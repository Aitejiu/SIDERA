from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import HTTPException, Request

from sidera_knowledge.config import KnowledgeSettings

Capability = Literal["local_owner", "main_agent", "control_plane"]


def _token_for_capability(
    settings: KnowledgeSettings, capability: Capability
) -> str | None:
    secret = getattr(settings, f"{capability}_token")
    return secret.get_secret_value() if secret is not None else None


def require_capability(
    capability: Capability,
) -> Callable[[Request], Awaitable[None]]:
    async def dependency(request: Request) -> None:
        settings: KnowledgeSettings = request.app.state.settings
        expected = _token_for_capability(settings, capability)
        if expected is None:
            raise HTTPException(status_code=503, detail="capability_unavailable")
        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            raise HTTPException(status_code=401, detail="authentication_required")
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="capability_forbidden")

    return dependency
