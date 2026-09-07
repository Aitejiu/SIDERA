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

from sidera_control_plane.config import Settings

type ReadinessStatus = Literal["ok", "missing", "invalid", "unavailable"]
type RepositoryResolver = (
    Mapping[str, object] | Callable[[str], object | None]
)

_SHA1 = re.compile(r"^[0-9a-fA-F]{40}$")
_SHELL_TOKENS = frozenset({";", "&&", "||", "|", "&", ">", ">>", "<"})


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
    argparse.ArgumentParser(
        description="Validate the local SIDERA result integration boundary"
    ).parse_args(argv)
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
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {"status": "error", "error_code": f"readiness_{type(exc).__name__}"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
    if not report.enabled:
        return 0
    return 0 if report.ready else 2


def _json_env(name: str, default: object) -> object:
    raw = os.environ.get(name)
    if not raw:
        return default
    value = json.loads(raw)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
