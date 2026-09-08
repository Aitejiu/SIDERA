from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from sidera_control_plane.config import Settings
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.local_gate_record import validate_local_gate_record

type ReadinessStatus = Literal["ok", "missing", "invalid", "unavailable"]
type RepositoryResolver = (
    Mapping[str, object] | Callable[[str], object | None]
)

_SHA1 = re.compile(r"^[0-9a-fA-F]{40}$")
_SHELL_TOKENS = frozenset({";", "&&", "||", "|", "&", ">", ">>", "<"})
_REAL_PROVIDER = "opencode-go"
_REAL_MODEL_PREFIX = "opencode-go/"
_REAL_CREDENTIAL_SOURCES = frozenset({"opencode-credential-store"})
_REAL_MAX_EXECUTIONS = 5
_REAL_MAX_WALL_SECONDS = 1_800
_DATABASE_NAME = re.compile(r"^sidera_test(?:_[A-Za-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class ResultReadinessCheck:
    name: str
    status: ReadinessStatus
    code: str
    message: str

    @property
    def ready(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class ResultReadinessReport:
    enabled: bool
    ready: bool
    checks: tuple[ResultReadinessCheck, ...]
    missing_capabilities: tuple[str, ...]

    def check(self, name: str) -> ResultReadinessCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
            "missing_capabilities": list(self.missing_capabilities),
        }


@dataclass(frozen=True, slots=True)
class RealRunAuthorization:
    """Non-secret, bounded authorization for a real result-integration run."""

    run_id: UUID
    provider: str
    model: str
    endpoint_host: str
    credential_source: str
    fallbacks: tuple[str, ...]
    repository: Path
    max_executions: int
    wall_clock_seconds: int

    def reserve_execution(self, executions_started: int) -> int:
        """Validate a durable counter before creating an external session."""

        if type(executions_started) is not int or executions_started < 0:
            raise ResultError("real_budget_invalid", {})
        if executions_started >= self.max_executions:
            raise ResultError("real_run_budget_exhausted", {})
        return executions_started + 1

    def safe_summary(self) -> dict[str, object]:
        """Return fields safe for a run log; never include credentials or paths."""

        return {
            "run_id": str(self.run_id),
            "provider": self.provider,
            "model": self.model,
            "endpoint_host": self.endpoint_host,
            "fallbacks": list(self.fallbacks),
            "max_executions": self.max_executions,
            "wall_clock_seconds": self.wall_clock_seconds,
        }


def authorize_real_result_run(
    settings: Settings,
    local_gate_record: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None = None,
) -> RealRunAuthorization:
    """Authorize one bounded real OpenCode result run without starting a session."""

    if not isinstance(settings, Settings):
        raise TypeError("real result authorization requires Settings")
    values = os.environ if environ is None else environ
    if (
        values.get("SIDERA_RUN_REAL_RESULT_INTEGRATION") != "1"
        or values.get("SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED") != "1"
    ):
        raise ResultError("real_run_not_authorized", {})
    if not settings.result_integration_enabled:
        raise ResultError("real_result_integration_disabled", {})
    if not isinstance(local_gate_record, Mapping):
        raise ResultError("real_local_gate_invalid", {})

    _validate_real_local_gate(local_gate_record)
    _validate_optional_local_gate_evidence(local_gate_record, values)
    approved = _mapping_value(local_gate_record, "approved")
    database = _mapping_value(local_gate_record, "database")
    repository_record = _mapping_value(local_gate_record, "repository")
    if approved is None or database is None or repository_record is None:
        raise ResultError("real_local_gate_invalid", {})

    provider = _real_required(values, "SIDERA_REAL_RESULT_PROVIDER")
    model = _real_required(values, "SIDERA_REAL_RESULT_MODEL")
    endpoint_host = _real_endpoint(values, "SIDERA_REAL_RESULT_ENDPOINT_HOST")
    credential_source = _real_required(values, "SIDERA_REAL_RESULT_CREDENTIAL_SOURCE")
    repository = _real_repository(values, repository_record)

    approved_provider = _approved_text(approved, "provider")
    approved_model = _approved_text(approved, "model")
    approved_endpoint = _approved_endpoint(approved)
    approved_credential_source = _approved_text(approved, "credential_source")
    if approved_provider != _REAL_PROVIDER or "deepseek" in approved_provider.casefold():
        raise ResultError("real_provider_not_approved", {})
    if (
        not approved_model.startswith(_REAL_MODEL_PREFIX)
        or "deepseek" in approved_model.casefold()
    ):
        raise ResultError("real_model_not_approved", {})
    if approved_credential_source not in _REAL_CREDENTIAL_SOURCES:
        raise ResultError("real_credential_source_invalid", {})
    if provider != approved_provider:
        raise ResultError("real_provider_not_approved", {})
    if model != approved_model:
        raise ResultError("real_model_not_approved", {})
    if endpoint_host != approved_endpoint:
        raise ResultError("real_endpoint_not_approved", {})
    if credential_source != approved_credential_source:
        raise ResultError("real_credential_source_invalid", {})
    if any(
        token in credential_source.casefold()
        for token in ("key=", "token=", "secret=", "password=")
    ):
        raise ResultError("real_credential_source_invalid", {})

    gate_fallbacks = _approved_fallbacks(approved)
    configured_fallbacks = _csv(values.get("SIDERA_REAL_RESULT_FALLBACKS"))
    declared_fallbacks = _csv(values.get("SIDERA_REAL_RESULT_APPROVED_FALLBACKS"))
    if not set(declared_fallbacks).issubset(gate_fallbacks):
        raise ResultError("real_fallback_not_approved", {})
    if not set(configured_fallbacks).issubset(gate_fallbacks):
        raise ResultError("real_fallback_not_approved", {})
    if any("deepseek" in item.casefold() for item in (*gate_fallbacks, *configured_fallbacks)):
        raise ResultError("real_fallback_not_approved", {})

    max_executions = _bounded_real_int(
        values.get("SIDERA_REAL_RESULT_MAX_EXECUTIONS"),
        default=_REAL_MAX_EXECUTIONS,
        upper=_REAL_MAX_EXECUTIONS,
    )
    wall_clock_seconds = _bounded_real_int(
        values.get("SIDERA_REAL_RESULT_WALL_SECONDS"),
        default=_REAL_MAX_WALL_SECONDS,
        upper=_REAL_MAX_WALL_SECONDS,
    )
    run_id = _real_run_id(values)
    return RealRunAuthorization(
        run_id=run_id,
        provider=provider,
        model=model,
        endpoint_host=endpoint_host,
        credential_source=credential_source,
        fallbacks=configured_fallbacks,
        repository=repository,
        max_executions=max_executions,
        wall_clock_seconds=wall_clock_seconds,
    )


def _validate_real_local_gate(record: Mapping[str, object]) -> None:
    if record.get("status") != "PASS" or record.get("provider_free") is not True:
        raise ResultError("real_local_gate_invalid", {})
    commit = record.get("commit")
    if not isinstance(commit, str) or _SHA1.fullmatch(commit) is None:
        raise ResultError("real_local_gate_invalid", {})

    database = _mapping_value(record, "database")
    if (
        database is None
        or database.get("scheme") != "postgresql+psycopg"
        or database.get("host") not in {"127.0.0.1", "localhost", "::1"}
        or not isinstance(database.get("name"), str)
        or _DATABASE_NAME.fullmatch(str(database["name"])) is None
    ):
        raise ResultError("real_local_gate_invalid", {})

    repository = _mapping_value(record, "repository")
    repository_path = None if repository is None else repository.get("path")
    if (
        repository is None
        or repository.get("isolated") is not True
        or not isinstance(repository_path, str)
        or not Path(repository_path).is_absolute()
        or not Path(repository_path).is_dir()
        or not (Path(repository_path) / ".git").exists()
    ):
        raise ResultError("real_local_gate_invalid", {})

    approved = _mapping_value(record, "approved")
    if approved is None:
        raise ResultError("real_local_gate_invalid", {})
    _approved_text(approved, "provider")
    _approved_text(approved, "model")
    _approved_endpoint(approved)
    _approved_text(approved, "credential_source")
    _approved_fallbacks(approved)


def validate_local_gate_evidence(
    record: Mapping[str, object], repository: Path
) -> None:
    """Require current provider-free local evidence before a real run is bound."""

    validation = validate_local_gate_record(record, repository)
    if not validation.ok:
        raise ResultError(validation.code, {})


def _validate_optional_local_gate_evidence(
    record: Mapping[str, object], values: Mapping[str, str]
) -> None:
    """Validate an explicit local evidence envelope without changing old gate files."""

    evidence = record.get("local_gate_evidence")
    if evidence is None:
        return
    if not isinstance(evidence, Mapping):
        raise ResultError("real_local_gate_invalid", {})
    repository_value = values.get("SIDERA_LOCAL_GATE_REPOSITORY")
    if repository_value is None or not repository_value.strip():
        raise ResultError("real_local_gate_invalid", {})
    validate_local_gate_evidence(evidence, Path(repository_value).expanduser())


def _mapping_value(value: object, key: str) -> Mapping[str, object] | None:
    candidate = value.get(key) if isinstance(value, Mapping) else None
    return candidate if isinstance(candidate, Mapping) else None


def _approved_text(approved: Mapping[str, object], key: str) -> str:
    value = approved.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResultError("real_local_gate_invalid", {})
    return value.strip()


def _approved_endpoint(approved: Mapping[str, object]) -> str:
    value = _approved_text(approved, "endpoint_host")
    return _parse_endpoint(value, code="real_local_gate_invalid")


def _approved_fallbacks(approved: Mapping[str, object]) -> tuple[str, ...]:
    value = approved.get("fallbacks")
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ResultError("real_local_gate_invalid", {})
    fallbacks = tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )
    if len(fallbacks) != len(value) or len(set(fallbacks)) != len(fallbacks):
        raise ResultError("real_local_gate_invalid", {})
    return fallbacks


def _real_required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise ResultError("real_configuration_missing", {})
    return value.strip()


def _real_run_id(values: Mapping[str, str]) -> UUID:
    value = _real_required(values, "SIDERA_REAL_RESULT_RUN_ID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ResultError("real_run_id_invalid", {}) from exc


def _real_endpoint(values: Mapping[str, str], name: str) -> str:
    return _parse_endpoint(_real_required(values, name), code="real_endpoint_invalid")


def _parse_endpoint(value: str, *, code: str) -> str:
    if any(character.isspace() for character in value) or "://" in value:
        raise ResultError(code, {})
    try:
        parsed = urlsplit(f"//{value}")
        _ = parsed.port
    except ValueError as exc:
        raise ResultError(code, {}) from exc
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(character in value for character in "/?#@")
    ):
        raise ResultError(code, {})
    return value.lower()


def _real_repository(
    values: Mapping[str, str], record: Mapping[str, object]
) -> Path:
    gate_value = record.get("path")
    requested_value = values.get("SIDERA_REAL_RESULT_REPOSITORY")
    if not isinstance(gate_value, str) or requested_value is None:
        raise ResultError("real_repository_not_approved", {})
    requested = Path(requested_value).expanduser()
    gate_path = Path(gate_value).expanduser()
    if (
        not requested.is_absolute()
        or requested.resolve() != gate_path.resolve()
        or not requested.is_dir()
        or not (requested / ".git").exists()
    ):
        raise ResultError("real_repository_not_approved", {})
    return requested.resolve()


def _csv(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items if len(set(items)) == len(items) else tuple(dict.fromkeys(items))


def _bounded_real_int(value: str | None, *, default: int, upper: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ResultError("real_budget_invalid", {}) from exc
    if parsed < 1 or parsed > upper:
        raise ResultError("real_budget_invalid", {})
    return parsed


def inspect_result_readiness(
    settings: Settings,
    module_catalog: object,
    repository_resolver: RepositoryResolver,
) -> ResultReadinessReport:
    """Validate declared result-integration boundaries without starting work."""

    if not isinstance(settings, Settings):
        raise TypeError("result readiness requires Settings")
    if not settings.result_integration_enabled:
        return ResultReadinessReport(
            enabled=False,
            ready=False,
            checks=(),
            missing_capabilities=(),
        )

    checks: list[ResultReadinessCheck] = []
    missing_capabilities = _missing_capabilities(module_catalog)
    checks.append(
        _check(
            "capabilities",
            "module_capability_missing" if missing_capabilities else "ok",
            "missing" if missing_capabilities else "ok",
            (
                "required result integration capabilities are unavailable"
                if missing_capabilities
                else "required result integration capabilities are declared"
            ),
        )
    )

    repository, repository_check = _repository_check(
        settings, repository_resolver
    )
    checks.append(repository_check)
    checks.append(_environment_check(settings, repository_resolver))
    checks.append(
        _check_specs(
            settings,
            repository_resolver,
            repository_path=repository,
        )
    )
    checks.append(_result_store_check(settings.result_store_root))
    checks.append(_retention_policy_check())
    checks.append(_apply_policy_check(settings))
    return ResultReadinessReport(
        enabled=True,
        ready=not missing_capabilities
        and all(check.ready for check in checks),
        checks=tuple(checks),
        missing_capabilities=tuple(missing_capabilities),
    )


def _missing_capabilities(module_catalog: object) -> tuple[str, ...]:
    entries = _catalog_entries(module_catalog)
    active = [entry for entry in entries if _lifecycle(entry) == "active"]
    worker_entries = [
        entry for entry in active if _module_type(entry) == "worker_adapter"
    ]
    workspace_entries = [
        entry for entry in active if _module_type(entry) == "workspace_provider"
    ]
    missing: list[str] = []
    if not any(_supports(entry, "code_result_versions", 1) for entry in worker_entries):
        missing.append("code_result_versions")
    if not any(_supports(entry, "verification_versions", 1) for entry in worker_entries):
        missing.append("verification_versions")
    if not any(
        _supports(entry, "code_input_versions", 1)
        and _capability_bool(entry, "git_worktree")
        and _capability_bool(entry, "writable")
        for entry in workspace_entries
    ):
        missing.append("code_input_versions")
        missing.append("workspace_provider_git_worktree")
    return tuple(missing)


def _catalog_entries(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        modules = value.get("modules")
        if isinstance(modules, Sequence) and not isinstance(modules, (str, bytes)):
            return tuple(modules)
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(value)
    modules = getattr(value, "modules", None)
    if isinstance(modules, Sequence) and not isinstance(modules, (str, bytes)):
        return tuple(modules)
    return (value,) if value is not None else ()


def _module_type(entry: object) -> str | None:
    value = entry.get("module_type") if isinstance(entry, Mapping) else getattr(entry, "module_type", None)
    if not isinstance(value, str):
        return None
    return value.strip().lower().replace("-", "_")


def _lifecycle(entry: object) -> str | None:
    value = entry.get("lifecycle") if isinstance(entry, Mapping) else getattr(entry, "lifecycle", None)
    value = getattr(value, "value", value)
    return value.strip().lower() if isinstance(value, str) else None


def _capabilities(entry: object) -> object:
    if isinstance(entry, Mapping):
        return entry.get("capabilities", entry)
    return getattr(entry, "capabilities", {})


def _capability_value(entry: object, name: str) -> object:
    capabilities = _capabilities(entry)
    if isinstance(capabilities, Mapping):
        return capabilities.get(name)
    return getattr(capabilities, name, None)


def _supports(entry: object, name: str, version: int) -> bool:
    value = _capability_value(entry, name)
    if isinstance(value, (list, tuple, set, frozenset)):
        return version in value
    method = getattr(_capabilities(entry), f"supports_{name[:-9]}version", None)
    return bool(method(version)) if callable(method) else False


def _capability_bool(entry: object, name: str) -> bool:
    return _capability_value(entry, name) is True


def _check(
    name: str,
    code: str,
    status: ReadinessStatus,
    message: str,
) -> ResultReadinessCheck:
    return ResultReadinessCheck(name=name, code=code, status=status, message=message)


def _resolve_reference(resolver: RepositoryResolver, ref: str, group: str) -> object | None:
    if callable(resolver):
        try:
            return resolver(ref)
        except Exception:  # noqa: BLE001
            return None
    grouped = resolver.get(group)
    if isinstance(grouped, Mapping) and ref in grouped:
        return grouped[ref]
    return resolver.get(ref)


def _repository_check(
    settings: Settings,
    resolver: RepositoryResolver,
) -> tuple[Path | None, ResultReadinessCheck]:
    raw = _resolve_reference(resolver, settings.result_repository_ref, "repositories")
    if raw is None:
        return None, _check(
            "repository",
            "repository_not_configured",
            "missing",
            "the configured repository reference is not registered",
        )
    entry = raw if isinstance(raw, Mapping) else {"path": raw}
    path_value = entry.get("path")
    if not isinstance(path_value, (str, Path)):
        return None, _check(
            "repository",
            "repository_path_invalid",
            "invalid",
            "the configured repository path is invalid",
        )
    path = Path(path_value).expanduser()
    if not path.is_dir() or not (path / ".git").exists():
        return None, _check(
            "repository",
            "repository_unavailable",
            "unavailable",
            "the configured repository is not an available Git worktree",
        )
    base_commit = entry.get("base_commit") or settings.result_base_commit
    if base_commit is not None:
        if not isinstance(base_commit, str) or not _SHA1.fullmatch(base_commit):
            return path, _check(
                "repository",
                "repository_base_commit_invalid",
                "invalid",
                "the configured repository base commit is invalid",
            )
        objects = entry.get("git_objects")
        if isinstance(objects, (list, tuple, set, frozenset)):
            exists = base_commit in objects or base_commit.lower() in {
                str(value).lower() for value in objects
            }
        else:
            exists = _git_object_exists(settings.git_executable, path, base_commit, settings.result_readiness_timeout_seconds)
        if not exists:
            return path, _check(
                "repository",
                "repository_git_object_missing",
                "missing",
                "the configured repository base commit is unavailable",
            )
    return path, _check(
        "repository",
        "ok",
        "ok",
        "the configured repository and base object are available",
    )


def _git_object_exists(
    executable: str,
    path: Path,
    commit: str,
    timeout_seconds: float,
) -> bool:
    try:
        result = subprocess.run(
            [executable, "-C", str(path), "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _environment_check(
    settings: Settings,
    resolver: RepositoryResolver,
) -> ResultReadinessCheck:
    raw = _resolve_reference(resolver, settings.result_environment_ref, "environments")
    if raw is None:
        return _check(
            "environment",
            "environment_not_configured",
            "missing",
            "the configured check environment reference is not registered",
        )
    if isinstance(raw, Mapping):
        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return _check(
                "environment",
                "environment_version_invalid",
                "invalid",
                "the configured check environment has no valid version",
            )
    elif isinstance(raw, (str, Path)) and not Path(raw).exists():
        return _check(
            "environment",
            "environment_unavailable",
            "unavailable",
            "the configured check environment is unavailable",
        )
    return _check(
        "environment",
        "ok",
        "ok",
        "the configured check environment is available",
    )


def _check_specs(
    settings: Settings,
    resolver: RepositoryResolver,
    *,
    repository_path: Path | None,
) -> ResultReadinessCheck:
    checks: object | None = None
    if isinstance(resolver, Mapping):
        checks = resolver.get("checks")
        if checks is None:
            repository = _resolve_reference(
                resolver, settings.result_repository_ref, "repositories"
            )
            if isinstance(repository, Mapping):
                checks = repository.get("checks")
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)) or not checks:
        return _check(
            "checks",
            "check_specs_missing",
            "missing",
            "at least one declared check is required",
        )
    if repository_path is None:
        return _check(
            "checks",
            "check_repository_unavailable",
            "unavailable",
            "checks cannot be validated without the configured repository",
        )
    for item in checks:
        error = _validate_check(item, repository_path, resolver)
        if error is not None:
            return _check("checks", "check_spec_invalid", "invalid", error)
    return _check(
        "checks",
        "ok",
        "ok",
        "declared checks use bounded argv and repository-relative paths",
    )


def _validate_check(
    item: object,
    repository_path: Path,
    resolver: RepositoryResolver,
) -> str | None:
    if not isinstance(item, Mapping):
        return "a check must be a mapping"
    key = item.get("key")
    if not isinstance(key, str) or not key.strip():
        return "a check key is required"
    argv = item.get("argv")
    if not _valid_argv(argv):
        return "check argv must be a non-empty argument array"
    cwd = item.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd or Path(cwd).is_absolute():
        return "check cwd must be a relative path"
    resolved_cwd = (repository_path / cwd).resolve()
    try:
        resolved_cwd.relative_to(repository_path.resolve())
    except ValueError:
        return "check cwd must remain inside the configured repository"
    if not resolved_cwd.is_dir():
        return "check cwd is unavailable"
    environment_ref = item.get("environment_ref")
    if not isinstance(environment_ref, str) or not environment_ref.strip():
        return "check environment_ref is required"
    if _resolve_reference(resolver, environment_ref, "environments") is None:
        return "check environment_ref is not registered"
    timeout = item.get("timeout_seconds", 0)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > 86_400
    ):
        return "check timeout_seconds is outside the allowed range"
    return None


def _valid_argv(value: object) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            return False
        if item in _SHELL_TOKENS or item.startswith(("$(", "${")):
            return False
    return True


def _result_store_check(path_value: Path) -> ResultReadinessCheck:
    path = Path(path_value).expanduser()
    candidate = path if path.exists() else path.parent
    if not candidate.is_dir() or not os.access(candidate, os.W_OK | os.X_OK):
        return _check(
            "result_store",
            "result_store_unwritable",
            "unavailable",
            "the result store is not writable or cannot be created under its parent",
        )
    if path.exists() and not path.is_dir():
        return _check(
            "result_store",
            "result_store_invalid",
            "invalid",
            "the result store path is not a directory",
        )
    return _check(
        "result_store",
        "ok",
        "ok",
        "the result store has a writable directory boundary",
    )


def _retention_policy_check() -> ResultReadinessCheck:
    return _check(
        "retention",
        "retention_conservative",
        "ok",
        "managed result refs are retained and historical snapshots are not auto-GC'd",
    )


def _apply_policy_check(settings: Settings) -> ResultReadinessCheck:
    if settings.result_apply_on_accept and not settings.result_apply_target_ref:
        return _check(
            "apply_policy",
            "apply_target_required",
            "invalid",
            "automatic apply requires an explicit target ref",
        )
    return _check(
        "apply_policy",
        "ok",
        "ok",
        (
            "automatic apply is disabled by default"
            if not settings.result_apply_on_accept
            else "automatic apply has an explicit target ref"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the local SIDERA result integration boundary"
    )
    parser.add_argument(
        "--real-gate",
        action="store_true",
        help="also evaluate the explicitly authorized real-run boundary",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        catalog = _json_env("SIDERA_RESULT_MODULE_CATALOG", [])
        checks = _json_env("SIDERA_RESULT_CHECKS", [])
        repository_path = os.environ.get("SIDERA_RESULT_REPOSITORY_PATH")
        base_commit = os.environ.get("SIDERA_RESULT_BASE_COMMIT")
        resolver: dict[str, object] = {
            "repositories": {
                settings.result_repository_ref: {
                    "path": repository_path,
                    "base_commit": base_commit,
                }
            },
            "environments": {
                settings.result_environment_ref: {"version": 1}
            },
            "checks": checks,
        }
        report = inspect_result_readiness(settings, catalog, resolver)
        payload = report.as_dict()
        real_gate_ready = True
        if args.real_gate:
            real_gate = _inspect_real_gate(settings)
            payload["real_result_gate"] = real_gate
            real_gate_ready = real_gate.get("status") == "authorized"
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {"status": "error", "error_code": f"readiness_{type(exc).__name__}"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    readiness_ready = True if not report.enabled else report.ready
    return 0 if readiness_ready and real_gate_ready else 2


def _inspect_real_gate(settings: Settings) -> dict[str, object]:
    if (
        os.environ.get("SIDERA_RUN_REAL_RESULT_INTEGRATION") != "1"
        or os.environ.get("SIDERA_REAL_RESULT_INTEGRATION_AUTHORIZED") != "1"
    ):
        return {"status": "blocked", "error_code": "real_run_not_authorized"}
    path_value = os.environ.get("SIDERA_REAL_RESULT_LOCAL_GATE_FILE")
    if path_value is None or not path_value.strip():
        return {"status": "blocked", "error_code": "real_local_gate_missing"}
    try:
        value = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "blocked", "error_code": "real_local_gate_invalid"}
    try:
        authorization = authorize_real_result_run(settings, value)
    except ResultError as exc:
        return {"status": "blocked", "error_code": exc.code}
    return {"status": "authorized", "summary": authorization.safe_summary()}


def _json_env(name: str, default: object) -> object:
    raw = os.environ.get(name)
    if not raw:
        return default
    value = json.loads(raw)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
