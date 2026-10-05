"""Product-owned main authority and per-original-Run task-tool grants."""

import hmac
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import NoReturn, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
)
from sidera_agents.api import AgentCatalog, AgentExecution, AgentRuntimes
from sidera_agents.contracts import (
    ExecutionWorkspace,
    ProfileRef,
    ProtocolState,
    ResourceKind,
    ResourceRef,
    RunSnapshot,
    RunState,
    RuntimeRef,
    SecretBinding,
    SourceRef,
    StartStatus,
    ToolBinding,
    ToolConnection,
)
from sidera_agents.errors import AgentError
from sidera_agents.ports import (
    DenyToolConnectionReader,
    ExecutionAuthorizer,
    ToolConnectionReader,
)
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneIdentity,
    ControlPlaneToolScope,
)

from sidera_product.config import ProductSettings
from sidera_product.postgres.authority_store import AgentGrantRecord, GrantStore

from .local_session import LocalSessions, token_digest


def _deny() -> NoReturn:
    raise AgentError("permission_denied", "Task-tool authority denied")


def _validate_main_refs(
    profile_ref: object, sources: object
) -> tuple[ProfileRef, tuple[SourceRef, ...]]:
    if not isinstance(profile_ref, ProfileRef) or not isinstance(sources, tuple):
        _deny()
    try:
        source_values = TypeAdapter(tuple[object, ...]).validate_python(
            sources, strict=True
        )
    except ValidationError:
        _deny()
    validated: list[SourceRef] = []
    for source in source_values:
        if not isinstance(source, SourceRef):
            _deny()
        validated.append(source)
    return profile_ref, tuple(validated)


def _validate_cp_scope(value: object) -> ControlPlaneToolScope:
    if not isinstance(value, ControlPlaneToolScope):
        _deny()
    return value


class MainAuthorityRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: ExecutionWorkspace
    sources: tuple[SourceRef, ...]
    tools: tuple[ToolBinding, ...]
    credentials: tuple[SecretBinding, ...]
    resources: tuple[ResourceRef, ...]
    profiles: tuple[ProfileRef, ...]
    runtimes: tuple[RuntimeRef, ...]


class TaskToolScopeReader(Protocol):
    def __call__(
        self, *, grant_id: UUID, connection_id: UUID
    ) -> ControlPlaneToolScope: ...


@dataclass(frozen=True, slots=True)
class VerifiedToolGrant:
    record: AgentGrantRecord
    run: RunSnapshot
    scope: ControlPlaneToolScope | None

    @property
    def identity(self) -> ControlPlaneIdentity:
        return ControlPlaneIdentity(
            self.record.principal_id, self.record.grant_id, "agent"
        )


def _same_scope(saved: AgentGrantRecord, candidate: AgentGrantRecord) -> bool:
    # Allocation timestamps, bearer and accepted Run binding are not new intent.
    return (
        replace(
            saved,
            grant_id=candidate.grant_id,
            run_id=candidate.run_id,
            created_at=candidate.created_at,
            expires_at=candidate.expires_at,
            revoked_at=candidate.revoked_at,
            bearer_digest=candidate.bearer_digest,
        )
        == candidate
    )


def _same_main_scope(saved: AgentGrantRecord, candidate: AgentGrantRecord) -> bool:
    if (
        saved.source != "product"
        or candidate.source != "product"
        or saved.role != "main"
        or candidate.role != "main"
    ):
        return False
    original = MainAuthorityRefs.model_validate(saved.authority_refs)
    observed = MainAuthorityRefs.model_validate(candidate.authority_refs)
    execution_refs = observed.model_copy(
        update={
            "profiles": original.profiles,
            "resources": original.resources,
            "runtimes": original.runtimes,
        }
    )
    return _same_scope(
        saved,
        replace(
            candidate,
            authority_refs=TypeAdapter(dict[str, JsonValue]).validate_json(
                execution_refs.model_dump_json()
            ),
        ),
    )


def _same_cp_scope(
    saved: AgentGrantRecord, candidate: AgentGrantRecord, *, original_run_id: UUID
) -> bool:
    if (
        saved.source != "control_plane"
        or candidate.source != "control_plane"
        or saved.run_id != original_run_id
        or (candidate.run_id is not None and candidate.run_id != original_run_id)
    ):
        return False
    if saved.role == "worker":
        if (
            type(saved.execution_version) is not int
            or type(candidate.execution_version) is not int
            or candidate.execution_version < saved.execution_version
        ):
            return False
        candidate = replace(candidate, execution_version=saved.execution_version)
    return _same_scope(saved, candidate)


class ProductGrants:
    def __init__(
        self,
        settings: ProductSettings,
        store: GrantStore,
        sessions: LocalSessions,
        *,
        catalog: AgentCatalog,
        runtimes: AgentRuntimes,
        execution: AgentExecution,
        read_cp_scope: TaskToolScopeReader,
        parent_authorizer: ExecutionAuthorizer,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings, self.store, self.sessions = settings, store, sessions
        self.catalog, self.runtimes, self.execution = catalog, runtimes, execution
        self.read_cp_scope = read_cp_scope
        self.parent_authorizer = parent_authorizer
        self.clock = clock or (lambda: datetime.now(UTC))
        self.connection_id = uuid5(
            settings.deployment_namespace, "sidera.product.task-tools"
        )
        self._lock = Lock()
        self._raw: dict[UUID, SecretStr] = {}
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise AgentError("product_closed", "Product task-tool authority is closed")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._raw.clear()

    def _owned_workspace(self, workspace: object) -> None:
        if (
            not isinstance(workspace, ExecutionWorkspace)
            or workspace.writable is not True
        ):
            _deny()
        for value, root in (
            (workspace.directory, self.settings.workspace_root),
            (workspace.artifact_directory, self.settings.artifact_root),
        ):
            if value is None:
                continue
            path = Path(value)
            try:
                if (
                    not path.is_absolute()
                    or ".." in path.parts
                    or path.is_symlink()
                    or not path.resolve(strict=True).is_relative_to(
                        root.resolve(strict=True)
                    )
                    or not path.is_dir()
                    or path.stat().st_uid != os.getuid()
                ):
                    _deny()
            except (OSError, RuntimeError, ValueError):
                _deny()

    def prepare_main_scope(
        self,
        *,
        session_grant_id: UUID,
        operation_id: UUID,
        profile_ref: ProfileRef,
        workspace: ExecutionWorkspace,
        sources: tuple[SourceRef, ...] = (),
    ) -> UUID:
        self._check_open()
        profile_ref, sources = _validate_main_refs(profile_ref, sources)
        identity = self.sessions.identity_for_grant(session_grant_id)
        if (
            identity is None
            or identity.actor_type != "user"
            or identity.principal_id != self.settings.local_principal_id
            or type(operation_id) is not UUID
            or profile_ref.workspace_id != self.settings.workspace_id
        ):
            _deny()
        self._owned_workspace(workspace)
        profile = self.catalog.get_profile(profile_ref, grant_id=session_grant_id)
        status = self.catalog.get_profile_status(profile_ref, grant_id=session_grant_id)
        runtime = self.runtimes.get_runtime(
            profile.draft.runtime_ref, grant_id=session_grant_id
        )
        if (
            profile.ref != profile_ref
            or status.profile_id != profile_ref.profile_id
            or not status.enabled
            or runtime.ref != profile.draft.runtime_ref
            or runtime.ref.workspace_id != self.settings.workspace_id
            or not runtime.installed
            or not runtime.enabled
            or runtime.protocol_state is not ProtocolState.READY
        ):
            _deny()
        choices = self.catalog.list_profiles(
            self.settings.workspace_id, grant_id=session_grant_id
        )
        profiles = tuple(
            dict.fromkeys((profile_ref, *(choice.ref for choice in choices)))
        )
        resources = tuple(
            dict.fromkeys(
                (
                    *profile.draft.resource_refs,
                    *(ref for choice in choices for ref in choice.draft.resource_refs),
                )
            )
        )
        runtimes = tuple(
            dict.fromkeys(
                (
                    profile.draft.runtime_ref,
                    *(choice.draft.runtime_ref for choice in choices),
                )
            )
        )
        credentials = list(profile.draft.secret_refs)
        tools = [ToolBinding(connection_id=self.connection_id)]
        for ref in profile.draft.resource_refs:
            resource = self.catalog.get_resource(ref, grant_id=session_grant_id)
            resource_status = self.catalog.get_resource_status(
                ref, grant_id=session_grant_id
            )
            if (
                resource.ref != ref
                or resource_status.resource_id != ref.resource_id
                or not resource_status.enabled
            ):
                _deny()
            credentials.extend(resource.draft.secret_refs)
            if resource.draft.kind is ResourceKind.MCP:
                tools.append(ToolBinding(resource_ref=ref))
        refs = MainAuthorityRefs(
            workspace=workspace,
            sources=sources,
            tools=tuple(tools),
            credentials=tuple(credentials),
            resources=resources,
            profiles=profiles,
            runtimes=runtimes,
        )
        now = self.clock()
        candidate = AgentGrantRecord(
            grant_id=uuid4(),
            source="product",
            role="main",
            workspace_id=self.settings.workspace_id,
            principal_id=identity.principal_id,
            profile_ref=profile_ref,
            start_operation_id=operation_id,
            native_grant_id=None,
            connection_id=self.connection_id,
            parent_session_id=session_grant_id,
            run_id=None,
            root_task_id=None,
            execution_id=None,
            worker_item_id=None,
            execution_version=None,
            binding_version=None,
            planning_request_id=None,
            planning_run_id=None,
            lease_id=None,
            fencing_token=None,
            authority_refs=TypeAdapter(dict[str, JsonValue]).validate_json(
                refs.model_dump_json()
            ),
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.tool_grant_ttl_seconds),
        )
        if self.sessions.identity_for_grant(session_grant_id) != identity:
            _deny()
        saved, _ = self.store.save_grant(candidate)
        if not _same_main_scope(saved, candidate):
            raise AgentError("grant_conflict", "Original main start scope changed")
        self._live_record(saved)
        return saved.grant_id

    def _live_record(self, record: AgentGrantRecord) -> None:
        if (
            record.workspace_id != self.settings.workspace_id
            or record.principal_id != self.settings.local_principal_id
            or record.connection_id != self.connection_id
            or record.profile_ref.workspace_id != record.workspace_id
            or record.revoked_at is not None
            or record.expires_at <= self.clock()
        ):
            _deny()
        if record.source == "product":
            if (
                record.role != "main"
                or record.parent_session_id is None
                or record.native_grant_id is not None
            ):
                _deny()
            current = self.sessions.identity_for_grant(record.parent_session_id)
            if (
                current is None
                or current.actor_type != "user"
                or current.principal_id != record.principal_id
            ):
                _deny()
        elif (
            record.source != "control_plane"
            or record.role not in {"worker", "planner"}
            or record.native_grant_id is None
        ):
            _deny()

    def _cp_scope(self, native_grant_id: UUID) -> ControlPlaneToolScope:
        scope = _validate_cp_scope(
            self.read_cp_scope(
                grant_id=native_grant_id, connection_id=self.connection_id
            )
        )
        if (
            scope.native_grant_id != native_grant_id
            or scope.connection_id != self.connection_id
            or scope.workspace_id != self.settings.workspace_id
            or scope.principal_id != self.settings.local_principal_id
        ):
            _deny()
        return scope

    def _cp_record(self, scope: ControlPlaneToolScope) -> AgentGrantRecord:
        now = self.clock()
        return AgentGrantRecord(
            grant_id=uuid4(),
            source="control_plane",
            role=scope.role,
            workspace_id=scope.workspace_id,
            principal_id=scope.principal_id,
            profile_ref=scope.profile_ref,
            start_operation_id=scope.start_operation_id,
            native_grant_id=scope.native_grant_id,
            connection_id=scope.connection_id,
            parent_session_id=None,
            run_id=scope.run_id,
            root_task_id=scope.root_task_id,
            execution_id=scope.execution_id,
            worker_item_id=scope.worker_item_id,
            execution_version=scope.execution_version,
            binding_version=scope.binding_version,
            planning_request_id=scope.planning_request_id,
            planning_run_id=scope.planning_run_id,
            lease_id=scope.lease_id,
            fencing_token=scope.fencing_token,
            authority_refs={},
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.tool_grant_ttl_seconds),
        )

    def _original_run(self, record: AgentGrantRecord) -> RunSnapshot:
        owner = record.native_grant_id or record.grant_id
        lookup = self.execution.get_start(
            record.workspace_id, record.start_operation_id, grant_id=owner
        )
        receipt = lookup.receipt
        if (
            not lookup.found
            or receipt is None
            or receipt.status is not StartStatus.ACCEPTED
            or receipt.operation_id != record.start_operation_id
            or receipt.run_id is None
            or (record.run_id is not None and record.run_id != receipt.run_id)
        ):
            _deny()
        run = self.execution.get_run(
            record.workspace_id, receipt.run_id, grant_id=owner
        )
        if (
            run.run_id != receipt.run_id
            or run.workspace_id != record.workspace_id
            or run.profile_ref != record.profile_ref
            or run.effective_config.profile_ref != record.profile_ref
            or run.state not in {RunState.STARTING, RunState.RUNNING}
        ):
            _deny()
        return run

    def _verified(
        self, record: AgentGrantRecord, *, require_running: bool
    ) -> VerifiedToolGrant:
        self._live_record(record)
        scope = None
        if record.source == "control_plane":
            if record.native_grant_id is None:
                _deny()
            scope = self._cp_scope(record.native_grant_id)
            if record.run_id is None or not _same_cp_scope(
                record, self._cp_record(scope), original_run_id=record.run_id
            ):
                _deny()
        else:
            if record.parent_session_id is None:
                _deny()
            profile = self.catalog.get_profile(
                record.profile_ref, grant_id=record.parent_session_id
            )
            status = self.catalog.get_profile_status(
                record.profile_ref, grant_id=record.parent_session_id
            )
            if (
                profile.ref != record.profile_ref
                or status.profile_id != record.profile_ref.profile_id
                or not status.enabled
            ):
                _deny()
        run = self._original_run(record)
        if require_running and (
            run.state is not RunState.RUNNING or run.active_response_id is None
        ):
            _deny()
        return VerifiedToolGrant(record, run, scope)

    def resolve(self, grant_id: UUID, connection_id: UUID) -> ToolConnection:
        self._check_open()
        if type(grant_id) is not UUID or connection_id != self.connection_id:
            _deny()
        original = self.store.find_grant(grant_id=grant_id)
        if original is None:
            scope = self._cp_scope(grant_id)
            candidate = self._cp_record(scope)
            self._live_record(candidate)
            run = self._original_run(candidate)
            candidate = replace(candidate, run_id=run.run_id)
            original, _ = self.store.save_grant(candidate)
            if not _same_cp_scope(original, candidate, original_run_id=run.run_id):
                _deny()
        else:
            self._live_record(original)
            run = self._original_run(original)
        with self._lock:
            current = self.store.find_grant(grant_id=original.grant_id)
            if current is None:
                _deny()
            if current.bearer_digest is None:
                raw = SecretStr(secrets.token_urlsafe(32))
                current, winner = self.store.bind_grant(
                    current.grant_id,
                    run_id=run.run_id,
                    bearer_digest=token_digest(raw.get_secret_value()),
                )
                if winner:
                    self._raw[current.grant_id] = raw
            token = self._raw.get(current.grant_id)
            if token is None or current.bearer_digest is None:
                raise AgentError(
                    "tool_connection_unavailable",
                    "Original tool bearer cannot be reprojected",
                )
            if not hmac.compare_digest(
                token_digest(token.get_secret_value()), current.bearer_digest
            ):
                _deny()
            self._verified(current, require_running=False)
            return ToolConnection(
                name="SIDERA Task Tools",
                transport="http",
                endpoint_or_argv=self.settings.local_url + "/product/v1/task-tools/mcp",
                headers={
                    "Authorization": SecretStr("Bearer " + token.get_secret_value())
                },
                environment={},
            )

    def verify(
        self, token: SecretStr, *, require_running: bool = False
    ) -> VerifiedToolGrant:
        self._check_open()
        raw = token.get_secret_value()
        if not 1 <= len(raw) <= 1024:
            _deny()
        digest = token_digest(raw)
        record = self.store.find_grant(bearer_digest=digest)
        if (
            record is None
            or record.bearer_digest is None
            or not hmac.compare_digest(record.bearer_digest, digest)
        ):
            _deny()
        return self._verified(record, require_running=require_running)

    def verify_grant(
        self, grant_id: UUID, *, require_running: bool = True
    ) -> VerifiedToolGrant:
        self._check_open()
        record = self.store.find_grant(grant_id=grant_id)
        if record is None or record.bearer_digest is None:
            _deny()
        return self._verified(record, require_running=require_running)

    def revoke(self, grant_id: UUID) -> None:
        self._check_open()
        self.store.revoke_grant(grant_id, now=self.clock())

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        """Structural native permission; callers separately verify the original Run."""
        self._check_open()
        if type(grant_id) is not UUID:
            _deny()
        record = self.store.find_grant(grant_id=grant_id)
        if (
            record is None
            or record.source != "product"
            or record.role != "main"
            or record.workspace_id != self.settings.workspace_id
            or record.principal_id != self.settings.local_principal_id
        ):
            _deny()
        if (
            action in {"run.read", "run.stop"}
            and type(target) is UUID
            and target == record.workspace_id
        ):
            return
        self._live_record(record)
        refs = MainAuthorityRefs.model_validate(record.authority_refs)
        parent = record.parent_session_id
        if parent is None:
            _deny()
        if (
            action == "run.start"
            and isinstance(target, ProfileRef)
            and target == record.profile_ref
        ):
            return
        if (
            action in {"run.input", "run.interrupt", "run.recover"}
            and type(target) is UUID
            and target == record.workspace_id
            and record.run_id is not None
        ):
            return
        if (
            action == "profile.list"
            and type(target) is UUID
            and target == record.workspace_id
        ):
            self.parent_authorizer.require(parent, action, target)
            return
        if (
            action == "profile.read"
            and type(target) is ProfileRef
            and target.workspace_id == record.workspace_id
            and any(
                (ref.workspace_id, ref.profile_id)
                == (target.workspace_id, target.profile_id)
                for ref in refs.profiles
            )
        ):
            self.parent_authorizer.require(parent, action, target)
            return
        if (
            action == "runtime.read"
            and type(target) is RuntimeRef
            and target.workspace_id == record.workspace_id
        ):
            self.parent_authorizer.require(parent, action, target)
            return
        if (
            action == "resource.read"
            and type(target) is ResourceRef
            and target.workspace_id == record.workspace_id
        ):
            self.parent_authorizer.require(parent, action, target)
            return
        if (
            action == "workspace.execute"
            and isinstance(target, ExecutionWorkspace)
            and target == refs.workspace
        ):
            return
        if (
            action == "source.read"
            and isinstance(target, SourceRef)
            and target in refs.sources
        ):
            return
        if (
            action == "tool.execute"
            and isinstance(target, ToolBinding)
            and target in refs.tools
        ):
            return
        if (
            action == "secret.read"
            and isinstance(target, SecretBinding)
            and target in refs.credentials
        ):
            return
        _deny()


class ToolConnectionResolver:
    def __init__(
        self, settings: ProductSettings, fallback: ToolConnectionReader | None = None
    ) -> None:
        self.connection_id = uuid5(
            settings.deployment_namespace, "sidera.product.task-tools"
        )
        self.grants: ProductGrants | None = None
        self.fallback = fallback or DenyToolConnectionReader()

    def resolve(self, grant_id: UUID, connection_id: UUID) -> ToolConnection:
        if connection_id != self.connection_id:
            return self.fallback.resolve(grant_id, connection_id)
        if self.grants is None:
            _deny()
        return self.grants.resolve(grant_id, connection_id)
