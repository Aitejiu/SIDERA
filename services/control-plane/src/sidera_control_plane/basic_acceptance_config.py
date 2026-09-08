"""Read-only effective profile inspection for the R6 acceptance manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from sidera_control_plane.basic_acceptance_manifest import (
    AgentProfile,
    BasicRunManifest,
)

ProfileStatus = Literal["verified", "mismatch", "unavailable"]
_ENV_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_INHERITED_ENV = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


class ResolvedAgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    runtime: str
    provider: str
    model_id: str
    configured_model_id: str
    endpoint_host: str
    config_version: int
    credential_source: str
    fallbacks: tuple[str, ...]
    status: ProfileStatus
    code: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfigInspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configured_profiles: tuple[AgentProfile, ...]
    resolved_profiles: tuple[ResolvedAgentProfile, ...]
    runtime_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_sessions: int = 0

    def profile(self, role: str) -> ResolvedAgentProfile:
        for value in self.resolved_profiles:
            if value.role == role:
                return value
        raise KeyError(role)


def inspect_profiles(
    manifest: BasicRunManifest,
    *,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ConfigInspectionReport:
    values = os.environ if environ is None else environ
    selected_overrides = overrides or {}
    resolved = tuple(
        _resolve_profile(
            profile,
            selected_overrides.get(profile.role, {}),
            values,
        )
        for profile in manifest.profiles
    )
    runtime_fingerprint = _fingerprint(
        [profile.fingerprint for profile in resolved]
        + [manifest.control_plane_url, manifest.knowledge_url]
    )
    return ConfigInspectionReport(
        configured_profiles=manifest.profiles,
        resolved_profiles=resolved,
        runtime_fingerprint=runtime_fingerprint,
    )


def prepare_process_environment(
    manifest: BasicRunManifest,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal child environment without inheriting ambient provider keys."""

    values = os.environ if environ is None else environ
    report = inspect_profiles(manifest, environ=values)
    unavailable = [
        f"{profile.role}:{profile.code}"
        for profile in report.resolved_profiles
        if profile.status == "unavailable"
    ]
    if unavailable:
        raise ValueError(f"selected acceptance profiles are unavailable: {unavailable}")
    selected_names = {
        reference.removeprefix("env:")
        for profile in manifest.profiles
        for reference in (profile.credential_ref, profile.config_ref)
        if _ENV_REF.fullmatch(reference)
    }
    selected_names.update(
        reference.removeprefix("env:")
        for reference in (manifest.database_ref, manifest.knowledge_database_ref)
        if _ENV_REF.fullmatch(reference)
    )
    result = {
        name: value
        for name, value in values.items()
        if name in _INHERITED_ENV or name in selected_names
    }
    result["SIDERA_ACCEPTANCE_RUNTIME_FINGERPRINT"] = report.runtime_fingerprint
    return result


def write_process_environment(path: Path, environment: Mapping[str, str]) -> None:
    """Persist a deliberately requested local environment file with mode 0600."""

    if not path.is_absolute():
        raise ValueError("process environment path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(environment), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _resolve_profile(
    profile: AgentProfile,
    override: Mapping[str, object],
    environ: Mapping[str, str],
) -> ResolvedAgentProfile:
    effective: dict[str, object] = {
        "runtime": profile.runtime,
        "provider": profile.provider,
        "model_id": profile.model_id,
        "endpoint_host": profile.endpoint_host,
        "credential_ref": profile.credential_ref,
        "config_version": profile.config_version,
        "fallbacks": profile.fallbacks,
    }
    status: ProfileStatus = "verified"
    code = "profile_verified"
    configured_values = dict(effective)
    config_error = _apply_config_reference(profile, effective, environ)
    if config_error is not None:
        status = "unavailable"
        code = config_error
    mismatch = any(
        effective[key] != configured_values[key]
        for key in configured_values
        if key in effective
    )
    for key, value in override.items():
        if key not in effective:
            status = "unavailable"
            code = "unsupported_profile_override"
            continue
        if effective[key] != value:
            mismatch = True
            effective[key] = value
    provider = _text(effective["provider"])
    model_id = _text(effective["model_id"])
    endpoint_host = _text(effective["endpoint_host"])
    credential_ref = _text(effective["credential_ref"])
    fallbacks = _fallback_values(effective["fallbacks"])
    if not provider or not model_id or not endpoint_host:
        status = "unavailable"
        code = "selected_runtime_incomplete"
    elif provider.casefold() == "deepseek" or "deepseek" in endpoint_host.casefold():
        status = "unavailable"
        code = "direct_deepseek_route_disallowed"
    elif not _credential_available(credential_ref, environ):
        status = "unavailable"
        code = "selected_credential_unavailable"
    elif status != "unavailable" and mismatch:
        status = "mismatch"
        code = "project_override_differs"
    fingerprint = _fingerprint(
        [
            profile.role,
            _text(effective["runtime"]),
            provider,
            model_id,
            endpoint_host,
            str(effective["config_version"]),
            credential_ref,
            *fallbacks,
            status,
            code,
        ]
    )
    config_version_value = effective["config_version"]
    config_version = (
        config_version_value
        if isinstance(config_version_value, int)
        else int(str(config_version_value))
    )
    return ResolvedAgentProfile(
        role=profile.role,
        runtime=_text(effective["runtime"]),
        provider=provider,
        model_id=model_id,
        configured_model_id=profile.model_id,
        endpoint_host=endpoint_host,
        config_version=config_version,
        credential_source=_credential_label(credential_ref),
        fallbacks=fallbacks,
        status=status,
        code=code,
        fingerprint=fingerprint,
    )


def _apply_config_reference(
    profile: AgentProfile,
    effective: dict[str, object],
    environ: Mapping[str, str],
) -> str | None:
    reference = profile.config_ref
    path: Path | None = None
    if reference.startswith("file:"):
        path = Path(reference.removeprefix("file:"))
    elif _ENV_REF.fullmatch(reference):
        value = environ.get(reference.removeprefix("env:"))
        if value:
            path = Path(value)
        else:
            return None
    elif reference.startswith("module:"):
        return None
    if path is None:
        return None
    if not path.exists() or not path.is_file():
        return "config_reference_unavailable"
    try:
        if path.suffix.casefold() == ".toml":
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return "config_reference_invalid"
    if not isinstance(raw, Mapping):
        return "config_reference_invalid"
    _merge_structured_profile(profile, effective, raw)
    return None


def _merge_structured_profile(
    profile: AgentProfile,
    effective: dict[str, object],
    raw: Mapping[str, object],
) -> None:
    if "llm" in raw and isinstance(raw.get("llm"), Mapping):
        llm = raw["llm"]
        assert isinstance(llm, Mapping)
        role_name = {
            "main_agent": "main",
            "planner": "planner",
            "worker": "worker",
            "second_worker": "second_worker",
        }[profile.role]
        selected = llm.get(role_name, llm.get("main"))
        runtimes = llm.get("runtimes")
        if isinstance(selected, str) and isinstance(runtimes, Mapping):
            selected_runtime = runtimes.get(selected)
            if isinstance(selected_runtime, Mapping):
                _merge_runtime(effective, selected_runtime)
                return
    _merge_runtime(effective, raw)


def _merge_runtime(effective: dict[str, object], raw: Mapping[str, object]) -> None:
    for key in ("runtime", "provider", "model_id", "model", "endpoint_host", "base_url", "config_version"):
        if key not in raw:
            continue
        value = raw[key]
        target = "model_id" if key == "model" else key
        if key in {"endpoint_host", "base_url"} and isinstance(value, str):
            parsed = urlsplit(value)
            if parsed.hostname:
                effective["endpoint_host"] = parsed.hostname + (
                    f":{parsed.port}" if parsed.port else ""
                )
        else:
            effective[target] = value
    if "auth" in raw and isinstance(raw["auth"], str) and raw["auth"].strip():
        effective["credential_ref"] = f"store:{raw['auth'].strip()}"
    elif "api_key" in raw and isinstance(raw["api_key"], str):
        value = raw["api_key"].strip()
        if value.startswith("${") and value.endswith("}"):
            effective["credential_ref"] = f"env:{value[2:-1]}"
        elif value:
            effective["credential_ref"] = "inline-secret"
    raw_fallbacks = raw.get("fallbacks")
    if isinstance(raw_fallbacks, (list, tuple)):
        effective["fallbacks"] = tuple(
            str(value).strip() for value in raw_fallbacks
        )


def _fallback_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value)


def _credential_available(reference: str, environ: Mapping[str, str]) -> bool:
    if reference == "none" or reference == "inline-secret":
        return False
    if _ENV_REF.fullmatch(reference):
        return bool(environ.get(reference.removeprefix("env:"), "").strip())
    if reference.startswith("file:"):
        path = Path(reference.removeprefix("file:"))
        return path.is_file() and os.access(path, os.R_OK)
    if reference.startswith("store:"):
        return bool(environ.get("SIDERA_CREDENTIAL_STORE_READY") == "1")
    return False


def _credential_label(reference: str) -> str:
    if reference == "inline-secret":
        return "inline-secret-redacted"
    return reference


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else str(value).strip()


def _fingerprint(values: list[str]) -> str:
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["ConfigInspectionReport", "ResolvedAgentProfile", "inspect_profiles"]
