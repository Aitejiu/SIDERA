from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.context_requests import (
    build_execution_context_assembly_request,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_gate import (
    ResultGateAction,
    require_result_gate,
)
from sidera_control_plane.application.result_input_sources import (
    build_rework_code_input_manifest,
    load_code_result_dependency_closure,
)
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.collaboration import CollaborationGateMode
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import ContextAssemblyRequest
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ReworkImpactV1,
    ReworkMode,
    ReworkRecordV1,
    ReworkStatus,
    ReworkSuggestionV1,
    TaskResultHead,
    extend_rework_budget,
    reserve_rework_round,
)
from sidera_control_plane.domain.work_context import WorkContextRevision
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus

type ResultProvenance = Mapping[UUID, CodeResultSnapshotV1]
type HardResultEdge = tuple[UUID, UUID]

_REWORK_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:result-rework:v1")


def require_authorized_source_candidate(
    uow: ControlPlaneUnitOfWork,
    request: ExecutionRequest,
) -> IntegrationCandidateV1:
    """Require a rework request to remain bound to its current candidate."""

    if request.result_derivation_kind != "rework" or request.source_candidate_id is None:
        raise ResultError("rework_source_candidate_required", {})
    if request.rework_record_id is None:
        raise ResultError("rework_record_required", {})
    candidate = uow.results.get_record(
        "candidate", request.source_candidate_id, request.workspace_id
    )
    record = uow.results.get_record(
        "rework", request.rework_record_id, request.workspace_id
    )
    if not isinstance(candidate, IntegrationCandidateV1):
        raise ResultError("rework_candidate_not_found", {})
    if not isinstance(record, ReworkRecordV1):
        raise ResultError("rework_record_not_found", {})
    if record.status not in {ReworkStatus.REQUESTED, ReworkStatus.RUNNING}:
        raise ResultError("rework_record_not_active", {})
    if (
        record.candidate_id != candidate.id
        or request.worker_item_id not in record.target_worker_item_ids
        or candidate.workspace_id != request.workspace_id
        or candidate.root_task_id != request.root_task_id
        or candidate.plan_snapshot_id != request.plan_snapshot_id
    ):
        raise ResultError("rework_source_candidate_scope_mismatch", {})
    head = uow.results.get_head(request.workspace_id, request.root_task_id)
    if (
        head is None
        or head.current_candidate_id != candidate.id
        or head.current_acceptance_id is not None
        or head.active_operation_id is not None
    ):
        raise ResultError("rework_source_candidate_not_current", {})
    plan = uow.plans.get_current_for_root(request.root_task_id, request.workspace_id)
    if (
        plan is None
        or plan.id != candidate.plan_snapshot_id
        or plan.generation != request.plan_generation
        or candidate.plan_content_hash != plan_snapshot_content_hash(plan)
    ):
        raise ResultError("rework_plan_stale", {})
    return candidate


def build_rework_context(
    uow: ControlPlaneUnitOfWork,
    request: ExecutionRequest,
    source_candidate: IntegrationCandidateV1,
    *,
    execution: Execution | None = None,
    worker_item: WorkerItem | None = None,
    work_context_revision: WorkContextRevision | None = None,
    ttl_seconds: int = 7_200,
    created_at: datetime | None = None,
) -> ContextAssemblyRequest:
    """Build a fresh V3 assembly request from candidate provenance."""

    authorized = require_authorized_source_candidate(uow, request)
    if authorized.id != source_candidate.id:
        raise ResultError("rework_source_candidate_scope_mismatch", {})
    if execution is None:
        if request.first_execution_id is None:
            raise ResultError("rework_execution_required", {})
        execution = uow.executions.get(request.first_execution_id, request.workspace_id)
    if execution is None or execution.worker_item_id != request.worker_item_id:
        raise ResultError("rework_execution_scope_mismatch", {})
    if worker_item is None:
        worker_item = uow.worker_items.get(request.worker_item_id, request.workspace_id)
    if worker_item is None or worker_item.status is WorkerItemStatus.RETIRED:
        raise ResultError("rework_worker_retired", {})
    if work_context_revision is None:
        work_context = uow.work_contexts.get_by_planning_request(
            request.planning_request_id, request.workspace_id
        )
        if work_context is None:
            work_context = uow.work_contexts.get_by_root_task(
                request.root_task_id, request.workspace_id
            )
        if work_context is None:
            raise ResultError("rework_work_context_not_found", {})
        work_context_revision = uow.work_contexts.get_revision(
            work_context.current_revision_id, request.workspace_id
        )
    if work_context_revision is None:
        raise ResultError("rework_work_context_revision_not_found", {})

    rework_record_id = request.rework_record_id
    if rework_record_id is None:
        raise ResultError("rework_record_required", {})

    plan = uow.plans.get(request.plan_snapshot_id, request.workspace_id)
    if plan is None or plan.integration_spec is None:
        raise ResultError("integration_spec_required", {})
    snapshots: list[CodeResultSnapshotV1] = []
    for result_id in source_candidate.source_result_ids:
        snapshot = uow.results.get_record("code_snapshot", result_id, request.workspace_id)
        if not isinstance(snapshot, CodeResultSnapshotV1):
            raise ResultError("rework_source_result_not_found", {"result_id": str(result_id)})
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
        code_manifest = build_rework_code_input_manifest(
            candidate=source_candidate,
            snapshots=snapshots,
            request_id=request.id,
            execution_id=execution.id,
            repository_ref=plan.integration_spec.repository_ref,
        )
    except ValueError as exc:
        raise ResultError("rework_source_result_invalid", {}) from exc
    record = uow.results.get_record("rework", rework_record_id, request.workspace_id)
    if not isinstance(record, ReworkRecordV1):
        raise ResultError("rework_record_not_found", {})
    return build_execution_context_assembly_request(
        execution=execution,
        execution_request=request,
        worker_item=worker_item,
        work_context_revision=work_context_revision,
        ttl_seconds=ttl_seconds,
        created_at=created_at or datetime.now(UTC),
        manifest_schema_version=3,
        additional_fact_scope_refs=(
            "result_rework",
            "source_candidate",
            "rework_record",
        ),
        additional_budget={
            "result_derivation_kind": "rework",
            "source_candidate_id": str(source_candidate.id),
            "source_candidate_hash": source_candidate.content_hash,
            "rework_record_id": str(record.id),
            "rework_reason": record.reason,
            "rework_issue_keys": list(record.source_issue_keys),
        },
        code_input_manifest=code_manifest,
    )


class ResultReworkService:
    """Authorize bounded rework without mutating old result facts."""

    command_types = frozenset({"RequestTaskRework", "AuthorizeAdditionalRework"})

    def __init__(
        self,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        *,
        local_actor_type: str = "local",
        local_actor_id: str = "operator",
    ) -> None:
        self._clock = clock
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        return self.request(uow, envelope)

    def request(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type == "AuthorizeAdditionalRework":
            return self._authorize_additional_rework(uow, envelope)
        if envelope.command_type != "RequestTaskRework":
            raise ResultError("rework_command_unsupported", {})

        head, candidate, plan, spec = self._load_scope(uow, envelope)
        mode = _mode(envelope.payload)
        _validate_actor_for_mode(envelope, mode)
        issue_keys, target_ids, reason, suggestion = _rework_inputs(
            envelope.payload,
            candidate=candidate,
        )
        worker_plan = {
            item.worker_item_id: item
            for item in plan.worker_item_plans
            if item.worker_item_id is not None
        }
        _validate_worker_scope(
            target_ids,
            impact_ids=_uuid_tuple(
                envelope.payload.get("impact_worker_item_ids", target_ids),
                "impact_worker_item_ids",
            ),
            worker_plan=worker_plan,
            allowed_keys=frozenset(spec.rework_policy.allowed_worker_item_keys),
        )

        if not target_ids:
            return self._record_attention(
                uow,
                envelope,
                head=head,
                candidate=candidate,
                mode=mode,
                issue_keys=issue_keys,
                reason="rework_owner_required",
            )

        impact_ids = _uuid_tuple(
            envelope.payload.get("impact_worker_item_ids", target_ids),
            "impact_worker_item_ids",
        )
        self._validate_rework_targets(
            uow,
            candidate=candidate,
            plan=plan,
            target_ids=target_ids,
        )
        now = self._now()
        next_head = head
        round_number: int | None = None
        if mode is ReworkMode.AUTOMATIC:
            next_head = reserve_rework_round(head, now)
            round_number = next_head.auto_rounds_used

        record = self._record(
            envelope,
            candidate=candidate,
            mode=mode,
            round_number=round_number,
            issue_keys=issue_keys,
            target_ids=target_ids,
            impact_ids=impact_ids,
            reason=reason,
            status=ReworkStatus.REQUESTED,
        )
        stored = uow.results.add_immutable("rework", record)
        if not isinstance(stored, ReworkRecordV1):
            raise ResultError("rework_record_conflict", {"record_id": str(record.id)})
        request_events = self._create_rework_requests(
            uow,
            envelope,
            candidate=candidate,
            plan=plan,
            record=stored,
            now=now,
        )
        if next_head != head:
            uow.results.save_head(next_head, expected_version=head.result_version)
        event = _rework_event(
            envelope,
            record=stored,
            aggregate_version=next_head.result_version,
            candidate_hash=candidate.content_hash,
            suggestion=suggestion,
        )
        uow.events.append_with_outbox(event)
        for request_event in request_events:
            uow.events.append_with_outbox(request_event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=head.root_task_id,
            aggregate_version=next_head.result_version,
            event_ids=(event.id, *(item.id for item in request_events)),
            data={
                "rework_record_id": str(stored.id),
                "request_ids": [str(item) for item in stored.request_ids],
                "target_worker_item_ids": [str(item) for item in stored.target_worker_item_ids],
                "impact_worker_item_ids": [str(item) for item in stored.impact_worker_item_ids],
                "status": stored.status.value,
                "round_number": stored.round_number,
            },
        )

    @staticmethod
    def _validate_rework_targets(
        uow: ControlPlaneUnitOfWork,
        *,
        candidate: IntegrationCandidateV1,
        plan: Any,
        target_ids: tuple[UUID, ...],
    ) -> None:
        worker_plan = {
            item.worker_item_id: item
            for item in plan.worker_item_plans
            if item.worker_item_id is not None
        }
        integration_execution = uow.executions.get(
            candidate.integration_execution_id, candidate.workspace_id
        )
        if integration_execution is None:
            raise ResultError("rework_source_execution_not_found", {})
        integration_request = uow.execution_requests.get_by_execution_lineage(
            integration_execution.lineage_id,
            candidate.workspace_id,
        )
        if integration_request is None:
            raise ResultError("rework_source_request_not_found", {})
        for worker_id in target_ids:
            plan_item = worker_plan[worker_id]
            worker_item = uow.worker_items.get(
                worker_id, candidate.workspace_id, for_update=True
            )
            if worker_item is None or worker_item.status is WorkerItemStatus.RETIRED:
                raise ResultError(
                    "rework_worker_retired", {"worker_item_id": str(worker_id)}
                )
            source_request = uow.execution_requests.get_by_proposal_worker_item(
                integration_request.proposal_revision_id,
                plan_item.key,
                candidate.workspace_id,
            )
            if (
                source_request is None
                or source_request.root_task_id != candidate.root_task_id
                or source_request.plan_snapshot_id != candidate.plan_snapshot_id
                or source_request.worker_item_id != worker_id
                or source_request.result_derivation_kind is not None
            ):
                raise ResultError(
                    "rework_source_request_not_found",
                    {"worker_item_id": str(worker_id)},
                )

    def _create_rework_requests(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        *,
        candidate: IntegrationCandidateV1,
        plan: Any,
        record: ReworkRecordV1,
        now: datetime,
    ) -> tuple[EventData, ...]:
        worker_plan = {
            item.worker_item_id: item
            for item in plan.worker_item_plans
            if item.worker_item_id is not None
        }
        integration_execution = uow.executions.get(
            candidate.integration_execution_id, candidate.workspace_id
        )
        if integration_execution is None:
            raise ResultError("rework_source_execution_not_found", {})
        integration_request = uow.execution_requests.get_by_execution_lineage(
            integration_execution.lineage_id,
            candidate.workspace_id,
        )
        if integration_request is None:
            raise ResultError("rework_source_request_not_found", {})
        events: list[EventData] = []
        for request_id, worker_id in zip(
            record.request_ids,
            record.target_worker_item_ids,
            strict=True,
        ):
            plan_item = worker_plan[worker_id]
            source_request = uow.execution_requests.get_by_proposal_worker_item(
                integration_request.proposal_revision_id,
                plan_item.key,
                candidate.workspace_id,
            )
            if source_request is None:
                raise ResultError(
                    "rework_source_request_not_found",
                    {"worker_item_id": str(worker_id)},
                )
            existing = uow.execution_requests.get(request_id, candidate.workspace_id)
            if existing is not None:
                if (
                    existing.result_derivation_kind != "rework"
                    or existing.source_candidate_id != candidate.id
                    or existing.rework_record_id != record.id
                    or existing.worker_item_id != worker_id
                ):
                    raise ResultError(
                        "rework_execution_request_conflict",
                        {"execution_request_id": str(request_id)},
                    )
                continue
            request = ExecutionRequest.create(
                request_id=request_id,
                workspace_id=candidate.workspace_id,
                planning_request_id=source_request.planning_request_id,
                proposal_revision_id=source_request.proposal_revision_id,
                plan_snapshot_id=candidate.plan_snapshot_id,
                plan_generation=plan.generation,
                root_task_id=candidate.root_task_id,
                subtask_id=source_request.subtask_id,
                worker_item_id=worker_id,
                worker_item_key=plan_item.key,
                intent_key=f"rework:{record.id}:{plan_item.key}",
                purpose="rework",
                capability_requirements=source_request.capability_requirements,
                adapter_preferences=source_request.adapter_preferences,
                adapter_forbidden=source_request.adapter_forbidden,
                budget_class=source_request.budget_class,
                not_before=now,
                max_attempts=source_request.max_attempts,
                expected_artifact_refs=source_request.expected_artifact_refs,
                expected_evidence_refs=source_request.expected_evidence_refs,
                expected_outputs=source_request.expected_outputs,
                now=now,
                collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
                result_derivation_kind="rework",
                source_candidate_id=candidate.id,
                rework_record_id=record.id,
            )
            request.mark_ready(now=now)
            uow.execution_requests.add(request)
            events.append(
                EventData(
                    id=uuid5(
                        _REWORK_NAMESPACE,
                        f"execution-request:{record.id}:{worker_id}",
                    ),
                    workspace_id=candidate.workspace_id,
                    aggregate_type="execution_request",
                    aggregate_id=request.id,
                    aggregate_version=request.version,
                    event_type="ExecutionRequestReworkCreated",
                    payload={
                        "execution_request_id": str(request.id),
                        "root_task_id": str(request.root_task_id),
                        "worker_item_id": str(request.worker_item_id),
                        "worker_item_key": request.worker_item_key,
                        "planning_request_id": str(request.planning_request_id),
                        "proposal_revision_id": str(request.proposal_revision_id),
                        "plan_snapshot_id": str(request.plan_snapshot_id),
                        "plan_generation": request.plan_generation,
                        "result_derivation_kind": request.result_derivation_kind,
                        "source_candidate_id": str(candidate.id),
                        "rework_record_id": str(record.id),
                        "status": request.status.value,
                    },
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    causation_id=envelope.id,
                    correlation_id=envelope.correlation_id,
                )
            )
        uow.flush()
        return tuple(events)

    def _authorize_additional_rework(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if (
            envelope.actor_type != self._local_actor_type
            or envelope.actor_id != self._local_actor_id
        ):
            raise ResultError("rework_authorization_actor_forbidden", {})
        head = self._load_head(uow, envelope)
        additional_rounds = _non_negative_int(
            envelope.payload.get("additional_rounds"), "additional_rounds"
        )
        additional_seconds = _non_negative_int(
            envelope.payload.get("additional_seconds"), "additional_seconds"
        )
        reason = _required_text(envelope.payload.get("reason"), "reason")
        next_head = extend_rework_budget(
            head,
            additional_rounds=additional_rounds,
            additional_seconds=additional_seconds,
        )
        uow.results.save_head(next_head, expected_version=head.result_version)
        event = EventData(
            id=uuid5(_REWORK_NAMESPACE, f"budget:{envelope.id}"),
            workspace_id=envelope.workspace_id,
            aggregate_type="result_rework",
            aggregate_id=head.root_task_id,
            aggregate_version=next_head.result_version,
            event_type="ResultReworkBudgetAuthorized",
            payload={
                "root_task_id": str(head.root_task_id),
                "additional_rounds": additional_rounds,
                "additional_seconds": additional_seconds,
                "auto_rounds_limit": next_head.auto_rounds_limit,
                "auto_seconds_limit": next_head.auto_seconds_limit,
                "reason": reason,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=head.root_task_id,
            aggregate_version=next_head.result_version,
            event_ids=(event.id,),
            data={
                "auto_rounds_used": next_head.auto_rounds_used,
                "auto_rounds_limit": next_head.auto_rounds_limit,
                "auto_seconds_limit": next_head.auto_seconds_limit,
                "auto_deadline": (
                    None
                    if next_head.auto_deadline is None
                    else next_head.auto_deadline.isoformat()
                ),
            },
        )

    def _load_scope(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> tuple[TaskResultHead, IntegrationCandidateV1, Any, IntegrationSpecV1]:
        if envelope.target_id is not None:
            require_result_gate(
                uow,
                envelope.workspace_id,
                envelope.target_id,
                ResultGateAction.REWORK,
            )
        head = self._load_head(uow, envelope)
        candidate_id = _payload_uuid(envelope.payload, "candidate_id")
        candidate = uow.results.get_record("candidate", candidate_id, envelope.workspace_id)
        if not isinstance(candidate, IntegrationCandidateV1):
            raise ResultError("rework_candidate_not_found", {"candidate_id": str(candidate_id)})
        if candidate.root_task_id != head.root_task_id:
            raise ResultError("rework_candidate_scope_mismatch", {})
        candidate_hash = envelope.payload.get("candidate_hash")
        if candidate_hash != candidate.content_hash:
            raise ResultError("rework_candidate_hash_mismatch", {})
        if head.current_candidate_id != candidate.id:
            raise ResultError(
                "rework_candidate_not_current", {"candidate_id": str(candidate.id)}
            )
        if head.current_acceptance_id is not None:
            raise ResultError("rework_acceptance_exists", {})

        plan_head = uow.plans.get_head(head.root_task_id, envelope.workspace_id, for_update=True)
        if plan_head is None:
            raise ResultError("rework_plan_not_found", {})
        plan = uow.plans.get(plan_head.active_snapshot_id, envelope.workspace_id)
        if plan is None or plan.id != candidate.plan_snapshot_id:
            raise ResultError("rework_plan_stale", {})
        if candidate.plan_content_hash != plan_snapshot_content_hash(plan):
            raise ResultError("rework_plan_hash_mismatch", {})
        spec = plan.integration_spec
        if not isinstance(spec, IntegrationSpecV1):
            raise ResultError("integration_spec_required", {})
        return head, candidate, plan, spec

    @staticmethod
    def _load_head(
        uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> TaskResultHead:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ResultError("rework_target_required", {})
        head = uow.results.get_head(
            envelope.workspace_id, envelope.target_id, for_update=True
        )
        if head is None:
            raise ResultError("rework_result_head_not_found", {})
        if envelope.expected_version != head.result_version:
            raise ResultError(
                "rework_result_version_conflict",
                {"expected_version": envelope.expected_version, "actual_version": head.result_version},
            )
        return head

    def _record_attention(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        *,
        head: TaskResultHead,
        candidate: IntegrationCandidateV1,
        mode: ReworkMode,
        issue_keys: tuple[str, ...],
        reason: str,
    ) -> CommandResult:
        record = self._record(
            envelope,
            candidate=candidate,
            mode=mode,
            round_number=None,
            issue_keys=issue_keys,
            target_ids=(),
            impact_ids=(),
            reason=reason,
            status=ReworkStatus.BLOCKED,
        )
        stored = uow.results.add_immutable("rework", record)
        if not isinstance(stored, ReworkRecordV1):
            raise ResultError("rework_record_conflict", {"record_id": str(record.id)})
        event = _rework_event(
            envelope,
            record=stored,
            aggregate_version=head.result_version,
            candidate_hash=candidate.content_hash,
            suggestion=None,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=head.root_task_id,
            aggregate_version=head.result_version,
            event_ids=(event.id,),
            data={
                "rework_record_id": str(stored.id),
                "request_ids": [],
                "status": stored.status.value,
                "attention_required": True,
                "reason": reason,
            },
        )

    def _record(
        self,
        envelope: CommandEnvelope,
        *,
        candidate: IntegrationCandidateV1,
        mode: ReworkMode,
        round_number: int | None,
        issue_keys: tuple[str, ...],
        target_ids: tuple[UUID, ...],
        impact_ids: tuple[UUID, ...],
        reason: str,
        status: ReworkStatus,
    ) -> ReworkRecordV1:
        record_id = uuid5(_REWORK_NAMESPACE, f"record:{envelope.id}")
        request_ids = tuple(
            uuid5(_REWORK_NAMESPACE, f"request:{record_id}:{worker_id}")
            for worker_id in sorted(target_ids, key=str)
        )
        return ReworkRecordV1(
            id=record_id,
            workspace_id=envelope.workspace_id,
            root_task_id=candidate.root_task_id,
            candidate_id=candidate.id,
            source_issue_keys=tuple(sorted(issue_keys)),
            target_worker_item_ids=tuple(sorted(target_ids, key=str)),
            impact_worker_item_ids=tuple(sorted(impact_ids, key=str)),
            reason=reason,
            mode=mode,
            round_number=round_number,
            source_command_id=envelope.id,
            request_ids=request_ids,
            status=status,
            created_at=self._now(),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("rework clock must return an aware datetime")
        return value.astimezone(UTC)


def _payload_uuid(payload: Mapping[str, object], name: str) -> UUID:
    value = payload.get(name)
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ResultError("rework_payload_invalid", {"field": name})
    try:
        return UUID(value)
    except ValueError as exc:
        raise ResultError("rework_payload_invalid", {"field": name}) from exc


def _uuid_tuple(value: object, label: str) -> tuple[UUID, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ResultError("rework_payload_invalid", {"field": label})
    result: list[UUID] = []
    for item in value:
        if isinstance(item, UUID):
            parsed = item
        elif isinstance(item, str):
            try:
                parsed = UUID(item)
            except ValueError as exc:
                raise ResultError("rework_payload_invalid", {"field": label}) from exc
        else:
            raise ResultError("rework_payload_invalid", {"field": label})
        if parsed in result:
            raise ResultError("rework_payload_duplicate", {"field": label})
        result.append(parsed)
    return tuple(result)


def _mode(payload: Mapping[str, object]) -> ReworkMode:
    value = payload.get("mode")
    try:
        return ReworkMode(value)
    except (TypeError, ValueError) as exc:
        raise ResultError("rework_mode_invalid", {}) from exc


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResultError("rework_payload_invalid", {"field": label})
    return value.strip()


def _issue_keys(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ResultError("rework_payload_invalid", {"field": "issue_keys"})
    result = tuple(_required_text(item, "issue_keys") for item in value)
    if len(set(result)) != len(result):
        raise ResultError("rework_payload_duplicate", {"field": "issue_keys"})
    return result


def _rework_inputs(
    payload: Mapping[str, object], *, candidate: IntegrationCandidateV1
) -> tuple[tuple[str, ...], tuple[UUID, ...], str, ReworkSuggestionV1 | None]:
    raw_suggestion = payload.get("suggestion")
    suggestion: ReworkSuggestionV1 | None = None
    if raw_suggestion is not None:
        try:
            suggestion = ReworkSuggestionV1.from_mapping(raw_suggestion)
        except ValidationError as exc:
            raise ResultError("rework_suggestion_invalid", {}) from exc
        if (
            suggestion.candidate_id != candidate.id
            or suggestion.candidate_hash != candidate.content_hash
        ):
            raise ResultError("rework_suggestion_scope_mismatch", {})

    raw_issues = payload.get("issue_keys")
    issues = _issue_keys(raw_issues) if raw_issues is not None else ()
    if not issues and suggestion is not None:
        issues = suggestion.issue_keys

    raw_targets = payload.get("target_worker_item_ids")
    targets = _uuid_tuple(raw_targets, "target_worker_item_ids") if raw_targets is not None else ()
    if not targets and suggestion is not None:
        targets = suggestion.target_worker_item_ids

    raw_reason = payload.get("reason")
    reason = (
        _required_text(raw_reason, "reason")
        if raw_reason is not None and raw_reason != ""
        else suggestion.reason if suggestion is not None else ""
    )
    if not reason:
        raise ResultError("rework_reason_required", {})
    return issues, targets, reason, suggestion


def _validate_actor_for_mode(envelope: CommandEnvelope, mode: ReworkMode) -> None:
    if mode is ReworkMode.AUTOMATIC:
        if envelope.actor_type == "system" and envelope.actor_id not in {
            "collaboration",
            "result-rework",
        }:
            raise ResultError("rework_automatic_authority_required", {})
        if envelope.actor_type not in {"system", "collaboration"}:
            raise ResultError("rework_automatic_authority_required", {})
    elif envelope.actor_type == "system":
        raise ResultError("rework_user_actor_required", {})


def _validate_worker_scope(
    target_ids: tuple[UUID, ...],
    *,
    impact_ids: tuple[UUID, ...],
    worker_plan: Mapping[UUID, Any],
    allowed_keys: frozenset[str],
) -> None:
    for worker_id in (*target_ids, *impact_ids):
        if worker_id not in worker_plan:
            raise ResultError(
                "rework_worker_not_found", {"worker_item_id": str(worker_id)}
            )
    for worker_id in target_ids:
        item = worker_plan[worker_id]
        if item.key not in allowed_keys:
            raise ResultError(
                "rework_worker_not_allowed", {"worker_item_id": str(worker_id)}
            )


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResultError("rework_payload_invalid", {"field": label})
    return value


def _rework_event(
    envelope: CommandEnvelope,
    *,
    record: ReworkRecordV1,
    aggregate_version: int,
    candidate_hash: str,
    suggestion: ReworkSuggestionV1 | None,
) -> EventData:
    return EventData(
        id=uuid5(_REWORK_NAMESPACE, f"event:{record.id}"),
        workspace_id=record.workspace_id,
        aggregate_type="result_rework",
        aggregate_id=record.id,
        aggregate_version=aggregate_version,
        event_type=(
            "ResultReworkAttentionRequired"
            if record.status is ReworkStatus.BLOCKED
            else "ResultReworkRequested"
        ),
        payload={
            "rework_record_id": str(record.id),
            "root_task_id": str(record.root_task_id),
            "candidate_id": str(record.candidate_id),
            "candidate_hash": candidate_hash,
            "source_issue_keys": list(record.source_issue_keys),
            "target_worker_item_ids": [str(item) for item in record.target_worker_item_ids],
            "impact_worker_item_ids": [str(item) for item in record.impact_worker_item_ids],
            "request_ids": [str(item) for item in record.request_ids],
            "mode": record.mode.value,
            "round_number": record.round_number,
            "status": record.status.value,
            "reason": record.reason,
            "suggestion": None if suggestion is None else suggestion.to_mapping(),
        },
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )


def compute_rework_impact(
    *,
    changed_result_id: UUID,
    replacement: CodeResultSnapshotV1,
    provenance: ResultProvenance,
    hard_edges: Iterable[HardResultEdge],
) -> ReworkImpactV1:
    """Compute a stable local impact report without mutating result history.

    A fixed consumer relation is stronger than a plan edge. A plan edge with no
    matching consumed-result fact is therefore classified as pending impact;
    it is never silently treated as reusable. Edges that point outside the
    fixed provenance are structural gaps and require a plan revision.
    """

    if not isinstance(changed_result_id, UUID):
        raise ResultError("rework_changed_result_invalid", {})
    if not isinstance(replacement, CodeResultSnapshotV1):
        raise ResultError("rework_replacement_invalid", {})
    if not isinstance(provenance, Mapping):
        raise ResultError("rework_provenance_invalid", {})

    snapshots: dict[UUID, CodeResultSnapshotV1] = {}
    for result_id, snapshot in provenance.items():
        if not isinstance(result_id, UUID) or not isinstance(snapshot, CodeResultSnapshotV1):
            raise ResultError("rework_provenance_invalid", {})
        if result_id != snapshot.id:
            raise ResultError(
                "rework_provenance_key_mismatch", {"result_id": str(result_id)}
            )
        snapshots[result_id] = snapshot

    changed = snapshots.get(changed_result_id)
    if changed is None:
        raise ResultError(
            "rework_source_result_not_found", {"result_id": str(changed_result_id)}
        )
    if replacement.id == changed_result_id:
        raise ResultError("rework_replacement_not_new", {})
    if (
        replacement.workspace_id != changed.workspace_id
        or replacement.root_task_id != changed.root_task_id
        or replacement.worker_item_id != changed.worker_item_id
        or replacement.repository_ref != changed.repository_ref
    ):
        raise ResultError(
            "rework_replacement_scope_mismatch",
            {"changed_result_id": str(changed_result_id)},
        )

    consumers_by_result: dict[UUID, set[UUID]] = {}
    workers = {snapshot.worker_item_id for snapshot in snapshots.values()}
    for snapshot in snapshots.values():
        for consumed_result_id in snapshot.consumed_result_ids:
            consumers_by_result.setdefault(consumed_result_id, set()).add(
                snapshot.worker_item_id
            )

    downstream_by_worker: dict[UUID, set[UUID]] = {}
    for edge in hard_edges:
        if not isinstance(edge, tuple) or len(edge) != 2:
            raise ResultError("rework_hard_edge_invalid", {})
        upstream, downstream = edge
        if not isinstance(upstream, UUID) or not isinstance(downstream, UUID):
            raise ResultError("rework_hard_edge_invalid", {})
        if upstream == downstream:
            raise ResultError("rework_hard_edge_invalid", {"worker_id": str(upstream)})
        downstream_by_worker.setdefault(upstream, set()).add(downstream)

    equivalent = _same_code_and_input(changed, replacement)
    changed_worker = changed.worker_item_id
    direct_consumers = consumers_by_result.get(changed_result_id, set()) - {changed_worker}
    planned_consumers = downstream_by_worker.get(changed_worker, set())

    affected: set[UUID] = set()
    reasons: dict[UUID, str] = {}
    requires_revision = False

    for worker_id in sorted(direct_consumers | planned_consumers, key=str):
        if worker_id in direct_consumers:
            if equivalent:
                reasons[worker_id] = "same_code_and_input"
            else:
                affected.add(worker_id)
                reasons[worker_id] = "consumes_changed_result"
            continue

        affected.add(worker_id)
        reasons[worker_id] = (
            "hard_edge_target_missing"
            if worker_id not in workers
            else "dependency_provenance_unknown"
        )
        if worker_id not in workers:
            requires_revision = True

    queue = sorted(affected, key=str)
    while queue:
        worker_id = queue.pop(0)
        for downstream in sorted(downstream_by_worker.get(worker_id, set()), key=str):
            if downstream in affected:
                continue
            affected.add(downstream)
            reasons[downstream] = (
                "hard_edge_target_missing"
                if downstream not in workers
                else "transitive_hard_dependency"
            )
            if downstream not in workers:
                requires_revision = True
            queue.append(downstream)
        queue.sort(key=str)

    reusable = workers - {changed_worker} - affected
    # A direct consumer whose replacement is equivalent is deliberately kept
    # out of affected and remains reusable with its original execution fact.
    return ReworkImpactV1(
        changed_result_id=changed_result_id,
        replacement_result_id=replacement.id,
        affected=tuple(sorted(affected, key=str)),
        reusable=tuple(sorted(reusable, key=str)),
        requires_revision=requires_revision,
        reason_by_worker=reasons,
    )


def _same_code_and_input(
    original: CodeResultSnapshotV1, replacement: CodeResultSnapshotV1
) -> bool:
    return (
        original.workspace_id == replacement.workspace_id
        and original.root_task_id == replacement.root_task_id
        and original.worker_item_id == replacement.worker_item_id
        and original.repository_ref == replacement.repository_ref
        and original.input_commit == replacement.input_commit
        and original.input_tree == replacement.input_tree
        and original.result_tree == replacement.result_tree
        and original.consumed_result_ids == replacement.consumed_result_ids
    )


__all__ = [
    "HardResultEdge",
    "ResultProvenance",
    "ResultReworkService",
    "build_rework_context",
    "compute_rework_impact",
    "require_authorized_source_candidate",
]
