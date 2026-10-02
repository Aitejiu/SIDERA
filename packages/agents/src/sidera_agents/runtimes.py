from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from .catalog import _validate_resource_settings
from .contracts import (
    AuthenticationState,
    CapabilitySupport,
    ConfigIssue,
    ConfigValidation,
    EffectiveConfig,
    ProfileRef,
    ProtocolState,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeProbe,
    RuntimeRef,
    RuntimeSnapshot,
    SupportStatus,
)
from .errors import AgentError
from .harnesses.registry import (
    HarnessRegistry,
    default_harness_registry,
)
from .ports import (
    ExecutionAuthorizer,
    RuntimeEntry,
    RuntimeObservation,
    RuntimeStore,
)

_NON_BLOCKING_ISSUE_CODES = frozenset(
    {"runtime_protocol_unconfirmed", "authentication_unconfirmed"}
)


def _support(status: SupportStatus, reason_code: str) -> CapabilitySupport:
    return CapabilitySupport(status=status, reason_code=reason_code, constraints={})


def _issue(
    field: str,
    code: str,
    message: str,
    status: SupportStatus,
    reason_code: str | None = None,
) -> ConfigIssue:
    return ConfigIssue(
        field=field,
        code=code,
        message=message,
        support=_support(status, reason_code or code),
    )


def _checked_at(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise AgentError("clock_invalid", "Runtime observation time is invalid.")
    return value.astimezone(UTC)


def _file_identity(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"


def _inspect_target(path: Path, *, resolve: bool) -> tuple[Path, str]:
    try:
        target = path.resolve(strict=True) if resolve else path
        info = target.lstat()
    except (OSError, RuntimeError):
        raise AgentError(
            "configuration_invalid", "Executable target is unavailable.", "executable"
        ) from None
    executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not info.st_mode & executable_bits
        or not os.access(target, os.X_OK)
    ):
        raise AgentError(
            "configuration_invalid", "Executable target is invalid.", "executable"
        )
    return target, _file_identity(info)


def _installation_key(harness_id: str, executable: str) -> str:
    canonical = f"{harness_id}\0{executable}".encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


class RuntimeCatalog:
    def __init__(
        self,
        store: RuntimeStore,
        authorizer: ExecutionAuthorizer,
        *,
        registry: HarnessRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._authorizer = authorizer
        self._registry = registry or default_harness_registry()
        self._clock = clock or (lambda: datetime.now(UTC))

    def discover(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[RuntimeCandidate, ...]:
        self._authorizer.require(grant_id, "runtime.discover", workspace_id)
        candidates: list[RuntimeCandidate] = []
        for harness in self._registry.all():
            found = shutil.which(harness.executable_name)
            if found is None:
                continue
            try:
                target, _ = _inspect_target(Path(found), resolve=True)
            except AgentError:
                continue
            candidates.append(
                RuntimeCandidate(harness_id=harness.harness_id, executable=str(target))
            )
        return tuple(candidates)

    def register(
        self,
        workspace_id: UUID,
        harness_id: str,
        executable: str,
        max_active: int,
        max_sessions: int,
        *,
        grant_id: UUID,
    ) -> RuntimeSnapshot:
        self._validate_positive(max_active, "max_active")
        self._validate_positive(max_sessions, "max_sessions")
        self._authorizer.require(grant_id, "runtime.register", workspace_id)
        harness = self._registry.get(harness_id)
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise AgentError(
                "configuration_invalid", "Executable target is invalid.", "executable"
            )
        requested_path = Path(executable)
        if not requested_path.is_absolute():
            raise AgentError(
                "configuration_invalid",
                "Executable target must be absolute.",
                "executable",
            )
        target, identity = _inspect_target(requested_path, resolve=True)
        canonical = str(target)
        existing = self._store.find_runtime(workspace_id, harness_id, canonical)
        if existing is not None:
            return self._snapshot(existing)

        candidate = RuntimeCandidate(harness_id=harness_id, executable=canonical)
        probe = harness.probe(candidate, _checked_at(self._clock))
        observation = RuntimeObservation(
            probe=probe, installed=True, file_identity=identity
        )
        entry = self._store.register_runtime(
            workspace_id,
            uuid4(),
            harness_id,
            canonical,
            _installation_key(harness_id, canonical),
            observation,
            max_active,
            max_sessions,
        )
        return self._snapshot(entry)

    def refresh(self, ref: RuntimeRef, *, grant_id: UUID) -> RuntimeSnapshot:
        self._authorizer.require(grant_id, "runtime.refresh", ref)
        entry = self._store.get_runtime_entry(ref)
        now = _checked_at(self._clock)
        path = Path(entry.record.executable)
        try:
            _, identity = _inspect_target(path, resolve=False)
        except AgentError:
            previous = entry.record.observation.probe
            probe = RuntimeProbe(
                actual_version=previous.actual_version,
                protocol_state=ProtocolState.UNAVAILABLE,
                capabilities=dict(previous.capabilities),
                checked_at=now,
            )
            observation = RuntimeObservation(
                probe=probe,
                installed=False,
                file_identity=None,
                auth_state=entry.record.observation.auth_state,
            )
        else:
            harness = self._registry.get(entry.record.harness_id)
            candidate = RuntimeCandidate(
                harness_id=entry.record.harness_id,
                executable=entry.record.executable,
            )
            probe = harness.probe(candidate, now)
            observation = RuntimeObservation(
                probe=probe,
                installed=True,
                file_identity=identity,
                auth_state=entry.record.observation.auth_state,
            )
        return self._snapshot(self._store.update_runtime_observation(ref, observation))

    def get_runtime(self, ref: RuntimeRef, *, grant_id: UUID) -> RuntimeSnapshot:
        self._authorizer.require(grant_id, "runtime.read", ref)
        return self._snapshot(self._store.get_runtime_entry(ref))

    def list_runtimes(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> tuple[RuntimeSnapshot, ...]:
        self._authorizer.require(grant_id, "runtime.list", workspace_id)
        return tuple(
            self._snapshot(entry)
            for entry in self._store.list_runtime_entries(workspace_id)
        )

    def describe(self, harness_id: str, *, grant_id: UUID) -> RuntimeDescriptor:
        self._authorizer.require(grant_id, "runtime.describe", harness_id)
        return self._registry.get(harness_id).describe()

    def validate_profile(self, ref: ProfileRef, *, grant_id: UUID) -> ConfigValidation:
        self._authorizer.require(grant_id, "profile.read", ref)
        profile = self._store.get_profile(ref)
        profile_status = self._store.get_profile_status(
            ref.workspace_id, ref.profile_id
        )
        issues: list[ConfigIssue] = []
        if not profile_status.enabled:
            issues.append(
                _issue(
                    "profile_ref",
                    "profile_disabled",
                    "This profile is disabled.",
                    SupportStatus.UNSUPPORTED,
                )
            )

        runtime: RuntimeSnapshot | None = None
        runtime_ref = profile.draft.runtime_ref
        if runtime_ref.workspace_id != ref.workspace_id:
            issues.append(
                _issue(
                    "runtime_ref",
                    "configuration_invalid",
                    "Runtime reference belongs to another workspace.",
                    SupportStatus.UNSUPPORTED,
                )
            )
        else:
            self._authorizer.require(grant_id, "runtime.read", runtime_ref)
            try:
                entry = self._store.get_runtime_entry(runtime_ref)
            except AgentError as error:
                if error.code != "not_found":
                    raise
                issues.append(
                    _issue(
                        "runtime_ref",
                        "runtime_unavailable",
                        "The referenced runtime is not registered.",
                        SupportStatus.UNSUPPORTED,
                    )
                )
            else:
                runtime = self._snapshot(entry)
                issues.extend(self._runtime_issues(runtime))
                try:
                    harness = self._registry.get(entry.record.harness_id)
                except AgentError as error:
                    if error.code != "unsupported_harness":
                        raise
                    issues.append(
                        _issue(
                            "runtime_ref",
                            "unsupported_capability",
                            "The registered harness has no trusted implementation.",
                            SupportStatus.UNSUPPORTED,
                            "unsupported_harness",
                        )
                    )
                else:
                    issues.extend(harness.validate(profile, runtime))

        issues.extend(self._resource_issues(profile, runtime, grant_id))
        preview = None
        if runtime is not None:
            draft = profile.draft
            preview = EffectiveConfig(
                profile_ref=profile.ref,
                config_hash=profile.config_hash,
                runtime_ref=draft.runtime_ref,
                actual_runtime_version=runtime.actual_version,
                selected_model=draft.model_id,
                observed_model=None,
                inherited_fields=tuple(
                    field_name
                    for field_name, value in (
                        ("model_id", draft.model_id),
                        ("thinking", draft.thinking),
                    )
                    if value is None
                ),
                applied_fields=(),
                resource_refs=draft.resource_refs,
                context_receipts=(),
            )
        return ConfigValidation(
            valid=all(issue.code in _NON_BLOCKING_ISSUE_CODES for issue in issues),
            issues=tuple(issues),
            effective_preview=preview,
        )

    def set_runtime_limits(
        self,
        ref: RuntimeRef,
        max_active: int,
        max_sessions: int,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> RuntimeSnapshot:
        self._validate_positive(max_active, "max_active")
        self._validate_positive(max_sessions, "max_sessions")
        self._validate_positive(expected_version, "expected_version")
        self._authorizer.require(grant_id, "runtime.configure", ref)
        self._authorizer.require(grant_id, "runtime.read", ref)
        entry = self._store.set_runtime_configuration(
            ref,
            expected_version=expected_version,
            max_active=max_active,
            max_sessions=max_sessions,
        )
        return self._snapshot(entry)

    def set_runtime_enabled(
        self,
        ref: RuntimeRef,
        enabled: bool,
        expected_version: int,
        *,
        grant_id: UUID,
    ) -> RuntimeSnapshot:
        if type(enabled) is not bool:
            raise AgentError(
                "configuration_invalid", "Enabled must be a boolean.", "enabled"
            )
        self._validate_positive(expected_version, "expected_version")
        self._authorizer.require(grant_id, "runtime.configure", ref)
        self._authorizer.require(grant_id, "runtime.read", ref)
        entry = self._store.set_runtime_configuration(
            ref, expected_version=expected_version, enabled=enabled
        )
        return self._snapshot(entry)

    @staticmethod
    def _validate_positive(value: int, field: str) -> None:
        if type(value) is not int or value < 1:
            raise AgentError(
                "configuration_invalid",
                f"{field} must be a positive integer.",
                field,
            )

    @staticmethod
    def _snapshot(entry: RuntimeEntry) -> RuntimeSnapshot:
        record = entry.record
        installation = entry.installation
        probe = record.observation.probe
        return RuntimeSnapshot(
            ref=record.ref,
            harness_id=record.harness_id,
            executable=record.executable,
            actual_version=probe.actual_version,
            installed=record.observation.installed,
            enabled=installation.enabled,
            version=installation.version,
            protocol_state=probe.protocol_state,
            auth_state=record.observation.auth_state,
            capabilities=dict(probe.capabilities),
            checked_at=probe.checked_at,
            limits={
                "max_active": installation.max_active,
                "max_sessions": installation.max_sessions,
            },
        )

    @staticmethod
    def _runtime_issues(runtime: RuntimeSnapshot) -> list[ConfigIssue]:
        issues: list[ConfigIssue] = []
        if not runtime.installed:
            issues.append(
                _issue(
                    "runtime_ref",
                    "runtime_unavailable",
                    "The selected runtime executable is unavailable.",
                    SupportStatus.UNSUPPORTED,
                )
            )
        if not runtime.enabled:
            issues.append(
                _issue(
                    "runtime_ref",
                    "runtime_disabled",
                    "The selected runtime is disabled.",
                    SupportStatus.UNSUPPORTED,
                )
            )
        if runtime.protocol_state != ProtocolState.READY:
            unavailable = runtime.protocol_state == ProtocolState.UNAVAILABLE
            issues.append(
                _issue(
                    "runtime_ref",
                    (
                        "runtime_protocol_unavailable"
                        if unavailable
                        else "runtime_protocol_unconfirmed"
                    ),
                    (
                        "Runtime protocol is unavailable."
                        if unavailable
                        else "Runtime protocol readiness has not been confirmed."
                    ),
                    (
                        SupportStatus.UNSUPPORTED
                        if unavailable
                        else SupportStatus.UNCONFIRMED
                    ),
                    (
                        "acp_handshake_unavailable"
                        if unavailable
                        else "acp_handshake_not_confirmed"
                    ),
                )
            )
        if runtime.auth_state != AuthenticationState.READY:
            failed = runtime.auth_state == AuthenticationState.FAILED
            issues.append(
                _issue(
                    "runtime_ref",
                    "authentication_failed" if failed else "authentication_unconfirmed",
                    (
                        "Runtime authentication failed."
                        if failed
                        else "Runtime authentication has not been confirmed."
                    ),
                    (
                        SupportStatus.UNSUPPORTED
                        if failed
                        else SupportStatus.UNCONFIRMED
                    ),
                )
            )
        return issues

    def _resource_issues(
        self,
        profile,
        runtime: RuntimeSnapshot | None,
        grant_id: UUID,
    ) -> list[ConfigIssue]:
        issues: list[ConfigIssue] = []
        for index, ref in enumerate(profile.draft.resource_refs):
            field = f"resource_refs[{index}]"
            if ref.workspace_id != profile.ref.workspace_id:
                issues.append(
                    _issue(
                        field,
                        "configuration_invalid",
                        "Resource reference belongs to another workspace.",
                        SupportStatus.UNSUPPORTED,
                    )
                )
                continue
            self._authorizer.require(grant_id, "resource.read", ref)
            try:
                resource = self._store.get_resource(ref)
                status = self._store.get_resource_status(
                    ref.workspace_id, ref.resource_id
                )
            except AgentError as error:
                if error.code != "not_found":
                    raise
                issues.append(
                    _issue(
                        field,
                        "resource_unavailable",
                        "The pinned resource revision is unavailable.",
                        SupportStatus.UNSUPPORTED,
                    )
                )
                continue
            if not status.enabled:
                issues.append(
                    _issue(
                        field,
                        "resource_disabled",
                        "The pinned resource is disabled.",
                        SupportStatus.UNSUPPORTED,
                    )
                )
            try:
                _validate_resource_settings(
                    resource.draft.kind, resource.draft.settings
                )
            except AgentError:
                issues.append(
                    _issue(
                        f"{field}.settings",
                        "configuration_invalid",
                        "The pinned resource settings are invalid.",
                        SupportStatus.UNSUPPORTED,
                    )
                )
                continue
            if runtime is None:
                continue
            capability_name = resource.draft.kind.value
            if (
                resource.draft.kind.value == "mcp"
                and resource.draft.settings.get("transport") == "http"
            ):
                capability_name = "mcp_http"
            support = runtime.capabilities.get(capability_name)
            if support is None:
                support = _support(
                    SupportStatus.UNCONFIRMED, "resource_capability_unconfirmed"
                )
            if support.status != SupportStatus.SUPPORTED:
                issues.append(
                    ConfigIssue(
                        field=f"{field}.{capability_name}",
                        code="unsupported_capability",
                        message="This resource capability is not confirmed for the runtime.",
                        support=support,
                    )
                )
        return issues
