from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.code_result_capture import (
    code_capture_operation_id,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.result_operations import ResultError


class ResultIntegrationService:
    """Publish immutable integration candidates from one released execution."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def publish_candidate(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        operation_id: UUID,
    ) -> IntegrationCandidateV1:
        self._require_uuid(workspace_id, "workspace_id")
        self._require_uuid(execution_id, "execution_id")
        self._require_uuid(operation_id, "operation_id")

        with self._uow_factory() as uow:
            execution = uow.executions.get(
                execution_id, workspace_id, for_update=True
            )
            if execution is None:
                raise ResultError(
                    "integration_execution_not_found",
                    {"execution_id": str(execution_id)},
                )
            if execution.status is not ExecutionStatus.SUCCEEDED:
                raise ResultError(
                    "integration_execution_not_succeeded",
                    {
                        "execution_id": str(execution_id),
                        "status": execution.status.value,
                    },
                )
            if execution.worker_item_id is None:
                raise ResultError("integration_worker_required", {})

            request = uow.execution_requests.get_by_execution_lineage(
                execution.lineage_id, workspace_id, for_update=True
            )
            if request is None:
                raise ResultError(
                    "integration_execution_request_not_found",
                    {"execution_id": str(execution_id)},
                )
            head = uow.results.get_head(
                workspace_id, request.root_task_id, for_update=True
            )
            plan_head = uow.plans.get_head(
                request.root_task_id, workspace_id, for_update=True
            )
            if head is None:
                raise ResultError(
                    "integration_result_head_not_found",
                    {"root_task_id": str(request.root_task_id)},
                )
            if plan_head is None:
                raise ResultError(
                    "integration_plan_head_not_found",
                    {"root_task_id": str(request.root_task_id)},
                )
            plan = uow.plans.get(plan_head.active_snapshot_id, workspace_id)
            if plan is None:
                raise ResultError(
                    "integration_plan_not_found",
                    {"plan_snapshot_id": str(plan_head.active_snapshot_id)},
                )
            spec = plan.integration_spec
            if not isinstance(spec, IntegrationSpecV1):
                raise ResultError("integration_spec_required", {})

            if request.worker_item_key != spec.integration_worker_item_key:
                raise ResultError(
                    "integration_worker_required",
                    {
                        "expected_worker_item_key": spec.integration_worker_item_key,
                        "actual_worker_item_key": request.worker_item_key,
                    },
                )
            self._validate_execution_scope(execution, request, request.root_task_id)
            worker_item = uow.worker_items.get(request.worker_item_id, workspace_id)
            if (
                worker_item is None
                or worker_item.root_task_id != request.root_task_id
            ):
                raise ResultError("integration_worker_required", {})
            if request.result_derivation_kind == "verification":
                raise ResultError("integration_worker_required", {})

            current_plan_hash = plan_snapshot_content_hash(plan)
            self._validate_plan_scope(request, plan, current_plan_hash)
            selection = self._load_selection(uow, request, workspace_id)
            self._validate_selection_scope(
                selection,
                request=request,
                plan=plan,
                plan_content_hash=current_plan_hash,
                workspace_id=workspace_id,
            )

            integration_snapshot = uow.results.get_code_result_by_capture_operation(
                code_capture_operation_id(execution.id), workspace_id
            )
            if integration_snapshot is None:
                raise ResultError(
                    "integration_result_not_ready",
                    {"execution_id": str(execution.id)},
                )
            self._validate_integration_snapshot(
                integration_snapshot,
                execution_id=execution.id,
                request_id=request.id,
                worker_item_id=request.worker_item_id,
                root_task_id=request.root_task_id,
                repository_ref=spec.repository_ref,
                workspace_id=workspace_id,
            )

            source_result_ids = self._source_result_ids(
                uow,
                selection,
                spec,
                workspace_id=workspace_id,
                root_task_id=request.root_task_id,
            )
            if head.current_acceptance_id is not None:
                raise ResultError(
                    "integration_acceptance_exists",
                    {"acceptance_id": str(head.current_acceptance_id)},
                )
            if head.active_operation_id is not None:
                raise ResultError(
                    "result_operation_in_progress",
                    {"operation_id": str(head.active_operation_id)},
                )

            candidate_id = uuid5(
                NAMESPACE_URL, f"sidera.integration-candidate:{operation_id}"
            )
            existing = uow.results.get_record(
                "candidate", candidate_id, workspace_id
            )
            if existing is not None:
                if not isinstance(existing, IntegrationCandidateV1):
                    raise ResultError("integration_candidate_conflict", {})
                expected = self._build_candidate(
                    candidate_id=candidate_id,
                    workspace_id=workspace_id,
                    root_task_id=request.root_task_id,
                    plan_snapshot_id=plan.id,
                    plan_content_hash=current_plan_hash,
                    integration_execution_id=execution.id,
                    base_commit=spec.base_commit,
                    source_result_ids=source_result_ids,
                    input_selection_id=selection.id,
                    input_selection_hash=selection.selection_hash,
                    code_result_id=integration_snapshot.id,
                    previous_candidate_id=existing.previous_candidate_id,
                    created_at=existing.created_at,
                )
                if expected.to_mapping() != existing.to_mapping():
                    raise ResultError(
                        "integration_candidate_conflict",
                        {"candidate_id": str(candidate_id)},
                    )
                return existing

            candidate = self._build_candidate(
                candidate_id=candidate_id,
                workspace_id=workspace_id,
                root_task_id=request.root_task_id,
                plan_snapshot_id=plan.id,
                plan_content_hash=current_plan_hash,
                integration_execution_id=execution.id,
                base_commit=spec.base_commit,
                source_result_ids=source_result_ids,
                input_selection_id=selection.id,
                input_selection_hash=selection.selection_hash,
                code_result_id=integration_snapshot.id,
                previous_candidate_id=head.current_candidate_id,
                created_at=self._now(),
            )
            stored = uow.results.add_immutable("candidate", candidate)
            if not isinstance(stored, IntegrationCandidateV1):
                raise ResultError("integration_candidate_conflict", {})
            next_head = replace(
                head,
                current_candidate_id=stored.id,
                result_version=head.result_version + 1,
            )
            uow.results.save_head(next_head, expected_version=head.result_version)
            event_id = uuid5(
                NAMESPACE_URL,
                f"sidera.integration-candidate-event:{stored.id}",
            )
            command = CommandEnvelope(
                id=operation_id,
                workspace_id=workspace_id,
                command_type="PublishIntegrationCandidate",
                target_type="task",
                actor_type="system",
                actor_id="result-integration",
                target_id=stored.root_task_id,
                payload={
                    "candidate_id": str(stored.id),
                    "integration_execution_id": str(stored.integration_execution_id),
                    "content_hash": stored.content_hash,
                },
                expected_version=head.result_version,
                idempotency_key=f"result-integration:{operation_id}",
                correlation_id=stored.root_task_id,
            )
            if uow.commands.find_by_id(workspace_id, operation_id) is None:
                uow.commands.record_applied(
                    command,
                    hash_request(command),
                    CommandResult(
                        command_id=operation_id,
                        aggregate_id=stored.root_task_id,
                        aggregate_version=next_head.result_version,
                        event_ids=(event_id,),
                        data={"candidate_id": str(stored.id)},
                    ),
                )
            uow.events.append_with_outbox(
                EventData(
                    id=event_id,
                    workspace_id=workspace_id,
                    aggregate_type="integration_candidate",
                    aggregate_id=stored.id,
                    aggregate_version=next_head.result_version,
                    event_type="IntegrationCandidatePrepared",
                    payload={
                        "candidate_id": str(stored.id),
                        "root_task_id": str(stored.root_task_id),
                        "worker_item_id": str(request.worker_item_id),
                        "integration_execution_id": str(
                            stored.integration_execution_id
                        ),
                        "execution_id": str(stored.integration_execution_id),
                        "plan_snapshot_id": str(stored.plan_snapshot_id),
                        "plan_content_hash": stored.plan_content_hash,
                        "input_selection_id": str(stored.input_selection_id),
                        "input_selection_hash": stored.input_selection_hash,
                        "code_result_id": str(stored.code_result_id),
                        "content_hash": stored.content_hash,
                    },
                    actor_type="system",
                    actor_id="result-integration",
                    causation_id=operation_id,
                    correlation_id=stored.root_task_id,
                )
            )
            return stored

    @staticmethod
    def _require_uuid(value: object, label: str) -> None:
        if not isinstance(value, UUID):
            raise ResultError("integration_identity_invalid", {"field": label})

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResultError("integration_clock_invalid", {})
        return value.astimezone(UTC)

    @staticmethod
    def _validate_execution_scope(execution: Any, request: Any, root_task_id: UUID) -> None:
        if (
            execution.workspace_id != request.workspace_id
            or execution.worker_item_id != request.worker_item_id
            or execution.task_id != request.subtask_id
            or request.root_task_id != root_task_id
            or request.execution_lineage_id != execution.lineage_id
        ):
            raise ResultError("integration_execution_scope_changed", {})

    @staticmethod
    def _validate_plan_scope(request: Any, plan: Any, plan_content_hash: str) -> None:
        if (
            request.plan_snapshot_id != plan.id
            or request.plan_generation != plan.generation
            or request.root_task_id != plan.root_task_id
        ):
            raise ResultError("integration_plan_changed", {})
        if request.input_selection_id is None:
            raise ResultError("integration_selection_changed", {})
        if request.input_selection_hash is None:
            raise ResultError("integration_selection_changed", {})

    @staticmethod
    def _load_selection(
        uow: ControlPlaneUnitOfWork, request: Any, workspace_id: UUID
    ) -> CollaborationInputSelectionV1:
        if request.input_selection_id is None:
            raise ResultError("integration_selection_changed", {})
        selection = uow.input_selections.get(workspace_id, request.input_selection_id)
        if selection is None:
            raise ResultError(
                "integration_selection_changed",
                {"selection_id": str(request.input_selection_id)},
            )
        return selection

    @staticmethod
    def _validate_selection_scope(
        selection: CollaborationInputSelectionV1,
        *,
        request: Any,
        plan: Any,
        plan_content_hash: str,
        workspace_id: UUID,
    ) -> None:
        if (
            selection.workspace_id != workspace_id
            or selection.root_task_id != plan.root_task_id
            or selection.plan_snapshot_id != plan.id
            or selection.plan_generation != plan.generation
            or selection.plan_content_hash != plan_content_hash
            or selection.target_execution_request_id != request.id
            or selection.target_worker_item_id != request.worker_item_id
            or request.input_selection_id != selection.id
            or request.input_selection_hash != selection.selection_hash
            or selection.selection_hash != selection.compute_selection_hash()
        ):
            raise ResultError("integration_selection_changed", {})

    @staticmethod
    def _validate_integration_snapshot(
        snapshot: CodeResultSnapshotV1,
        *,
        execution_id: UUID,
        request_id: UUID,
        worker_item_id: UUID,
        root_task_id: UUID,
        repository_ref: str,
        workspace_id: UUID,
    ) -> None:
        if (
            snapshot.workspace_id != workspace_id
            or snapshot.root_task_id != root_task_id
            or snapshot.worker_item_id != worker_item_id
            or snapshot.request_id != request_id
            or snapshot.execution_id != execution_id
            or snapshot.repository_ref != repository_ref
        ):
            raise ResultError("integration_result_scope_changed", {})

    @classmethod
    def _source_result_ids(
        cls,
        uow: ControlPlaneUnitOfWork,
        selection: CollaborationInputSelectionV1,
        spec: IntegrationSpecV1,
        *,
        workspace_id: UUID,
        root_task_id: UUID,
    ) -> tuple[UUID, ...]:
        wanted_groups = set(spec.input_group_keys)
        selected_sources: list[InputSelectionSourceV1] = []
        selected_group_keys: set[str] = set()
        for group in selection.groups:
            if group.group_key in wanted_groups:
                selected_group_keys.add(group.group_key)
                selected_sources.extend(group.sources)
        if selected_group_keys != wanted_groups:
            raise ResultError("integration_selection_changed", {})
        selected_sources.extend(item.source for item in selection.hard_inputs)

        snapshots = {
            item.execution_id: item
            for item in uow.results.retained_code_results(workspace_id)
        }
        result_ids: list[UUID] = []
        seen_execution_ids: set[UUID] = set()
        for source in selected_sources:
            if source.effective_execution_id in seen_execution_ids:
                continue
            seen_execution_ids.add(source.effective_execution_id)
            execution = uow.executions.get(
                source.effective_execution_id, workspace_id
            )
            snapshot = snapshots.get(source.effective_execution_id)
            if execution is None or execution.status is not ExecutionStatus.SUCCEEDED:
                raise ResultError("integration_source_not_ready", {})
            source_request = uow.execution_requests.get_by_execution_lineage(
                execution.lineage_id, workspace_id
            )
            if (
                source_request is None
                or source_request.id != source.effective_request_id
                or execution.lineage_id != source.effective_lineage_id
                or execution.task_id != source_request.subtask_id
            ):
                raise ResultError("integration_source_changed", {})
            if (
                snapshot is None
                or snapshot.id in result_ids
                or snapshot.workspace_id != workspace_id
                or snapshot.root_task_id != root_task_id
                or snapshot.worker_item_id != source.worker_item_id
                or snapshot.request_id != source.effective_request_id
                or snapshot.execution_id != source.effective_execution_id
            ):
                raise ResultError("integration_source_changed", {})
            result_ids.append(snapshot.id)
        if not result_ids:
            raise ResultError("integration_selection_changed", {})
        return tuple(result_ids)

    @staticmethod
    def _build_candidate(
        *,
        candidate_id: UUID,
        workspace_id: UUID,
        root_task_id: UUID,
        plan_snapshot_id: UUID,
        plan_content_hash: str,
        integration_execution_id: UUID,
        base_commit: str,
        source_result_ids: tuple[UUID, ...],
        input_selection_id: UUID,
        input_selection_hash: str,
        code_result_id: UUID,
        previous_candidate_id: UUID | None,
        created_at: datetime,
    ) -> IntegrationCandidateV1:
        content = {
            "schema_version": 1,
            "workspace_id": str(workspace_id),
            "root_task_id": str(root_task_id),
            "plan_snapshot_id": str(plan_snapshot_id),
            "plan_content_hash": plan_content_hash,
            "integration_execution_id": str(integration_execution_id),
            "base_commit": base_commit,
            "source_result_ids": [str(item) for item in source_result_ids],
            "input_selection_id": str(input_selection_id),
            "input_selection_hash": input_selection_hash,
            "code_result_id": str(code_result_id),
            "previous_candidate_id": (
                None if previous_candidate_id is None else str(previous_candidate_id)
            ),
        }
        content_hash = hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return IntegrationCandidateV1(
            id=candidate_id,
            schema_version=1,
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            plan_snapshot_id=plan_snapshot_id,
            plan_content_hash=plan_content_hash,
            integration_execution_id=integration_execution_id,
            base_commit=base_commit,
            source_result_ids=source_result_ids,
            input_selection_id=input_selection_id,
            input_selection_hash=input_selection_hash,
            code_result_id=code_result_id,
            previous_candidate_id=previous_candidate_id,
            content_hash=content_hash,
            created_at=created_at,
        )

__all__ = ["ResultIntegrationService"]
