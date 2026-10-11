from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.code_result_capture import (
    capture_operation_for_execution,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.context_requests import (
    build_execution_context_assembly_request,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_gate import (
    ResultGateAction,
    require_result_gate,
)
from sidera_control_plane.application.result_input_evaluation import (
    build_code_input_manifest,
)
from sidera_control_plane.application.result_input_sources import (
    load_code_result_dependency_closure,
)
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    RuntimeDecision,
    RuntimeIntent,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
    RuntimeRelease,
)
from sidera_control_plane.domain.collaboration_runtime import canonical_json_bytes
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ResultInputBudgetV1,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.result_inputs import (
    ResultInputArtifact,
    ResultInputEvidence,
)
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ReworkRecordV1,
    ReworkStatus,
)
from sidera_control_plane.domain.work_context import WorkContextRevision
from sidera_control_plane.domain.worker_item import WorkerItem

_REINTEGRATION_NAMESPACE = uuid5(
    NAMESPACE_URL, "sidera:result-reintegration:v1"
)


class ResultIntegrationService:
    """Publish immutable integration candidates from one released execution."""

    command_types = frozenset({"ReintegrateTaskResult"})

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def handle(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
    ) -> CommandResult:
        return self.reintegrate(uow, envelope)

    def reintegrate(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
    ) -> CommandResult:
        """Create a fresh integration consumer for an authorized replacement."""

        payload = _reintegrate_payload(envelope.payload)
        source_candidate_id = _payload_uuid(
            payload, "source_candidate_id", code="reintegrate_payload_invalid"
        )
        candidate_hash = _payload_hash(payload, "source_candidate_hash")
        replacement_result_ids = _payload_uuid_array(
            payload, "replacement_result_ids"
        )
        expected_result_version = _payload_positive_int(
            payload, "expected_result_version"
        )
        target_observation_id = _optional_payload_uuid(
            payload, "target_observation_id"
        )
        reason = _payload_text(payload, "reason")

        if envelope.target_id is None:
            raise ResultError("reintegrate_target_required", {})
        if envelope.expected_version is not None and (
            envelope.expected_version != expected_result_version
        ):
            raise ResultError(
                "reintegrate_result_version_mismatch",
                {
                    "expected_result_version": expected_result_version,
                    "command_expected_version": envelope.expected_version,
                },
            )

        require_result_gate(
            uow,
            envelope.workspace_id,
            envelope.target_id,
            ResultGateAction.REINTEGRATE,
        )

        head = uow.results.get_head(
            envelope.workspace_id, envelope.target_id, for_update=True
        )
        if head is None:
            raise ResultError(
                "reintegrate_result_head_not_found",
                {"root_task_id": str(envelope.target_id)},
            )
        if head.result_version != expected_result_version:
            raise ResultError(
                "reintegrate_result_version_conflict",
                {
                    "expected_result_version": expected_result_version,
                    "actual_result_version": head.result_version,
                },
            )
        if head.current_candidate_id != source_candidate_id:
            raise ResultError("reintegrate_candidate_not_current", {})
        if head.current_acceptance_id is not None:
            raise ResultError("reintegrate_acceptance_exists", {})

        candidate = uow.results.get_record(
            "candidate", source_candidate_id, envelope.workspace_id
        )
        if not isinstance(candidate, IntegrationCandidateV1):
            raise ResultError("reintegrate_candidate_not_found", {})
        if candidate.content_hash != candidate_hash:
            raise ResultError("reintegrate_candidate_hash_mismatch", {})
        if candidate.root_task_id != envelope.target_id:
            raise ResultError("reintegrate_candidate_scope_mismatch", {})

        plan_head = uow.plans.get_head(
            candidate.root_task_id, envelope.workspace_id, for_update=True
        )
        if plan_head is None:
            raise ResultError("reintegrate_plan_head_not_found", {})
        plan = uow.plans.get(plan_head.active_snapshot_id, envelope.workspace_id)
        if plan is None:
            raise ResultError("reintegrate_plan_not_found", {})
        if (
            plan.id != candidate.plan_snapshot_id
            or plan.generation != plan_head.active_generation
            or candidate.plan_content_hash != plan_snapshot_content_hash(plan)
        ):
            raise ResultError("reintegrate_plan_changed", {})
        spec = plan.integration_spec
        if not isinstance(spec, IntegrationSpecV1):
            raise ResultError("integration_spec_required", {})

        old_selection = uow.input_selections.get(
            envelope.workspace_id, candidate.input_selection_id
        )
        if old_selection is None:
            raise ResultError("reintegrate_selection_not_found", {})
        old_integration_request = _integration_request_for_candidate(
            uow, candidate, envelope.workspace_id
        )
        _validate_old_selection(
            uow,
            old_selection,
            candidate=candidate,
            request=old_integration_request,
            plan=plan,
            workspace_id=envelope.workspace_id,
        )
        old_release = uow.collaboration_ledger.get_release_for_request(
            envelope.workspace_id, old_integration_request.id
        )
        if old_release is None:
            raise ResultError("reintegrate_release_not_found", {})
        if (
            old_release.input_selection_id != old_selection.id
            or old_release.input_selection_hash != old_selection.selection_hash
        ):
            raise ResultError("reintegrate_release_not_found", {})

        rework_record, replacements = _validate_replacement_results(
            uow,
            candidate=candidate,
            replacement_result_ids=replacement_result_ids,
            repository_ref=spec.repository_ref,
            workspace_id=envelope.workspace_id,
        )
        projection = _reintegrate_projection(uow, old_release, envelope.workspace_id)
        run = uow.collaboration_runs.get(
            envelope.workspace_id, old_release.run_id, for_update=True
        )
        if run is None:
            raise ResultError("reintegrate_run_not_found", {})
        now = self._now()
        request_id = uuid5(
            _REINTEGRATION_NAMESPACE, f"request:{envelope.id}"
        )
        operation_id = uuid5(
            _REINTEGRATION_NAMESPACE, f"operation:{envelope.id}"
        )
        decision_id = uuid5(
            _REINTEGRATION_NAMESPACE, f"decision:{envelope.id}"
        )
        intent_id = uuid5(_REINTEGRATION_NAMESPACE, f"intent:{envelope.id}")
        selection_id = uuid5(
            _REINTEGRATION_NAMESPACE, f"selection:{envelope.id}"
        )
        release_id = uuid5(_REINTEGRATION_NAMESPACE, f"release:{envelope.id}")

        existing_request = uow.execution_requests.get(
            request_id, envelope.workspace_id, for_update=True
        )
        if existing_request is not None:
            raise ResultError(
                "reintegrate_request_conflict",
                {"execution_request_id": str(request_id)},
            )
        request = _build_reintegration_request(
            old_integration_request,
            request_id=request_id,
            candidate=candidate,
            record=rework_record,
            now=now,
        )
        uow.execution_requests.add(request)
        uow.flush()
        for dependency in uow.execution_requests.list_dependencies(
            old_integration_request.id, envelope.workspace_id
        ):
            uow.execution_requests.add_dependency(
                replace(dependency, downstream_request_id=request.id)
            )

        replacements_by_worker = {item[0].worker_item_id: item for item in replacements}
        new_selection = _build_reintegration_selection(
            old_selection,
            selection_id=selection_id,
            request=request,
            candidate=candidate,
            record=rework_record,
            projection=projection,
            decision_id=decision_id,
            intent_id=intent_id,
            replacements_by_worker=replacements_by_worker,
            uow=uow,
            now=now,
        )
        decision = _build_reintegration_decision(
            decision_id=decision_id,
            operation_id=operation_id,
            run_id=old_release.run_id,
            evaluation_id=_evaluation_id_for_projection(
                uow, old_release.run_id, projection.id, envelope.workspace_id
            ),
            projection=projection,
            replacement_result_ids=replacement_result_ids,
            reason=reason,
            created_at=now,
            sequence=uow.collaboration_ledger.next_decision_sequence(
                old_release.run_id, envelope.workspace_id
            ),
        )
        intent = RuntimeIntent(
            id=intent_id,
            workspace_id=envelope.workspace_id,
            run_id=old_release.run_id,
            decision_id=decision.id,
            ordinal=0,
            intent_type="release_execution_request",
            target_execution_request_id=request.id,
            target_request_version=request.version,
            reason_code="result_reintegration",
            summary=reason,
            idempotency_key=f"result-reintegration:{envelope.id}",
            payload_json={
                "intent_type": "release_execution_request",
                "target_execution_request_id": str(request.id),
                "target_request_version": request.version,
                "input_group_selections": [
                    item.to_mapping() for item in new_selection.groups
                ],
                "target_observation_id": (
                    str(target_observation_id)
                    if target_observation_id is not None
                    else None
                ),
            },
            created_at=now,
        )
        uow.collaboration_ledger.add_decision_bundle(decision, (intent,))
        stored_selection = uow.input_selections.add(new_selection)
        release = RuntimeRelease(
            id=release_id,
            workspace_id=envelope.workspace_id,
            run_id=old_release.run_id,
            plan_snapshot_id=plan.id,
            plan_generation=plan.generation,
            plan_content_hash=plan_snapshot_content_hash(plan),
            decision_id=decision.id,
            intent_id=intent.id,
            execution_request_id=request.id,
            request_version_at_release=request.version,
            issued_at=now,
            input_selection_id=stored_selection.id,
            input_selection_hash=stored_selection.selection_hash,
        )
        uow.collaboration_ledger.add_releases((release,))
        uow.flush()
        application = RuntimeIntentApplication(
            id=uuid5(_REINTEGRATION_NAMESPACE, f"application:{envelope.id}"),
            workspace_id=envelope.workspace_id,
            intent_id=intent.id,
            status=RuntimeIntentApplicationStatus.APPLIED,
            expected_run_version=run.resource_version,
            observed_run_version=run.resource_version,
            expected_request_version=request.version,
            observed_request_version=request.version,
            release_id=release.id,
            result_code="result_reintegration",
            result_summary="repaired inputs were released to a fresh integration request",
            resource_version=1,
            created_at=now,
            applied_at=now,
        )
        uow.collaboration_ledger.add_applications((application,))

        previous_request_version = request.version
        request.bind_input_selection(
            selection_id=stored_selection.id,
            selection_hash=stored_selection.selection_hash,
            now=now,
        )
        request.mark_ready(now=now)
        uow.execution_requests.save(request, previous_request_version)
        next_head = replace(
            head,
            current_candidate_id=None,
            result_version=head.result_version + 1,
        )
        uow.results.save_head(next_head, expected_version=head.result_version)
        event_id = uuid5(
            _REINTEGRATION_NAMESPACE, f"event:{envelope.id}"
        )
        uow.events.append_with_outbox(
            EventData(
                id=event_id,
                workspace_id=envelope.workspace_id,
                aggregate_type="task_result",
                aggregate_id=candidate.root_task_id,
                aggregate_version=next_head.result_version,
                event_type="TaskResultReintegrationRequested",
                payload={
                    "root_task_id": str(candidate.root_task_id),
                    "source_candidate_id": str(candidate.id),
                    "source_candidate_hash": candidate.content_hash,
                    "replacement_result_ids": [
                        str(item) for item in replacement_result_ids
                    ],
                    "rework_record_id": str(rework_record.id),
                    "execution_request_id": str(request.id),
                    "integration_operation_id": str(operation_id),
                    "input_selection_id": str(stored_selection.id),
                    "release_id": str(release.id),
                    "target_observation_id": (
                        str(target_observation_id)
                        if target_observation_id is not None
                        else None
                    ),
                    "reason": reason,
                },
                actor_type=envelope.actor_type,
                actor_id=envelope.actor_id,
                causation_id=envelope.id,
                correlation_id=envelope.correlation_id,
            )
        )
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=candidate.root_task_id,
            aggregate_version=next_head.result_version,
            event_ids=(event_id,),
            data={
                "source_candidate_id": str(candidate.id),
                "execution_request_id": str(request.id),
                "input_selection_id": str(stored_selection.id),
                "release_id": str(release.id),
                "integration_operation_id": str(operation_id),
                "rework_record_id": str(rework_record.id),
                "target_observation_id": (
                    str(target_observation_id)
                    if target_observation_id is not None
                    else None
                ),
                "status": "scheduled",
            },
        )

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
            execution = uow.executions.get(execution_id, workspace_id)
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
                execution.lineage_id, workspace_id
            )
            if request is None:
                raise ResultError(
                    "integration_execution_request_not_found",
                    {"execution_id": str(execution_id)},
                )
            require_result_gate(
                uow,
                workspace_id,
                request.root_task_id,
                ResultGateAction.PUBLISH_CANDIDATE,
            )
            execution = uow.executions.get(
                execution_id, workspace_id, for_update=True
            )
            if execution is None:
                raise ResultError(
                    "integration_execution_not_found",
                    {"execution_id": str(execution_id)},
                )
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
                capture_operation_for_execution(
                    uow, workspace_id=execution.workspace_id, execution_id=execution.id
                ),
                workspace_id,
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
            candidate_id = uuid5(
                NAMESPACE_URL, f"sidera.integration-candidate:{operation_id}"
            )
            previous_candidate_id = head.current_candidate_id
            if request.purpose == "reintegrate":
                if request.source_candidate_id is None:
                    raise ResultError("reintegrate_candidate_not_found", {})
                previous_candidate_id = request.source_candidate_id
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
                previous_candidate_id=previous_candidate_id,
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


def build_reintegration_context(
    uow: ControlPlaneUnitOfWork,
    request: ExecutionRequest,
    execution: Any,
    worker_item: WorkerItem,
    work_context_revision: WorkContextRevision,
    *,
    ttl_seconds: int,
    created_at: datetime,
) -> ContextAssemblyRequest:
    """Build a normal V3 code package for a re-integration request."""

    if request.purpose != "reintegrate" or request.result_derivation_kind != "rework":
        raise ResultError("reintegrate_request_invalid", {})
    if request.input_selection_id is None:
        raise ResultError("reintegrate_selection_not_found", {})
    selection = uow.input_selections.get(
        request.workspace_id, request.input_selection_id
    )
    if selection is None or selection.target_execution_request_id != request.id:
        raise ResultError("reintegrate_selection_not_found", {})
    plan = uow.plans.get(request.plan_snapshot_id, request.workspace_id)
    if plan is None or not isinstance(plan.integration_spec, IntegrationSpecV1):
        raise ResultError("integration_spec_required", {})
    source_result_ids = ResultIntegrationService._source_result_ids(
        uow,
        selection,
        plan.integration_spec,
        workspace_id=request.workspace_id,
        root_task_id=request.root_task_id,
    )
    snapshots: list[CodeResultSnapshotV1] = []
    for result_id in source_result_ids:
        snapshot = uow.results.get_record(
            "code_snapshot", result_id, request.workspace_id
        )
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise ResultError("reintegrate_result_not_found", {"result_id": str(result_id)})
        snapshots.append(snapshot)
    try:
        snapshots = list(
            load_code_result_dependency_closure(
                direct_snapshots=tuple(snapshots),
                load_snapshot=lambda result_id: uow.results.get_record(
                    "code_snapshot", result_id, request.workspace_id
                ),
                workspace_id=request.workspace_id,
                root_task_id=request.root_task_id,
                repository_ref=plan.integration_spec.repository_ref,
            )
        )
    except ValueError as exc:
        raise ResultError("reintegrate_result_not_found", {}) from exc
    manifest = build_code_input_manifest(
        selection=selection,
        snapshots=snapshots,
        request_id=request.id,
        execution_id=execution.id,
        repository_ref=plan.integration_spec.repository_ref,
        base_commit=plan.integration_spec.base_commit,
    )
    return build_execution_context_assembly_request(
        execution=execution,
        execution_request=request,
        worker_item=worker_item,
        work_context_revision=work_context_revision,
        ttl_seconds=ttl_seconds,
        created_at=created_at,
        manifest_schema_version=3,
        input_selection=selection,
        result_input_budget=ResultInputBudgetV1(),
        code_input_manifest=manifest,
        idempotency_key=(
            f"result-reintegration-context:{execution.id}:v3:input:{manifest.input_hash}"
        ),
    )


def _reintegrate_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    allowed = {
        "source_candidate_id",
        "source_candidate_hash",
        "replacement_result_ids",
        "expected_result_version",
        "target_observation_id",
        "reason",
    }
    if not isinstance(value, Mapping):
        raise ResultError("reintegrate_payload_invalid", {})
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ResultError("reintegrate_payload_invalid", {"unknown": unknown})
    missing = sorted(
        key
        for key in (
            "source_candidate_id",
            "source_candidate_hash",
            "replacement_result_ids",
            "expected_result_version",
            "target_observation_id",
            "reason",
        )
        if key not in value
    )
    if missing:
        raise ResultError("reintegrate_payload_invalid", {"missing": missing})
    return value


def _payload_uuid(
    payload: Mapping[str, object], key: str, *, code: str
) -> UUID:
    value = payload.get(key)
    try:
        result = value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ResultError(code, {"field": key}) from exc
    return result


def _optional_payload_uuid(payload: Mapping[str, object], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ResultError("reintegrate_payload_invalid", {"field": key}) from exc


def _payload_hash(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) != 64:
        raise ResultError("reintegrate_payload_invalid", {"field": key})
    return value


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResultError("reintegrate_payload_invalid", {"field": key})
    return value


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResultError("reintegrate_payload_invalid", {"field": key})
    return value.strip()


def _payload_uuid_array(payload: Mapping[str, object], key: str) -> tuple[UUID, ...]:
    value = payload.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ResultError("reintegrate_payload_invalid", {"field": key})
    result: list[UUID] = []
    for item in value:
        try:
            parsed = item if isinstance(item, UUID) else UUID(str(item))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ResultError("reintegrate_payload_invalid", {"field": key}) from exc
        if parsed in result:
            raise ResultError("reintegrate_payload_invalid", {"field": key})
        result.append(parsed)
    if not result:
        raise ResultError("reintegrate_payload_invalid", {"field": key})
    return tuple(result)


def _integration_request_for_candidate(
    uow: ControlPlaneUnitOfWork,
    candidate: IntegrationCandidateV1,
    workspace_id: UUID,
) -> ExecutionRequest:
    execution = uow.executions.get(candidate.integration_execution_id, workspace_id)
    if execution is None:
        raise ResultError("reintegrate_integration_execution_not_found", {})
    request = uow.execution_requests.get_by_execution_lineage(
        execution.lineage_id, workspace_id, for_update=True
    )
    if request is None:
        raise ResultError("reintegrate_integration_request_not_found", {})
    return request


def _validate_old_selection(
    uow: ControlPlaneUnitOfWork,
    selection: CollaborationInputSelectionV1,
    *,
    candidate: IntegrationCandidateV1,
    request: ExecutionRequest,
    plan: Any,
    workspace_id: UUID,
) -> None:
    if not isinstance(plan.integration_spec, IntegrationSpecV1):
        raise ResultError("integration_spec_required", {})
    ResultIntegrationService._validate_selection_scope(
        selection,
        request=request,
        plan=plan,
        plan_content_hash=plan_snapshot_content_hash(plan),
        workspace_id=workspace_id,
    )
    source_ids = ResultIntegrationService._source_result_ids(
        uow,
        selection,
        plan.integration_spec,
        workspace_id=workspace_id,
        root_task_id=candidate.root_task_id,
    )
    if (
        tuple(source_ids) != candidate.source_result_ids
        or selection.id != candidate.input_selection_id
        or selection.selection_hash != candidate.input_selection_hash
    ):
        raise ResultError("reintegrate_selection_changed", {})


def _validate_replacement_results(
    uow: ControlPlaneUnitOfWork,
    *,
    candidate: IntegrationCandidateV1,
    replacement_result_ids: tuple[UUID, ...],
    repository_ref: str,
    workspace_id: UUID,
) -> tuple[ReworkRecordV1, tuple[tuple[CodeResultSnapshotV1, ExecutionRequest, Any], ...]]:
    # A candidate's source results are the inputs consumed by integration,
    # while code_result_id is the integration Worker's own current result.
    # Both are replaceable when a rework targets the corresponding WorkerItem.
    candidate_result_ids = tuple(
        dict.fromkeys((*candidate.source_result_ids, candidate.code_result_id))
    )
    record_by_id = {
        item: uow.results.get_record("code_snapshot", item, workspace_id)
        for item in candidate_result_ids
    }
    old_by_worker = {
        item.worker_item_id: item
        for item in record_by_id.values()
        if isinstance(item, CodeResultSnapshotV1)
    }
    if not old_by_worker:
        raise ResultError("reintegrate_candidate_sources_missing", {})

    replacements: list[tuple[CodeResultSnapshotV1, ExecutionRequest, Any]] = []
    seen_workers: set[UUID] = set()
    replacement_record: ReworkRecordV1 | None = None
    for result_id in replacement_result_ids:
        snapshot = uow.results.get_record("code_snapshot", result_id, workspace_id)
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise ResultError(
                "reintegrate_result_not_authorized", {"result_id": str(result_id)}
            )
        old = old_by_worker.get(snapshot.worker_item_id)
        if old is None:
            raise ResultError(
                "reintegrate_result_not_authorized", {"result_id": str(result_id)}
            )
        if snapshot.execution_id == old.execution_id:
            if snapshot.content_hash != old.content_hash:
                raise ResultError("result_identity_conflict", {})
            raise ResultError(
                "reintegrate_result_not_authorized", {"result_id": str(result_id)}
            )
        if snapshot.worker_item_id in seen_workers:
            raise ResultError("reintegrate_result_not_authorized", {})
        execution = uow.executions.get(snapshot.execution_id, workspace_id)
        request = (
            None
            if execution is None
            else uow.execution_requests.get_by_execution_lineage(
                execution.lineage_id, workspace_id
            )
        )
        record = (
            None
            if request is None or request.rework_record_id is None
            else uow.results.get_record("rework", request.rework_record_id, workspace_id)
        )
        if (
            execution is None
            or execution.status is not ExecutionStatus.SUCCEEDED
            or request is None
            or request.result_derivation_kind != "rework"
            or request.source_candidate_id != candidate.id
            or not isinstance(record, ReworkRecordV1)
            or record.status not in {ReworkStatus.REQUESTED, ReworkStatus.RUNNING}
            or record.candidate_id != candidate.id
            or record.id != request.rework_record_id
            or snapshot.request_id != request.id
            or snapshot.root_task_id != candidate.root_task_id
            or snapshot.workspace_id != workspace_id
            or snapshot.repository_ref != repository_ref
            or execution.worker_item_id != snapshot.worker_item_id
            or request.worker_item_id != snapshot.worker_item_id
            or snapshot.worker_item_id not in record.target_worker_item_ids
        ):
            raise ResultError(
                "reintegrate_result_not_authorized", {"result_id": str(result_id)}
            )
        if replacement_record is None:
            replacement_record = record
        elif replacement_record.id != record.id:
            raise ResultError("reintegrate_result_not_authorized", {})
        seen_workers.add(snapshot.worker_item_id)
        replacements.append((snapshot, request, execution))
    if replacement_record is None:
        raise ResultError("reintegrate_result_not_authorized", {})
    return replacement_record, tuple(replacements)


def _reintegrate_projection(
    uow: ControlPlaneUnitOfWork,
    release: RuntimeRelease,
    workspace_id: UUID,
) -> Any:
    run = uow.collaboration_runs.get(workspace_id, release.run_id)
    if run is None or run.last_projection_snapshot_id is None:
        raise ResultError("reintegrate_projection_not_found", {})
    projection = uow.collaboration_ledger.get_projection(
        workspace_id, run.last_projection_snapshot_id
    )
    if projection is None:
        raise ResultError("reintegrate_projection_not_found", {})
    return projection


def _evaluation_id_for_projection(
    uow: ControlPlaneUnitOfWork,
    run_id: UUID,
    projection_id: UUID,
    workspace_id: UUID,
) -> UUID:
    evaluation = uow.collaboration_evaluations.get_for_projection(
        workspace_id, projection_id
    )
    if evaluation is None or evaluation.run_id != run_id:
        raise ResultError("reintegrate_evaluation_not_found", {})
    return evaluation.id


def _build_reintegration_request(
    source: ExecutionRequest,
    *,
    request_id: UUID,
    candidate: IntegrationCandidateV1,
    record: ReworkRecordV1,
    now: datetime,
) -> ExecutionRequest:
    return ExecutionRequest.create(
        request_id=request_id,
        workspace_id=source.workspace_id,
        planning_request_id=source.planning_request_id,
        proposal_revision_id=source.proposal_revision_id,
        plan_snapshot_id=source.plan_snapshot_id,
        plan_generation=source.plan_generation,
        root_task_id=source.root_task_id,
        subtask_id=source.subtask_id,
        worker_item_id=source.worker_item_id,
        worker_item_key=source.worker_item_key,
        intent_key=f"reintegrate:{record.id}",
        purpose="reintegrate",
        capability_requirements=source.capability_requirements,
        adapter_preferences=source.adapter_preferences,
        adapter_forbidden=source.adapter_forbidden,
        budget_class=source.budget_class,
        not_before=now,
        max_attempts=source.max_attempts,
        expected_artifact_refs=source.expected_artifact_refs,
        expected_evidence_refs=source.expected_evidence_refs,
        expected_outputs=source.expected_outputs,
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
        result_derivation_kind="rework",
        source_candidate_id=candidate.id,
        rework_record_id=record.id,
        now=now,
    )


def _build_reintegration_selection(
    old: CollaborationInputSelectionV1,
    *,
    selection_id: UUID,
    request: ExecutionRequest,
    candidate: IntegrationCandidateV1,
    record: ReworkRecordV1,
    projection: Any,
    decision_id: UUID,
    intent_id: UUID,
    replacements_by_worker: Mapping[UUID, tuple[CodeResultSnapshotV1, ExecutionRequest, Any]],
    uow: ControlPlaneUnitOfWork,
    now: datetime,
) -> CollaborationInputSelectionV1:
    groups = tuple(
        replace(
            group,
            sources=tuple(
                _replace_selection_source(
                    source,
                    candidate=candidate,
                    record=record,
                    replacement=replacements_by_worker.get(source.worker_item_id),
                    uow=uow,
                )
                for source in group.sources
            ),
        )
        for group in old.groups
    )
    hard_inputs = tuple(
        replace(
            item,
            source=_replace_selection_source(
                item.source,
                candidate=candidate,
                record=record,
                replacement=replacements_by_worker.get(item.source.worker_item_id),
                uow=uow,
            ),
        )
        for item in old.hard_inputs
    )
    draft = replace(
        old,
        id=selection_id,
        target_execution_request_id=request.id,
        target_request_version=request.version,
        projection_snapshot_id=projection.id,
        projection_hash=projection.content_hash,
        decision_id=decision_id,
        intent_id=intent_id,
        groups=groups,
        hard_inputs=hard_inputs,
        selection_hash="0" * 64,
        created_at=now,
    )
    return replace(draft, selection_hash=draft.compute_selection_hash())


def _replace_selection_source(
    source: InputSelectionSourceV1,
    *,
    candidate: IntegrationCandidateV1,
    record: ReworkRecordV1,
    replacement: tuple[CodeResultSnapshotV1, ExecutionRequest, Any] | None,
    uow: ControlPlaneUnitOfWork,
) -> InputSelectionSourceV1:
    if replacement is None:
        return source
    snapshot, request, execution = replacement
    artifacts, evidence = _selection_artifacts(uow, snapshot)
    basis = sha256(
        canonical_json_bytes(
            {
                "previous_source_basis_hash": source.source_basis_hash,
                "candidate_id": str(candidate.id),
                "rework_record_id": str(record.id),
                "result_id": str(snapshot.id),
                "result_hash": snapshot.content_hash,
                "request_id": str(request.id),
                "execution_id": str(execution.id),
            }
        )
    ).hexdigest()
    return replace(
        source,
        effective_request_id=request.id,
        allowed_lineage_ids=(execution.lineage_id,),
        effective_lineage_id=execution.lineage_id,
        effective_execution_id=execution.id,
        attempt=execution.attempt,
        inheritance_refs=(
            f"rework:{candidate.id}:{source.effective_execution_id}:{execution.id}",
        ),
        source_basis_hash=basis,
        artifacts=artifacts,
        evidence=evidence,
    )


def _selection_artifacts(
    uow: ControlPlaneUnitOfWork,
    snapshot: CodeResultSnapshotV1,
) -> tuple[tuple[ResultInputArtifact, ...], tuple[ResultInputEvidence, ...]]:
    artifacts = tuple(
        ResultInputArtifact(
            artifact_id=item.id,
            artifact_type=item.artifact_type,
            content_hash=item.content_hash,
            size_bytes=item.size_bytes,
            media_type=item.media_type,
        )
        for item in uow.artifacts.list_for_execution(
            snapshot.execution_id, snapshot.workspace_id
        )
        if item.id in snapshot.artifact_ids
    )
    artifact_ids = {item.artifact_id for item in artifacts}
    evidence = tuple(
        ResultInputEvidence(
            evidence_id=item.id,
            acceptance_criterion_ref=item.acceptance_criterion_ref,
            artifact_ids=item.artifact_ids,
        )
        for item in uow.evidence_claims.list_for_executions(
            (snapshot.execution_id,), snapshot.workspace_id
        )
        if set(item.artifact_ids).issubset(artifact_ids)
    )
    return artifacts, evidence


def _build_reintegration_decision(
    *,
    decision_id: UUID,
    operation_id: UUID,
    run_id: UUID,
    evaluation_id: UUID,
    projection: Any,
    replacement_result_ids: Sequence[UUID],
    reason: str,
    created_at: datetime,
    sequence: int,
) -> RuntimeDecision:
    strategy_state = {
        "kind": "result_reintegration",
        "replacement_result_ids": [str(item) for item in replacement_result_ids],
    }
    content_hash = sha256(
        canonical_json_bytes(
            {
                "decision_id": str(decision_id),
                "operation_id": str(operation_id),
                "run_id": str(run_id),
                "projection_id": str(projection.id),
                "projection_hash": projection.content_hash,
                "sequence": sequence,
                "strategy_state": strategy_state,
                "reason": reason,
            }
        )
    ).hexdigest()
    return RuntimeDecision(
        id=decision_id,
        workspace_id=projection.workspace_id,
        run_id=run_id,
        evaluation_id=evaluation_id,
        projection_snapshot_id=projection.id,
        projection_hash=projection.content_hash,
        operation_id=operation_id,
        schema_version=1,
        sequence=sequence,
        outcome="intents",
        reason_code="result_reintegration",
        summary=reason,
        strategy_state=strategy_state,
        content_hash=content_hash,
        runtime_duration_ms=0,
        created_at=created_at,
    )

__all__ = ["ResultIntegrationService", "build_reintegration_context"]
