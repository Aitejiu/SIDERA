from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError

_GIT_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHELL_METACHARACTERS = frozenset(";&|$`()<>\n\r")
_SPEC_FIELDS = frozenset(
    {
        "schema_version",
        "repository_ref",
        "base_commit",
        "integration_worker_item_key",
        "input_group_keys",
        "criteria",
        "checks",
        "verification_binding",
        "rework_policy",
        "apply_policy",
        "finalization_policy",
    }
)
_CHECK_FIELDS = frozenset(
    {
        "key",
        "version",
        "argv",
        "cwd",
        "environment_ref",
        "prepare_argvs",
        "timeout_seconds",
        "success_exit_codes",
        "report_paths",
        "required",
    }
)
_CRITERION_FIELDS = frozenset({"key", "text", "mode", "check_keys"})
_BINDING_FIELDS = frozenset(
    {
        "adapter_module_ref",
        "adapter_config_version",
        "workspace_module_ref",
        "workspace_config_version",
    }
)
_REWORK_FIELDS = frozenset(
    {
        "max_auto_rework_rounds",
        "max_auto_rework_seconds",
        "allowed_worker_item_keys",
    }
)
_APPLY_FIELDS = frozenset({"apply_on_accept", "mode", "target_ref"})


class CriterionMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class ApplyMode(StrEnum):
    FAST_FORWARD_ONLY = "fast_forward_only"


def _mapping(value: object, label: str, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    keys = set(value)
    unknown = sorted(keys - fields)
    missing = sorted(fields - keys)
    if unknown:
        raise ValidationError(f"unknown {label} fields: {', '.join(unknown)}")
    if missing:
        raise ValidationError(f"missing {label} fields: {', '.join(missing)}")
    if not all(isinstance(key, str) for key in keys):
        raise ValidationError(f"{label} field names must be strings")
    return value


def _text(value: object, label: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValidationError(f"{label} is too long")
    return normalized


def _key(value: object, label: str) -> str:
    result = _text(value, label, max_length=128)
    if _KEY_PATTERN.fullmatch(result) is None:
        raise ValidationError(f"{label} has invalid format")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be a boolean")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError(f"{label} must be an array")
    return tuple(value)


def _unique_texts(value: object, label: str) -> tuple[str, ...]:
    result = tuple(_text(item, f"{label} item", max_length=512) for item in _sequence(value, label))
    if len(set(result)) != len(result):
        raise ValidationError(f"{label} must not contain duplicates")
    return result


def _git_oid(value: object, label: str) -> str:
    result = _text(value, label, max_length=64).lower()
    if _GIT_OID_PATTERN.fullmatch(result) is None:
        raise ValidationError(f"{label} must be a Git object id")
    return result


def _relative_path(value: object, label: str) -> str:
    result = _text(value, label, max_length=1024)
    if result.startswith("/") or "\\" in result:
        raise ValidationError(f"{label} must be relative")
    path = PurePosixPath(result)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise ValidationError(f"{label} must be relative")
    return result


def _safe_argv(value: object, label: str) -> tuple[str, ...]:
    argv = tuple(_text(item, f"{label} item", max_length=4096) for item in _sequence(value, label))
    if not argv:
        raise ValidationError(f"{label} must not be empty")
    if any(any(char in token for char in _SHELL_METACHARACTERS) for token in argv):
        raise ValidationError(f"{label} must not contain shell metacharacters")
    return argv


def _enum(value: object, enum_type: type[StrEnum], label: str) -> StrEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} has an unsupported value") from exc


@dataclass(frozen=True, slots=True)
class CriterionSpecV1:
    key: str
    text: str
    mode: CriterionMode
    check_keys: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object) -> CriterionSpecV1:
        mapping = _mapping(value, "criterion", _CRITERION_FIELDS)
        check_keys = _unique_texts(mapping["check_keys"], "criterion check_keys")
        mode = cast(CriterionMode, _enum(mapping["mode"], CriterionMode, "criterion mode"))
        if mode is CriterionMode.AUTOMATIC and not check_keys:
            raise ValidationError("automatic criterion requires a check")
        return cls(
            key=_key(mapping["key"], "criterion key"),
            text=_text(mapping["text"], "criterion text"),
            mode=mode,
            check_keys=check_keys,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "text": self.text,
            "mode": self.mode.value,
            "check_keys": list(self.check_keys),
        }


@dataclass(frozen=True, slots=True)
class CheckSpecV1:
    key: str
    version: int
    argv: tuple[str, ...]
    cwd: str
    environment_ref: str
    prepare_argvs: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    success_exit_codes: tuple[int, ...]
    report_paths: tuple[str, ...]
    required: bool

    @classmethod
    def from_mapping(cls, value: object) -> CheckSpecV1:
        mapping = _mapping(value, "check", _CHECK_FIELDS)
        raw_prepares = _sequence(mapping["prepare_argvs"], "check prepare_argvs")
        prepare_argvs = tuple(
            _safe_argv(item, "check prepare argv") for item in raw_prepares
        )
        raw_exit_codes = _sequence(mapping["success_exit_codes"], "check success_exit_codes")
        exit_codes = tuple(_non_negative_int(item, "success exit code") for item in raw_exit_codes)
        if not exit_codes or len(set(exit_codes)) != len(exit_codes):
            raise ValidationError("check success_exit_codes must be unique and non-empty")
        return cls(
            key=_key(mapping["key"], "check key"),
            version=_positive_int(mapping["version"], "check version"),
            argv=_safe_argv(mapping["argv"], "check argv"),
            cwd=_relative_path(mapping["cwd"], "check cwd"),
            environment_ref=_text(mapping["environment_ref"], "check environment_ref"),
            prepare_argvs=prepare_argvs,
            timeout_seconds=_positive_int(mapping["timeout_seconds"], "check timeout_seconds"),
            success_exit_codes=exit_codes,
            report_paths=tuple(
                _relative_path(item, "check report path")
                for item in _sequence(mapping["report_paths"], "check report_paths")
            ),
            required=_bool(mapping["required"], "check required"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "version": self.version,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "environment_ref": self.environment_ref,
            "prepare_argvs": [list(argv) for argv in self.prepare_argvs],
            "timeout_seconds": self.timeout_seconds,
            "success_exit_codes": list(self.success_exit_codes),
            "report_paths": list(self.report_paths),
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class VerificationBindingV1:
    adapter_module_ref: UUID
    adapter_config_version: int
    workspace_module_ref: UUID
    workspace_config_version: int

    @classmethod
    def from_mapping(cls, value: object) -> VerificationBindingV1:
        mapping = _mapping(value, "verification binding", _BINDING_FIELDS)
        return cls(
            adapter_module_ref=_uuid(mapping["adapter_module_ref"], "adapter_module_ref"),
            adapter_config_version=_positive_int(
                mapping["adapter_config_version"], "adapter_config_version"
            ),
            workspace_module_ref=_uuid(mapping["workspace_module_ref"], "workspace_module_ref"),
            workspace_config_version=_positive_int(
                mapping["workspace_config_version"], "workspace_config_version"
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "adapter_module_ref": str(self.adapter_module_ref),
            "adapter_config_version": self.adapter_config_version,
            "workspace_module_ref": str(self.workspace_module_ref),
            "workspace_config_version": self.workspace_config_version,
        }


@dataclass(frozen=True, slots=True)
class ReworkPolicyV1:
    max_auto_rework_rounds: int
    max_auto_rework_seconds: int
    allowed_worker_item_keys: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object) -> ReworkPolicyV1:
        mapping = _mapping(value, "rework policy", _REWORK_FIELDS)
        return cls(
            max_auto_rework_rounds=_non_negative_int(
                mapping["max_auto_rework_rounds"], "max_auto_rework_rounds"
            ),
            max_auto_rework_seconds=_positive_int(
                mapping["max_auto_rework_seconds"], "max_auto_rework_seconds"
            ),
            allowed_worker_item_keys=_unique_texts(
                mapping["allowed_worker_item_keys"], "allowed_worker_item_keys"
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_auto_rework_rounds": self.max_auto_rework_rounds,
            "max_auto_rework_seconds": self.max_auto_rework_seconds,
            "allowed_worker_item_keys": list(self.allowed_worker_item_keys),
        }


@dataclass(frozen=True, slots=True)
class ApplyPolicyV1:
    apply_on_accept: bool
    mode: ApplyMode
    target_ref: str | None

    @classmethod
    def from_mapping(cls, value: object) -> ApplyPolicyV1:
        mapping = _mapping(value, "apply policy", _APPLY_FIELDS)
        apply_on_accept = _bool(mapping["apply_on_accept"], "apply_on_accept")
        mode = cast(ApplyMode, _enum(mapping["mode"], ApplyMode, "apply mode"))
        target = mapping["target_ref"]
        if target is not None:
            target = _text(target, "target_ref", max_length=512)
            if not target.startswith("refs/"):
                raise ValidationError("target_ref must be a full ref")
        if apply_on_accept and target is None:
            raise ValidationError("automatic application requires a target ref")
        return cls(apply_on_accept=apply_on_accept, mode=mode, target_ref=target)

    def to_mapping(self) -> dict[str, object]:
        return {
            "apply_on_accept": self.apply_on_accept,
            "mode": self.mode.value,
            "target_ref": self.target_ref,
        }


def _uuid(value: object, label: str) -> UUID:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc


@dataclass(frozen=True, slots=True)
class IntegrationSpecV1:
    schema_version: int
    repository_ref: str
    base_commit: str
    integration_worker_item_key: str
    input_group_keys: tuple[str, ...]
    criteria: tuple[CriterionSpecV1, ...]
    checks: tuple[CheckSpecV1, ...]
    verification_binding: VerificationBindingV1
    rework_policy: ReworkPolicyV1
    apply_policy: ApplyPolicyV1
    finalization_policy: str

    @classmethod
    def from_mapping(cls, value: object) -> IntegrationSpecV1:
        mapping = _mapping(value, "integration spec", _SPEC_FIELDS)
        schema_version = mapping["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValidationError("unsupported integration spec schema_version")
        criteria = tuple(
            CriterionSpecV1.from_mapping(item)
            for item in _sequence(mapping["criteria"], "criteria")
        )
        checks = tuple(
            CheckSpecV1.from_mapping(item)
            for item in _sequence(mapping["checks"], "checks")
        )
        if not criteria:
            raise ValidationError("integration spec requires criteria")
        if not checks:
            raise ValidationError("integration spec requires checks")
        _ensure_unique((item.key for item in criteria), "criterion keys")
        _ensure_unique((item.key for item in checks), "check keys")
        check_keys = {item.key for item in checks}
        for criterion in criteria:
            unknown = set(criterion.check_keys) - check_keys
            if unknown:
                raise ValidationError(
                    f"criterion {criterion.key} references unknown check {min(unknown)}"
                )
        return cls(
            schema_version=1,
            repository_ref=_text(mapping["repository_ref"], "repository_ref"),
            base_commit=_git_oid(mapping["base_commit"], "base_commit"),
            integration_worker_item_key=_key(
                mapping["integration_worker_item_key"], "integration_worker_item_key"
            ),
            input_group_keys=_unique_texts(mapping["input_group_keys"], "input_group_keys"),
            criteria=criteria,
            checks=checks,
            verification_binding=VerificationBindingV1.from_mapping(
                mapping["verification_binding"]
            ),
            rework_policy=ReworkPolicyV1.from_mapping(mapping["rework_policy"]),
            apply_policy=ApplyPolicyV1.from_mapping(mapping["apply_policy"]),
            finalization_policy=_finalization_policy(mapping["finalization_policy"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "repository_ref": self.repository_ref,
            "base_commit": self.base_commit,
            "integration_worker_item_key": self.integration_worker_item_key,
            "input_group_keys": list(self.input_group_keys),
            "criteria": [item.to_mapping() for item in self.criteria],
            "checks": [item.to_mapping() for item in self.checks],
            "verification_binding": self.verification_binding.to_mapping(),
            "rework_policy": self.rework_policy.to_mapping(),
            "apply_policy": self.apply_policy.to_mapping(),
            "finalization_policy": self.finalization_policy,
        }


def _ensure_unique(values: Iterable[str], label: str) -> None:
    values = tuple(values)
    if len(set(values)) != len(values):
        raise ValidationError(f"{label} must not contain duplicates")


def _finalization_policy(value: object) -> str:
    result = _text(value, "finalization_policy", max_length=64)
    if result != "stop_unused":
        raise ValidationError("finalization_policy must be stop_unused")
    return result


def validate_integration_spec(
    spec: IntegrationSpecV1,
    worker_keys: frozenset[str],
    input_group_keys: frozenset[str],
) -> None:
    if not isinstance(spec, IntegrationSpecV1):
        raise ValidationError("integration spec has an invalid type")
    if spec.integration_worker_item_key not in worker_keys:
        raise ValidationError("integration WorkerItem is not in the plan")
    missing_groups = set(spec.input_group_keys) - set(input_group_keys)
    if missing_groups:
        raise ValidationError(
            f"integration spec references unknown input group {min(missing_groups)}"
        )
    if not set(spec.rework_policy.allowed_worker_item_keys) <= set(worker_keys):
        raise ValidationError("rework policy references an unknown WorkerItem")


__all__ = [
    "ApplyMode",
    "ApplyPolicyV1",
    "CheckSpecV1",
    "CriterionMode",
    "CriterionSpecV1",
    "IntegrationSpecV1",
    "ReworkPolicyV1",
    "VerificationBindingV1",
    "validate_integration_spec",
]
