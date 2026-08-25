from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterable
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
        authorize_capability(request, capability)

    return dependency


def authorize_capability(request: Request, capability: Capability) -> None:
    settings: KnowledgeSettings = request.app.state.settings
    expected = _token_for_capability(settings, capability)
    if expected is None:
        raise HTTPException(status_code=503, detail="capability_unavailable")
    authorization = request.headers.get("Authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        raise HTTPException(status_code=403, detail="permission_denied")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="permission_denied")


def authorize_any_capability(
    request: Request, capabilities: Iterable[Capability]
) -> None:
    settings: KnowledgeSettings = request.app.state.settings
    configured = tuple(
        (capability, _token_for_capability(settings, capability))
        for capability in capabilities
    )
    if not any(token is not None for _, token in configured):
        raise HTTPException(status_code=503, detail="capability_unavailable")
    authorization = request.headers.get("Authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        raise HTTPException(status_code=403, detail="permission_denied")
    if not any(
        token is not None and secrets.compare_digest(supplied, token)
        for _, token in configured
    ):
        raise HTTPException(status_code=403, detail="permission_denied")


def authorize_actor(request: Request, actor_type: str) -> None:
    if actor_type not in {"local_owner", "main_agent", "control_plane"}:
        raise HTTPException(status_code=403, detail="permission_denied")
    authorize_capability(request, actor_type)  # type: ignore[arg-type]
