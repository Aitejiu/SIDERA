from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.event_ingestor import ArtifactRegistry
from sidera_control_plane.application.ports import (
    ArtifactContentWriter,
    ControlPlaneUnitOfWork,
)
from sidera_control_plane.application.result_ports import (
    CapturedCode,
    CodeResultCapturePort,
    ResultOperationAttemptV1,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.code_results import CodeResultSnapshotV1
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionStatus,
)
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ResultOperationStatus,
)
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceLifecycle,
)

CODE_RESULT_CAPTURE_OPERATION_KIND = "code_result_capture"
DEFAULT_MAX_EXPORT_BYTES = 10 * 1024 * 1024
_SUCCESSFUL_TERMINAL_OUTCOME = "succeeded"
_TERMINAL_OUTCOMES = frozenset({"succeeded", "failed", "canceled", "lost"})


def code_capture_operation_id(execution_id: UUID) -> UUID:
    if not isinstance(execution_id, UUID):
        raise TypeError("execution_id must be a UUID")
    return uuid5(NAMESPACE_URL, f"sidera.code-result-capture:{execution_id}")


@dataclass(frozen=True, slots=True)
class _CaptureContext:
    execution_id: UUID
    workspace_id: UUID
    task_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    request_id: UUID
    manifest: ExecutionWorkspaceManifest
    operation: ResultOperationAttemptV1
    input_commit: str
    writable_paths: tuple[str, ...]


class CodeResultCaptureService:
    """Coordinate durable execution boundaries and immutable code results."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        code_store: CodeResultCapturePort,
        *,
        artifact_registry: ArtifactRegistry | None = None,
        artifact_content_store: ArtifactContentWriter | None = None,
        max_export_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_export_bytes <= 0:
            raise ValueError("max_export_bytes must be positive")
        self._uow_factory = uow_factory
        self._code_store = code_store
        self._artifact_registry = artifact_registry or ArtifactRegistry(
            uow_factory, clock=clock
        )
        self._artifact_content_store = artifact_content_store
        self._max_export_bytes = max_export_bytes
        self._clock = clock

    def record_execution_start(
        self,
        *,
        manifest: ExecutionWorkspaceManifest,
        input_commit: str | None = None,
        writable_paths: Sequence[str],
        operation_id: UUID | None = None,
    ) -> UUID:
        """Persist the actual input boundary before an Agent can write."""
        self._validate_manifest(manifest)
        operation_id = operation_id or code_capture_operation_id(manifest.execution_id)
        if not isinstance(operation_id, UUID):
            raise ResultError("result_operation_identity_invalid", {})
        checkout = manifest.checkouts[0]
        requested_input = input_commit or checkout.current_head
        input_revision = getattr(self._code_store, "inspect_revision", None)
        if not callable(input_revision):
            raise ResultError("result_input_inspection_unavailable", {})
        canonical_input, input_tree = cast(
            Callable[..., tuple[str, str]], input_revision
        )(
            repository_ref=checkout.repository_ref,
            revision=requested_input,
        )
        paths = _normalize_paths(writable_paths)
        now = self._now()
        payload = {
            "schema_version": 1,
            "manifest_id": str(manifest.id),
            "manifest_version": manifest.version,
            "workspace_id": str(manifest.workspace_id),
            "task_id": str(manifest.task_id),
            "execution_id": str(manifest.execution_id),
            "repository_ref": checkout.repository_ref,
            "input_commit": canonical_input,
            "input_tree": input_tree,
            "writable_paths": list(paths),
            "workspace_base_revision": checkout.base_revision,
            "fencing_token": manifest.fencing_token,
            "started_at": now.isoformat(),
        }
        with self._uow_factory() as uow:
            execution = uow.executions.get(
                manifest.execution_id, manifest.workspace_id, for_update=True
            )
            if execution is None:
                raise ResultError("result_execution_not_found", {})
            if execution.task_id != manifest.task_id:
                raise ResultError("result_manifest_context_mismatch", {})
            if execution.status not in {ExecutionStatus.LEASED, ExecutionStatus.RUNNING}:
                raise ResultError(
                    "result_execution_start_boundary_invalid",
                    {"status": execution.status.value},
                )
            if execution.active_lease_id is None or execution.last_fencing_token != manifest.fencing_token:
                raise ResultError("result_execution_fence_invalid", {})
            existing = uow.results.get_operation(
                operation_id, manifest.workspace_id, for_update=True
            )
            if existing is not None:
                if not isinstance(existing, ResultOperationAttemptV1):
                    raise ResultError("result_capture_operation_conflict", {})
                self._assert_operation_identity(
                    existing,
                    workspace_id=manifest.workspace_id,
                    execution_id=manifest.execution_id,
                    payload=payload,
                )
                return operation_id
            operation = ResultOperationAttemptV1(
                operation_id=operation_id,
                workspace_id=manifest.workspace_id,
                root_task_id=self._root_task_id(
                    uow, execution.worker_item_id, manifest.workspace_id
                ),
                operation_kind=CODE_RESULT_CAPTURE_OPERATION_KIND,
                attempt=1,
                status=ResultOperationStatus.PENDING,
                payload=payload,
                lease_owner=None,
                lease_token=None,
                lease_expiry=None,
                receipt_ref=None,
                failure_code=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            uow.results.save_operation(operation, expected_version=0)
        return operation_id

    def observe_terminal(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        operation_id: UUID,
        outcome: str = _SUCCESSFUL_TERMINAL_OUTCOME,
        fencing_token: int | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ResultOperationAttemptV1:
        """Persist terminal observation before the execution lease is released."""
        normalized = _terminal_outcome(outcome)
        now = self._now()
        with self._uow_factory() as uow:
            operation = self._capture_operation(
                uow, operation_id, workspace_id, for_update=True
            )
            execution = uow.executions.get(execution_id, workspace_id, for_update=True)
            if execution is None or operation.root_task_id != self._root_task_id(
                uow, execution.worker_item_id, workspace_id
            ):
                raise ResultError("result_capture_context_mismatch", {})
            payload = dict(operation.payload)
            observed = payload.get("terminal_observed") is True
            if observed:
                if payload.get("terminal_outcome") != normalized:
                    raise ResultError("result_terminal_observation_conflict", {})
                return operation
            if execution.status in TERMINAL_EXECUTION_STATUSES:
                raise ResultError("result_terminal_observation_too_late", {})
            if execution.status is not ExecutionStatus.RUNNING:
                raise ResultError("result_terminal_execution_not_running", {})
            expected_token = payload.get("fencing_token")
            if fencing_token is not None and fencing_token != expected_token:
                raise ResultError("result_execution_fence_invalid", {})
            if not isinstance(expected_token, int) or execution.last_fencing_token != expected_token:
                raise ResultError("result_execution_fence_invalid", {})
            if execution.active_lease_id is None:
                raise ResultError("result_execution_lease_missing", {})
            lease = uow.leases.get(execution.active_lease_id, workspace_id)
            if lease is None or execution.assigned_worker_id is None:
                raise ResultError("result_execution_lease_missing", {})
            lease.assert_owner(execution.assigned_worker_id, expected_token, now)
            payload.update(
                {
                    "terminal_observed": True,
                    "terminal_outcome": normalized,
                    "terminal_observed_at": now.isoformat(),
                    "terminal_fencing_token": expected_token,
                    "terminal_details": dict(details or {}),
                }
            )
            updated = replace(
                operation,
                status=ResultOperationStatus.RUNNING,
                payload=payload,
                updated_at=now,
                version=operation.version + 1,
            )
            return cast(
                ResultOperationAttemptV1,
                uow.results.save_operation(updated, expected_version=operation.version),
            )

    def capture_execution(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        operation_id: UUID,
    ) -> CodeResultSnapshotV1:
        """Capture and publish one execution result, safely replayable after restart."""
        with self._uow_factory() as uow:
            existing = uow.results.get_code_result_by_capture_operation(
                operation_id, workspace_id
            )
        if existing is not None:
            return existing
        try:
            context = self._load_capture_context(
                workspace_id=workspace_id,
                execution_id=execution_id,
                operation_id=operation_id,
            )
        except _ExistingSnapshot as found:
            return found.snapshot

        captured = self._code_store.inspect_capture(operation_id)
        if captured is None:
            captured = self._code_store.capture(
                manifest=context.manifest,
                input_commit=context.input_commit,
                operation_id=operation_id,
                writable_paths=context.writable_paths,
            )
        self._assert_captured_context(captured, context)
        candidates, export_state = self._artifact_candidates(captured, context)
        report = self._artifact_registry.register(
            workspace_id=workspace_id,
            task_id=context.task_id,
            execution_id=execution_id,
            session_id=None,
            candidates=candidates,
            acceptance_criteria=(),
            producer="sidera.result.capture",
            environment_ref=f"git:{captured.repository_ref}",
        )
        snapshot = _build_snapshot(
            context,
            captured,
            tuple(artifact.id for artifact in report.artifacts),
        )
        return self._persist_snapshot(
            context,
            captured,
            snapshot,
            export_state=export_state,
        )

    def _load_capture_context(
        self, *, workspace_id: UUID, execution_id: UUID, operation_id: UUID
    ) -> _CaptureContext:
        with self._uow_factory() as uow:
            existing = uow.results.get_code_result_by_capture_operation(
                operation_id, workspace_id
            )
            if existing is not None:
                raise _ExistingSnapshot(existing)
            operation = self._capture_operation(
                uow, operation_id, workspace_id, for_update=False
            )
            if operation.operation_kind != CODE_RESULT_CAPTURE_OPERATION_KIND:
                raise ResultError("result_capture_operation_conflict", {})
            payload = operation.payload
            if payload.get("terminal_observed") is not True:
                raise ResultError("result_terminal_observation_required", {})
            if payload.get("terminal_outcome") != _SUCCESSFUL_TERMINAL_OUTCOME:
                raise ResultError(
                    "result_execution_not_successful",
                    {"outcome": payload.get("terminal_outcome")},
                )
            execution = uow.executions.get(execution_id, workspace_id)
            if execution is None or payload.get("execution_id") != str(execution_id):
                raise ResultError("result_capture_context_mismatch", {})
            if execution.status not in {ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED}:
                raise ResultError(
                    "result_execution_not_capturable",
                    {"status": execution.status.value},
                )
            if execution.status is ExecutionStatus.RUNNING:
                self._assert_active_fence(uow, execution, payload)
            manifest = uow.workspaces.get_for_execution(execution_id, workspace_id)
            if manifest is None:
                raise ResultError("result_manifest_not_found", {})
            self._validate_manifest(manifest)
            if payload.get("manifest_id") != str(manifest.id):
                raise ResultError("result_capture_context_mismatch", {})
            if execution.worker_item_id is None:
                raise ResultError("result_worker_item_missing", {})
            worker_item = uow.worker_items.get(execution.worker_item_id, workspace_id)
            request = uow.execution_requests.get_by_execution_lineage(
                execution.lineage_id, workspace_id
            )
            if worker_item is None or request is None:
                raise ResultError("result_capture_context_missing", {})
            if request.worker_item_id != worker_item.id:
                raise ResultError("result_capture_context_mismatch", {})
            input_commit = payload.get("input_commit")
            writable_paths = payload.get("writable_paths")
            if not isinstance(input_commit, str) or not isinstance(writable_paths, list):
                raise ResultError("result_capture_operation_corrupt", {})
            return _CaptureContext(
                execution_id=execution.id,
                workspace_id=workspace_id,
                task_id=execution.task_id,
                root_task_id=worker_item.root_task_id,
                worker_item_id=worker_item.id,
                request_id=request.id,
                manifest=manifest,
                operation=operation,
                input_commit=input_commit,
                writable_paths=_normalize_paths(writable_paths),
            )

    def _persist_snapshot(
        self,
        context: _CaptureContext,
        captured: CapturedCode,
        snapshot: CodeResultSnapshotV1,
        *,
        export_state: Mapping[str, object],
    ) -> CodeResultSnapshotV1:
        now = self._now()
        with self._uow_factory() as uow:
            existing = uow.results.get_code_result_by_capture_operation(
                context.operation.operation_id,
                context.workspace_id,
                for_update=True,
            )
            if existing is not None:
                return existing
            operation = self._capture_operation(
                uow,
                context.operation.operation_id,
                context.workspace_id,
                for_update=True,
            )
            stored = cast(
                CodeResultSnapshotV1,
                uow.results.add_immutable("code_snapshot", snapshot),
            )
            payload = dict(operation.payload)
            payload.update(
                {
                    "captured": captured.to_mapping(),
                    "snapshot_id": str(stored.id),
                    "snapshot_content_hash": stored.content_hash,
                    "artifact_ids": [str(item) for item in stored.artifact_ids],
                    **dict(export_state),
                }
            )
            succeeded = replace(
                operation,
                status=ResultOperationStatus.SUCCEEDED,
                payload=payload,
                receipt_ref=captured.receipt_ref,
                failure_code=None,
                updated_at=now,
                version=operation.version + 1,
            )
            uow.results.save_operation(succeeded, expected_version=operation.version)
            command_id = uuid5(
                NAMESPACE_URL,
                f"sidera.code-result-capture-command:{context.operation.operation_id}",
            )
            event_id = uuid5(
                NAMESPACE_URL,
                f"sidera.code-result-captured-event:{context.operation.operation_id}",
            )
            command = CommandEnvelope(
                id=command_id,
                workspace_id=context.workspace_id,
                command_type="CaptureCodeResult",
                target_type="execution",
                actor_type="system",
                actor_id="result-capture",
                target_id=context.execution_id,
                payload={
                    "execution_id": str(context.execution_id),
                    "capture_operation_id": str(context.operation.operation_id),
                    "snapshot_id": str(stored.id),
                },
                expected_version=None,
                idempotency_key=(
                    f"result-capture:{context.operation.operation_id}"
                ),
                correlation_id=context.root_task_id,
            )
            if uow.commands.find_by_id(context.workspace_id, command_id) is None:
                uow.commands.record_applied(
                    command,
                    hash_request(command),
                    CommandResult(
                        command_id=command_id,
                        aggregate_id=context.execution_id,
                        aggregate_version=self._execution_version(
                            uow, context.execution_id, context.workspace_id
                        ),
                        event_ids=(event_id,),
                        data={"snapshot_id": str(stored.id)},
                    ),
                )
            uow.events.append_with_outbox(
                EventData(
                    id=event_id,
                    workspace_id=context.workspace_id,
                    aggregate_type="execution",
                    aggregate_id=context.execution_id,
                    aggregate_version=self._execution_version(uow, context.execution_id, context.workspace_id),
                    event_type="CodeResultCaptured",
                    payload={
                        "execution_id": str(context.execution_id),
                        "snapshot_id": str(stored.id),
                        "content_hash": stored.content_hash,
                        "capture_operation_id": str(context.operation.operation_id),
                        "export_state": dict(export_state),
                    },
                    actor_type="system",
                    actor_id="result-capture",
                    causation_id=command_id,
                    correlation_id=context.root_task_id,
                )
            )
            return stored

    def _artifact_candidates(
        self, captured: CapturedCode, context: _CaptureContext
    ) -> tuple[tuple[ArtifactCandidate, ...], Mapping[str, object]]:
        metadata = _json_bytes(captured.to_mapping())
        metadata_candidate = self._store_artifact(
            context,
            artifact_type=ArtifactType.REPORT,
            media_type="application/json",
            content=metadata,
            storage_suffix="metadata",
        )
        candidates = [metadata_candidate]
        export_state: dict[str, object] = {"export_available": True}
        try:
            patch = self._code_store.export_patch(captured)
        except ResultError as exc:
            if exc.code != "result_export_too_large":
                raise
            export_state = {
                "export_available": False,
                "export_unavailable": "result_export_too_large",
            }
        else:
            if len(patch) > self._max_export_bytes:
                raise ResultError(
                    "result_export_too_large",
                    {"size_bytes": len(patch), "max_bytes": self._max_export_bytes},
                )
            candidates.append(
                self._store_artifact(
                    context,
                    artifact_type=ArtifactType.DIFF,
                    media_type="text/x-diff",
                    content=patch,
                    storage_suffix="patch",
                )
            )
        return tuple(candidates), export_state

    def _store_artifact(
        self,
        context: _CaptureContext,
        *,
        artifact_type: ArtifactType,
        media_type: str,
        content: bytes,
        storage_suffix: str,
    ) -> ArtifactCandidate:
        digest = hashlib.sha256(content).hexdigest()
        if self._artifact_content_store is None:
            storage_ref = (
                f"artifact://sidera/result/{context.operation.operation_id}/{storage_suffix}"
            )
        else:
            storage_ref = self._artifact_content_store.put(
                context.workspace_id,
                context.execution_id,
                digest,
                content,
            )
        return ArtifactCandidate(
            artifact_type=artifact_type,
            storage_ref=storage_ref,
            content_hash=digest,
            size_bytes=len(content),
            media_type=media_type,
            provenance={
                "producer": "sidera.result.capture",
                "environment_ref": f"git:{context.manifest.checkouts[0].repository_ref}",
                "capture_operation_id": str(context.operation.operation_id),
                "storage_role": storage_suffix,
            },
            content=None,
        )

    def _capture_operation(
        self,
        uow: ControlPlaneUnitOfWork,
        operation_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool,
    ) -> ResultOperationAttemptV1:
        operation = uow.results.get_operation(
            operation_id, workspace_id, for_update=for_update
        )
        if not isinstance(operation, ResultOperationAttemptV1):
            raise ResultError("result_capture_operation_not_found", {})
        return operation

    def _assert_active_fence(
        self,
        uow: ControlPlaneUnitOfWork,
        execution: Execution,
        payload: Mapping[str, object],
    ) -> None:
        expected_token = payload.get("fencing_token")
        active_lease_id = getattr(execution, "active_lease_id", None)
        assigned_worker_id = getattr(execution, "assigned_worker_id", None)
        if (
            not isinstance(expected_token, int)
            or active_lease_id is None
            or not isinstance(assigned_worker_id, UUID)
        ):
            raise ResultError("result_execution_fence_invalid", {})
        lease = uow.leases.get(active_lease_id, execution.workspace_id)
        if lease is None:
            raise ResultError("result_execution_lease_missing", {})
        lease.assert_owner(assigned_worker_id, expected_token, self._now())

    def _assert_operation_identity(
        self,
        operation: ResultOperationAttemptV1,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        payload: Mapping[str, object],
    ) -> None:
        if (
            operation.operation_kind != CODE_RESULT_CAPTURE_OPERATION_KIND
            or operation.workspace_id != workspace_id
            or operation.payload.get("execution_id") != str(execution_id)
            or operation.payload.get("input_commit") != payload.get("input_commit")
            or operation.payload.get("input_tree") != payload.get("input_tree")
            or operation.payload.get("writable_paths") != payload.get("writable_paths")
        ):
            raise ResultError("result_capture_identity_changed", {})

    @staticmethod
    def _root_task_id(
        uow: ControlPlaneUnitOfWork,
        worker_item_id: UUID | None,
        workspace_id: UUID,
    ) -> UUID:
        if worker_item_id is None:
            raise ResultError("result_worker_item_missing", {})
        worker_item = uow.worker_items.get(worker_item_id, workspace_id)
        if worker_item is None:
            raise ResultError("result_worker_item_missing", {})
        return worker_item.root_task_id

    @staticmethod
    def _execution_version(
        uow: ControlPlaneUnitOfWork, execution_id: UUID, workspace_id: UUID
    ) -> int:
        execution = uow.executions.get(execution_id, workspace_id)
        if execution is None:
            raise ResultError("result_execution_not_found", {})
        return execution.version

    @staticmethod
    def _validate_manifest(manifest: ExecutionWorkspaceManifest) -> None:
        if not isinstance(manifest, ExecutionWorkspaceManifest):
            raise ResultError("result_manifest_invalid", {})
        if manifest.lifecycle in {
            WorkspaceLifecycle.RELEASED,
            WorkspaceLifecycle.QUARANTINED,
        }:
            raise ResultError("result_workspace_unavailable", {})
        if len(manifest.checkouts) != 1:
            raise ResultError("result_manifest_invalid", {})

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ResultError("result_capture_clock_invalid", {})
        return now.astimezone(UTC)

    @staticmethod
    def _assert_captured_context(
        captured: CapturedCode, context: _CaptureContext
    ) -> None:
        if (
            captured.operation_id != context.operation.operation_id
            or captured.repository_ref != context.manifest.checkouts[0].repository_ref
            or captured.input_commit.lower() != context.input_commit.lower()
        ):
            raise ResultError("result_capture_context_mismatch", {})


class _ExistingSnapshot(Exception):
    def __init__(self, snapshot: CodeResultSnapshotV1) -> None:
        self.snapshot = snapshot


def _build_snapshot(
    context: _CaptureContext,
    captured: CapturedCode,
    artifact_ids: tuple[UUID, ...],
) -> CodeResultSnapshotV1:
    snapshot_id = uuid5(
        NAMESPACE_URL, f"sidera.code-result-snapshot:{captured.operation_id}"
    )
    content = {
        "schema_version": 1,
        "workspace_id": str(context.workspace_id),
        "root_task_id": str(context.root_task_id),
        "worker_item_id": str(context.worker_item_id),
        "request_id": str(context.request_id),
        "execution_id": str(context.execution_id),
        "repository_ref": captured.repository_ref,
        "input_commit": captured.input_commit,
        "input_tree": captured.input_tree,
        "result_commit": captured.result_commit,
        "result_tree": captured.result_tree,
        "consumed_result_ids": [],
        "artifact_ids": [str(item) for item in artifact_ids],
        "capture_operation_id": str(captured.operation_id),
    }
    content_hash = hashlib.sha256(_json_bytes(content)).hexdigest()
    return CodeResultSnapshotV1(
        id=snapshot_id,
        schema_version=1,
        workspace_id=context.workspace_id,
        root_task_id=context.root_task_id,
        worker_item_id=context.worker_item_id,
        request_id=context.request_id,
        execution_id=context.execution_id,
        repository_ref=captured.repository_ref,
        input_commit=captured.input_commit,
        input_tree=captured.input_tree,
        result_commit=captured.result_commit,
        result_tree=captured.result_tree,
        consumed_result_ids=(),
        artifact_ids=artifact_ids,
        capture_operation_id=captured.operation_id,
        content_hash=content_hash,
        created_at=captured.created_at,
    )


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        .encode("utf-8")
    )


def _terminal_outcome(value: str) -> str:
    if not isinstance(value, str) or value.strip() not in _TERMINAL_OUTCOMES:
        raise ResultError("result_terminal_outcome_invalid", {})
    return value.strip()


def _normalize_paths(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not value:
        raise ResultError("result_writable_scope_invalid", {})
    normalized: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ResultError("result_writable_scope_invalid", {})
        path = raw.strip().replace("\\", "/")
        if path in {"", "."}:
            path = "."
        if path.startswith("/") or "\x00" in path or ".." in path.split("/"):
            raise ResultError("result_writable_scope_invalid", {})
        if path not in normalized:
            normalized.append(path)
    return (".",) if "." in normalized else tuple(normalized)


__all__ = [
    "CODE_RESULT_CAPTURE_OPERATION_KIND",
    "CodeResultCaptureService",
    "code_capture_operation_id",
]
