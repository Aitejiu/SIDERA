from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.context_materialization import (
    ContextMaterializationError,
    ContextMaterializer,
    build_materialization_audit,
)
from sidera_control_plane.application.errors import InvalidCursor, NotFound
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    KnowledgeCatalogPort,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextMaterializationAudit,
    ContextMaterializationMode,
    ContextMaterializationOutcome,
    ContextMaterializationV1,
    ContextPackageManifest,
    ContextPackageManifestV2,
    ContextPurpose,
)
from sidera_control_plane.domain.errors import (
    DomainError,
    InvalidTransition,
    ValidationError,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.planning import PlanningRunStatus
from sidera_control_plane.domain.work_context import WorkContextRevision


@dataclass(frozen=True, slots=True)
class ContextPackageAssemblyView:
    request_id: UUID
    status: str
    attempt: int
    next_attempt_at: datetime | None
    error_code: str | None
    request_fingerprint: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ContextPackageAcknowledgementView:
    package_id: UUID
    consumer_type: str
    consumer_id: UUID
    context_version: int | None
    package_hash: str
    adapter_module_id: UUID
    adapter_config_version: int
    acknowledged_at: datetime


@dataclass(frozen=True, slots=True)
class ContextPackageMaterializationAuditView:
    audit_id: UUID
    package_id: UUID
    consumer_type: str
    consumer_id: UUID
    context_version: int | None
    attempt: int
    outcome: str
    section_count: int
    byte_count: int
    input_token_count: int
    failure_code: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ContextPackageView:
    manifest: ContextPackageManifest
    assembly: ContextPackageAssemblyView
    work_context_revision_number: int
    work_context_content_hash: str
    acknowledgements: tuple[ContextPackageAcknowledgementView, ...]
    materialization_audits: tuple[ContextPackageMaterializationAuditView, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeSubmissionView:
    submission_id: UUID
    workspace_id: UUID
    producer_type: str
    producer_id: UUID
    planning_run_id: UUID | None
    execution_id: UUID | None
    worker_item_id: UUID | None
    task_id: UUID | None
    subtask_task_id: UUID | None
    proposal_hash: str
    status: str
    attempt: int
    knowledge_command_id: UUID | None
    terminal_candidate_id: UUID | None
    candidate_deep_link: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeSubmissionPageView:
    items: tuple[KnowledgeSubmissionView, ...]
    next_cursor: str | None


class ContextPackageQueryStore(Protocol):
    def get_package(
        self, package_id: UUID, workspace_id: UUID
    ) -> ContextPackageView | None: ...

    def get_package_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        manifest_schema_version: int = 1,
    ) -> ContextPackageView | None: ...

    def list_submissions(
        self,
        workspace_id: UUID,
        *,
        planning_run_id: UUID | None = None,
        execution_id: UUID | None = None,
        worker_item_id: UUID | None = None,
        task_id: UUID | None = None,
        limit: int,
        cursor: str | None,
    ) -> KnowledgeSubmissionPageView: ...

    def materialization_input(
        self, package_id: UUID, workspace_id: UUID
    ) -> tuple[ContextPackageManifest, WorkContextRevision] | None: ...

    def next_materialization_attempt(
        self, package_id: UUID, workspace_id: UUID
    ) -> int: ...

    def append_materialization_audit(
        self,
        audit: ContextMaterializationAudit,
        *,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None: ...

    def mark_corrupt(
        self,
        package_id: UUID,
        workspace_id: UUID,
        *,
        reason: str,
        now: datetime,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None: ...


class ContextPackageOperationError(DomainError):
    def __init__(
        self, code: str, message: str, *, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class ContextPackageQueryService:
    def __init__(
        self,
        store: ContextPackageQueryStore,
        *,
        catalog_factory: Callable[[], KnowledgeCatalogPort],
        actor_type: str,
        actor_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._catalog_factory = catalog_factory
        self._actor_type = actor_type
        self._actor_id = actor_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_package(self, workspace_id: UUID, package_id: UUID) -> ContextPackageView:
        package = self._store.get_package(package_id, workspace_id)
        if package is None:
            raise NotFound("context package not found")
        return package

    def get_by_consumer(
        self,
        workspace_id: UUID,
        consumer_id: UUID,
        purpose: ContextPurpose,
        *,
        manifest_schema_version: int = 1,
    ) -> ContextPackageView:
        package = self._store.get_package_for_consumer(
            purpose,
            consumer_id,
            workspace_id,
            manifest_schema_version=manifest_schema_version,
        )
        if package is None:
            raise NotFound("context package not found")
        return package

    def list_submissions(
        self,
        workspace_id: UUID,
        *,
        planning_run_id: UUID | None = None,
        execution_id: UUID | None = None,
        worker_item_id: UUID | None = None,
        task_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> KnowledgeSubmissionPageView:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return self._store.list_submissions(
            workspace_id,
            planning_run_id=planning_run_id,
            execution_id=execution_id,
            worker_item_id=worker_item_id,
            task_id=task_id,
            limit=limit,
            cursor=cursor,
        )

    def materialize_owner(
        self,
        workspace_id: UUID,
        package_id: UUID,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ContextMaterializationV1:
        materialization_input = self._store.materialization_input(
            package_id, workspace_id
        )
        if materialization_input is None:
            raise NotFound("context package not found")
        manifest, revision = materialization_input
        if isinstance(manifest, ContextPackageManifestV2):
            raise ContextPackageOperationError(
                "context_materialization_v2_not_supported",
                "context package v2 materialization is not available in this worker",
                details={"package_id": str(package_id)},
            )
        now = self._clock()
        catalog = self._catalog_factory()
        try:
            materialization = ContextMaterializer(catalog).materialize(
                manifest,
                revision,
                mode=ContextMaterializationMode.OWNER_AUDIT,
                workspace_id=workspace_id,
                expected_package_hash=manifest.package_hash,
                owner_capability=True,
                now=now,
                correlation_id=correlation_id,
                caller_request_id=caller_request_id,
            )
        except ContextMaterializationError as exc:
            audit = build_materialization_audit(
                manifest,
                attempt=self._store.next_materialization_attempt(
                    package_id, workspace_id
                ),
                outcome=ContextMaterializationOutcome.FAILED,
                occurred_at=now,
                failure_code=exc.code,
            )
            self._store.append_materialization_audit(
                audit,
                actor_type=self._actor_type,
                actor_id=self._actor_id,
                causation_id=caller_request_id,
                correlation_id=correlation_id,
            )
            if exc.corrupt:
                self._store.mark_corrupt(
                    package_id,
                    workspace_id,
                    reason=exc.code,
                    now=now,
                    actor_type=self._actor_type,
                    actor_id=self._actor_id,
                    causation_id=caller_request_id,
                    correlation_id=correlation_id,
                )
            raise ContextPackageOperationError(
                exc.code,
                str(exc),
                details={"package_id": str(package_id)},
            ) from exc
        finally:
            close = getattr(catalog, "close", None)
            if callable(close):
                close()

        audit = build_materialization_audit(
            manifest,
            attempt=self._store.next_materialization_attempt(package_id, workspace_id),
            outcome=ContextMaterializationOutcome.SUCCEEDED,
            occurred_at=now,
            materialization=materialization,
        )
        self._store.append_materialization_audit(
            audit,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            causation_id=caller_request_id,
            correlation_id=correlation_id,
        )
        return materialization


class ContextPackageCommandHandler:
    command_types = frozenset({"RetryContextAssembly", "RevokeContextPackage"})

    def __init__(
        self,
        *,
        local_actor_type: str,
        local_actor_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "RetryContextAssembly":
            return self._retry(uow, envelope)
        if envelope.command_type == "RevokeContextPackage":
            return self._revoke(uow, envelope)
        raise ValidationError(
            f"unsupported context package command: {envelope.command_type}"
        )

    def _retry(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_type != "context_assembly" or envelope.target_id is None:
            raise ValidationError(
                "RetryContextAssembly requires a context_assembly target"
            )
        request = uow.context_assemblies.get_for_update(
            envelope.target_id, envelope.workspace_id
        )
        if request is None:
            raise NotFound("context assembly request not found")
        if envelope.expected_version != request.version:
            raise ValidationError(
                "context assembly version is stale",
                details={
                    "expected": envelope.expected_version,
                    "actual": request.version,
                },
            )
        fingerprint = _required_hash(envelope.payload, "request_fingerprint")
        if fingerprint != request.request_fingerprint:
            raise ContextPackageOperationError(
                "context_fingerprint_conflict",
                "context assembly fingerprint changed",
            )
        now = self._clock()
        if now >= request.expires_at:
            raise ContextPackageOperationError(
                "context_package_expired",
                "context assembly expiry has passed; create a new consumer",
            )
        if request.status not in {
            ContextAssemblyStatus.BLOCKED,
            ContextAssemblyStatus.RETRY_WAIT,
        }:
            raise InvalidTransition(
                "only blocked or retry_wait context assemblies can be retried"
            )
        self._assert_consumer_not_started(uow, request, now=now)
        updated = uow.context_assemblies.reset_for_retry(
            request,
            expected_version=request.version,
            now=now,
        )
        event = EventData(
            id=uuid5(
                NAMESPACE_URL,
                f"context-assembly-retry:{request.request_id}:{updated.version}",
            ),
            workspace_id=request.workspace_id,
            aggregate_type="context_assembly",
            aggregate_id=request.request_id,
            aggregate_version=updated.version,
            event_type="ContextAssemblyRetryRequested",
            payload={
                "context_assembly_request_id": str(request.request_id),
                "consumer_id": str(request.consumer_id),
                "purpose": request.purpose.value,
                "status": updated.status.value,
                "request_fingerprint": request.request_fingerprint,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=request.request_id,
            aggregate_version=updated.version,
            event_ids=(event.id,),
            data={
                "context_assembly_request_id": str(request.request_id),
                "status": updated.status.value,
            },
        )

    def _revoke(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_type != "context_package" or envelope.target_id is None:
            raise ValidationError(
                "RevokeContextPackage requires a context_package target"
            )
        if not (
            envelope.actor_type == "system"
            or (
                envelope.actor_type == self._local_actor_type
                and envelope.actor_id == self._local_actor_id
            )
        ):
            raise ValidationError("only the context package owner can revoke a package")
        package = uow.context_packages.get(
            envelope.target_id, envelope.workspace_id, for_update=True
        )
        if package is None:
            raise NotFound("context package not found")
        expected_status = _required_text(envelope.payload, "expected_package_status")
        expected_hash = _required_hash(envelope.payload, "expected_package_hash")
        if expected_status != package.status.value:
            raise ContextPackageOperationError(
                "context_package_status_conflict",
                "context package status changed",
                details={"expected": expected_status, "actual": package.status.value},
            )
        if expected_hash != package.package_hash:
            raise ContextPackageOperationError(
                "context_package_hash_conflict",
                "context package hash changed",
            )
        if envelope.expected_version != 1:
            raise ValidationError("context package expected_version must be 1")
        scope = _required_text(envelope.payload, "revocation_scope")
        reason = _required_text(envelope.payload, "reason")
        now = self._clock()
        revoked = uow.context_packages.revoke(
            package.package_id,
            package.workspace_id,
            expected_package_hash=expected_hash,
            reason=reason,
            scope=scope,
            now=now,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        attention_event = EventData(
            id=uuid5(
                NAMESPACE_URL,
                f"context-package-attention:{package.package_id}:{now.isoformat()}",
            ),
            workspace_id=package.workspace_id,
            aggregate_type="attention",
            aggregate_id=uuid5(NAMESPACE_URL, f"context-package:{package.package_id}"),
            aggregate_version=1,
            event_type="ContextPackageAttentionRequired",
            payload={
                "package_id": str(package.package_id),
                "package_hash": package.package_hash,
                "scope": revoked.revocation_scope.value
                if revoked.revocation_scope
                else scope,
                "reason": reason,
                "requires_attention": True,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.events.append_with_outbox(attention_event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=package.package_id,
            aggregate_version=1,
            event_ids=(attention_event.id,),
            data={
                "package_id": str(package.package_id),
                "package_status": revoked.status.value,
                "revocation_scope": revoked.revocation_scope.value
                if revoked.revocation_scope
                else scope,
            },
        )

    @staticmethod
    def _assert_consumer_not_started(
        uow: ControlPlaneUnitOfWork,
        request: ContextAssemblyRequest,
        *,
        now: datetime,
    ) -> None:
        if request.purpose is ContextPurpose.PLANNING_RUN:
            run = uow.planning_runs.get(
                request.consumer_id, request.workspace_id, for_update=True
            )
            if run is None:
                raise NotFound("planning run not found")
            if run.status is PlanningRunStatus.BLOCKED and (
                run.error_code or ""
            ).startswith("context_"):
                planning_request = uow.planning_requests.get(
                    run.planning_request_id, request.workspace_id, for_update=True
                )
                if planning_request is None:
                    raise NotFound("planning request not found")
                previous_run_version = run.version
                previous_request_version = planning_request.version
                planning_request.retry_context_assembly(run.id, now=now)
                updated_run = planning_request.run(run.id)
                uow.planning_runs.save(updated_run, previous_run_version)
                uow.planning_requests.save(planning_request, previous_request_version)
                return
            if run.status is not PlanningRunStatus.QUEUED:
                raise ContextPackageOperationError(
                    "context_consumer_started",
                    "planning run has already started or finished",
                )
            return
        execution = uow.executions.get(
            request.consumer_id, request.workspace_id, for_update=True
        )
        if execution is None:
            raise NotFound("execution not found")
        if execution.status is not ExecutionStatus.QUEUED:
            raise ContextPackageOperationError(
                "context_consumer_started",
                "execution has already started or finished",
            )


def _required_text(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _required_hash(payload: Mapping[str, object], name: str) -> str:
    value = _required_text(payload, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError(f"{name} must be a lowercase SHA-256 hash")
    return value


def encode_submission_cursor(created_at: datetime, submission_id: UUID) -> str:
    payload = {
        "created_at": created_at.astimezone(UTC).isoformat(),
        "id": str(submission_id),
    }
    return (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )


def decode_submission_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        submission_id = UUID(str(payload["id"]))
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidCursor("invalid knowledge submission cursor") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise InvalidCursor("invalid knowledge submission cursor")
    return created_at, submission_id
