"""Secret-free runtime route facts for acceptance-bound provider calls."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import RuntimeGuardError, RuntimeRouteIdentity

_ENV_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_STORE_REF = re.compile(r"^store:[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class RuntimeRouteBinding:
    """The route selected by a consumer and its resolved, non-secret facts."""

    route_id: str
    identity: RuntimeRouteIdentity


def route_binding_from_env(
    role: str,
    *,
    route_id: str | None = None,
    required: bool = False,
    environ: Mapping[str, str] | None = None,
) -> RuntimeRouteBinding | None:
    """Read the selected route facts without reading ambient provider secrets."""

    values = os.environ if environ is None else environ
    raw_value = values.get("SIDERA_ACCEPTANCE_ROUTE_FACTS")
    if not raw_value:
        if required:
            raise RuntimeGuardError("route_identity_missing")
        return None
    try:
        raw = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeGuardError("route_identity_invalid") from exc
    if not isinstance(raw, Mapping):
        raise RuntimeGuardError("route_identity_invalid")
    selected = raw.get(role)
    if not isinstance(selected, Mapping):
        raise RuntimeGuardError("route_identity_missing")
    selected_route_id = selected.get("route_id")
    if not isinstance(selected_route_id, str) or not selected_route_id.strip():
        raise RuntimeGuardError("route_identity_invalid")
    selected_route_id = selected_route_id.strip()
    if route_id is not None and route_id != selected_route_id:
        raise RuntimeGuardError("route_identity_conflict")
    identity = RuntimeRouteIdentity.from_mapping(selected)
    return RuntimeRouteBinding(route_id=selected_route_id, identity=identity)


def _validate_credential_ref(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeGuardError("route_credential_ref_invalid")
    value = value.strip()
    if value == "none" or _ENV_REF.fullmatch(value) or _STORE_REF.fullmatch(value):
        return value
    if value.startswith("file:") and Path(value.removeprefix("file:")).is_absolute():
        return value
    raise RuntimeGuardError("route_credential_ref_invalid")


def _validate_endpoint_host(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeGuardError("route_endpoint_invalid")
    value = value.strip().lower()
    if not value or any(character.isspace() for character in value):
        raise RuntimeGuardError("route_endpoint_invalid")
    if "://" in value or any(character in value for character in "/?#@"):
        raise RuntimeGuardError("route_endpoint_invalid")
    try:
        parsed = urlsplit(f"//{value}")
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeGuardError("route_endpoint_invalid") from exc
    if parsed.hostname is None or parsed.username or parsed.password:
        raise RuntimeGuardError("route_endpoint_invalid")
    return value


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeGuardError(code)
    return value.strip()


__all__ = ["RuntimeRouteBinding", "route_binding_from_env"]
