from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sidera_control_plane.application.collaboration_projection import (
    CollaborationFacts,
    CollaborationRequestFact,
)
from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.domain.artifacts import EvidenceClaimStatus
from sidera_control_plane.domain.collaboration_projections import (
    CollaborationProjectionFacts,
    CollaborationProjectionService,
    WorkerItemCollaborationSummaryProjection,
)
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.input_selections import (
    CollaborationInputSelectionV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_inputs import (
    InputCandidateState,
    InputGroupCandidateV1,
    InputGroupMode,
    InputGroupRequirementsV1,
    InputGroupV1,
    InputSelectionStrategy,
    ResultInputArtifact,
    ResultInputEvidence,
    ResultInputSource,
)

MAX_RESULT_INPUT_CANDIDATES = 32
MAX_RESULT_INPUT_GROUPS = 32
MAX_RESULT_INPUT_HARD_DEPENDENCIES = 32
MAX_RESULT_INPUT_CONSUMERS = 50
MAX_RESULT_INPUT_HISTORY = 20


@dataclass(frozen=True, slots=True)
class ResultInputWorkerItemFact:
    workspace_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    worker_item_key: str
    title: str
    responsibility: str
    status: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResultInputDeliveryFact:
    consumer_type: str
    consumer_id: UUID
    execution_request_id: UUID | None
    worker_item_id: UUID | None
    package_id: UUID | None
    context_version: int | None
    package_hash: str | None
    package_status: str | None
    assembly_request_id: UUID | None
    assembly_status: str | None
    assembly_attempt: int | None
    assembly_error_code: str | None
    assembly_version: int | None
    assembly_request_fingerprint: str | None
    materialization_attempt: int | None
    materialization_outcome: str | None
    materialization_failure_code: str | None
    materialization_occurred_at: datetime | None
    acknowledgement_status: str
    acknowledged_package_hash: str | None
    acknowledged_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResultInputQuerySnapshot:
    worker_item: ResultInputWorkerItemFact
    request: CollaborationRequestFact | None
    facts: CollaborationFacts | None
    projection_facts: CollaborationProjectionFacts | None
    selection: CollaborationInputSelectionV1 | None
    delivery: ResultInputDeliveryFact | None
    is_historical: bool


@dataclass(frozen=True, slots=True)
class ResultInputSelectionQuerySnapshot:
    selection: CollaborationInputSelectionV1
    plan: TaskPlanSnapshot | None
    consumers: tuple[ResultInputDeliveryFact, ...]


class ResultInputQueryStore(Protocol):
    def load_worker_result_inputs(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
        execution_request_id: UUID | None,
    ) -> ResultInputQuerySnapshot | None:
        raise NotImplementedError

    def load_input_selection(
        self, workspace_id: UUID, selection_id: UUID
    ) -> ResultInputSelectionQuerySnapshot | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ResultInputArtifactView:
    artifact_id: UUID
    artifact_type: str
    content_hash: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class ResultInputEvidenceView:
    evidence_id: UUID
    acceptance_criterion_ref: str
    artifact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ResultInputCandidateView:
    worker_item_key: str
    worker_item_id: UUID | None
    worker_item_title: str
    subtask_key: str | None
    subtask_path: tuple[str, ...]
    priority: int
    ready: bool
    reason_code: str | None
    missing_artifact_refs: tuple[str, ...]
    missing_evidence_refs: tuple[str, ...]
    retry_possible: bool
    outcome_unknown: bool
    execution_request_id: UUID | None
    execution_id: UUID | None
    attempt: int | None
    execution_status: str | None
    artifacts: tuple[ResultInputArtifactView, ...]
    evidence: tuple[ResultInputEvidenceView, ...]


@dataclass(frozen=True, slots=True)
class ResultInputSelectedSourceView:
    worker_item_key: str
    worker_item_id: UUID
    worker_item_title: str
    subtask_key: str | None
    subtask_path: tuple[str, ...]
    effective_request_id: UUID
    effective_lineage_id: UUID
    effective_execution_id: UUID
    attempt: int
    priority: int
    inheritance_refs: tuple[str, ...]
    source_basis_hash: str
    artifacts: tuple[ResultInputArtifactView, ...]
    evidence: tuple[ResultInputEvidenceView, ...]


@dataclass(frozen=True, slots=True)
class ResultInputGroupView:
    group_key: str
    title: str
    mode: str
    minimum_count: int | None
    required_count: int
    ready_count: int
    reason_code: str | None
    candidates: tuple[ResultInputCandidateView, ...]
    selected_sources: tuple[ResultInputSelectedSourceView, ...]
    selection_id: UUID | None
    selection_hash: str | None


@dataclass(frozen=True, slots=True)
class ResultInputHardDependencyView:
    dependency_key: str
    upstream_request_id: UUID
    upstream_worker_item_id: UUID | None
    upstream_worker_item_key: str
    upstream_worker_item_title: str
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]
    ready: bool
    reason_code: str | None
    selected_source: ResultInputSelectedSourceView | None


@dataclass(frozen=True, slots=True)
class ResultInputRequestView:
    execution_request_id: UUID
    worker_item_id: UUID
    worker_item_key: str
    subtask_key: str
    version: int
    status: str
    collaboration_gate_mode: str
    gate_reason: str


@dataclass(frozen=True, slots=True)
class ResultInputExecutionView:
    execution_request_id: UUID
    request_status: str
    request_version: int
    collaboration_gate_mode: str
    gate_reason: str
    execution_id: UUID | None
    lineage_id: UUID | None
    attempt: int | None
    status: str | None
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    release_status: str
    released_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResultInputGovernanceHistoryView:
    kind: str
    resource_id: UUID
    status: str
    summary: str
    occurred_at: datetime
    result_code: str | None
    proposal_id: UUID | None
    resolution_action: str | None
    resolution_result: str | None


@dataclass(frozen=True, slots=True)
class ResultInputGovernanceView:
    enabled: bool
    state: str
    waiting_summary: str | None
    governance_mode: str
    release_state: str
    active_hold_id: UUID | None
    hold_resource_version: int | None
    active_proposal_id: UUID | None
    active_proposal_version: int | None
    active_proposal_input_selection_id: UUID | None
    active_proposal_input_selection_hash: str | None
    attention_state: str
    available_action_names: tuple[str, ...]
    history: tuple[ResultInputGovernanceHistoryView, ...]


@dataclass(frozen=True, slots=True)
class ResultInputDeliveryView:
    consumer_type: str
    consumer_id: UUID | None
    package_id: UUID | None
    context_version: int | None
    package_hash: str | None
    package_status: str | None
    assembly_request_id: UUID | None
    assembly_status: str | None
    assembly_attempt: int | None
    assembly_version: int | None
    assembly_request_fingerprint: str | None
    materialization_attempt: int | None
    materialization_outcome: str | None
    materialization_failure_code: str | None
    materialization_occurred_at: datetime | None
    acknowledgement_status: str
    acknowledged_package_hash: str | None
    acknowledged_at: datetime | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class ResultInputActionView:
    action: str
    command_type: str
    target_type: str
    target_id: UUID
    expected_version: int | None
    expected_fingerprint: str | None
    expected_plan_generation: int | None
    expected_input_selection_id: UUID | None
    expected_input_selection_hash: str | None


@dataclass(frozen=True, slots=True)
class WorkerResultInputsView:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    request: ResultInputRequestView | None
    plan_snapshot_id: UUID | None
    plan_generation: int | None
    plan_content_hash: str | None
    is_historical: bool
    groups: tuple[ResultInputGroupView, ...]
    hard_dependencies: tuple[ResultInputHardDependencyView, ...]
    governance: ResultInputGovernanceView
    delivery: ResultInputDeliveryView
    execution: ResultInputExecutionView | None
    available_actions: tuple[ResultInputActionView, ...]


@dataclass(frozen=True, slots=True)
class InputSelectionConsumerView:
    consumer_type: str
    consumer_id: UUID
    execution_request_id: UUID | None
    worker_item_id: UUID | None
    package_id: UUID | None
    context_version: int | None
    package_hash: str | None
    package_status: str | None
    assembly_request_id: UUID | None
    assembly_status: str | None
    assembly_attempt: int | None
    assembly_version: int | None
    materialization_attempt: int | None
    materialization_outcome: str | None
    materialization_failure_code: str | None
    acknowledgement_status: str
    acknowledged_at: datetime | None


@dataclass(frozen=True, slots=True)
class InputSelectionGroupView:
    group_key: str
    rule_hash: str
    sources: tuple[ResultInputSelectedSourceView, ...]


@dataclass(frozen=True, slots=True)
class InputSelectionHardInputView:
    dependency_key: str
    rule_hash: str
    source: ResultInputSelectedSourceView


@dataclass(frozen=True, slots=True)
class InputSelectionView:
    schema_version: int
    selection_id: UUID
    workspace_id: UUID
    root_task_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    target_worker_item_id: UUID
    target_execution_request_id: UUID
    target_request_version: int
    projection_snapshot_id: UUID
    projection_hash: str
    decision_id: UUID
    intent_id: UUID
    groups: tuple[InputSelectionGroupView, ...]
    hard_inputs: tuple[InputSelectionHardInputView, ...]
    selection_hash: str
    created_at: datetime
    consumers: tuple[InputSelectionConsumerView, ...]
    consumers_truncated: bool


class ResultInputQueryService:
    def __init__(
        self,
        store: ResultInputQueryStore,
        *,
        governance_enabled: bool = True,
    ) -> None:
        self._store = store
        self._governance_enabled = governance_enabled

    def get_worker_result_inputs(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
        *,
        execution_request_id: UUID | None = None,
    ) -> WorkerResultInputsView:
        snapshot = self._store.load_worker_result_inputs(
            workspace_id, worker_item_id, execution_request_id
        )
        if snapshot is None:
            raise NotFound("worker item result inputs not found")
        return self._compose_worker(snapshot)

    def get_input_selection(
        self, workspace_id: UUID, selection_id: UUID
    ) -> InputSelectionView:
        snapshot = self._store.load_input_selection(workspace_id, selection_id)
        if snapshot is None:
            raise NotFound("input selection not found")
        selection = snapshot.selection
        return InputSelectionView(
            schema_version=selection.schema_version,
            selection_id=selection.id,
            workspace_id=selection.workspace_id,
            root_task_id=selection.root_task_id,
            run_id=selection.run_id,
            plan_snapshot_id=selection.plan_snapshot_id,
            plan_generation=selection.plan_generation,
            plan_content_hash=selection.plan_content_hash,
            target_worker_item_id=selection.target_worker_item_id,
            target_execution_request_id=selection.target_execution_request_id,
            target_request_version=selection.target_request_version,
            projection_snapshot_id=selection.projection_snapshot_id,
            projection_hash=selection.projection_hash,
            decision_id=selection.decision_id,
            intent_id=selection.intent_id,
            groups=tuple(
                InputSelectionGroupView(
                    group_key=group.group_key,
                    rule_hash=group.rule_hash,
                    sources=tuple(
                        self._selected_source(source, plan=snapshot.plan)
                        for source in group.sources
                    ),
                )
                for group in selection.groups
            ),
            hard_inputs=tuple(
                InputSelectionHardInputView(
                    dependency_key=item.dependency_key,
                    rule_hash=item.rule_hash,
                    source=self._selected_source(item.source, plan=snapshot.plan),
                )
                for item in selection.hard_inputs
            ),
            selection_hash=selection.selection_hash,
            created_at=selection.created_at,
            consumers=tuple(
                _consumer_view(item) for item in snapshot.consumers[:MAX_RESULT_INPUT_CONSUMERS]
            ),
            consumers_truncated=len(snapshot.consumers) > MAX_RESULT_INPUT_CONSUMERS,
        )

    def _compose_worker(
        self, snapshot: ResultInputQuerySnapshot
    ) -> WorkerResultInputsView:
        facts = snapshot.facts
        request = snapshot.request
        projection_facts = snapshot.projection_facts
        plan = facts.plan_snapshot if facts is not None else None
        groups = self._groups(snapshot)
        hard_dependencies = self._hard_dependencies(snapshot)
        governance_summary = (
            CollaborationProjectionService(
                governance_enabled=self._governance_enabled
            ).worker_summary(projection_facts, worker_item_id=snapshot.worker_item.worker_item_id)
            if projection_facts is not None
            else None
        )
        governance = _governance_view(governance_summary)
        return WorkerResultInputsView(
            schema_version=1,
            workspace_id=snapshot.worker_item.workspace_id,
            root_task_id=snapshot.worker_item.root_task_id,
            worker_item_id=snapshot.worker_item.worker_item_id,
            worker_item_key=snapshot.worker_item.worker_item_key,
            worker_item_title=snapshot.worker_item.title,
            request=(
                ResultInputRequestView(
                    execution_request_id=request.id,
                    worker_item_id=request.worker_item_id,
                    worker_item_key=request.worker_item_key,
                    subtask_key=request.subtask_key,
                    version=request.version,
                    status=request.status,
                    collaboration_gate_mode=request.collaboration_gate_mode,
                    gate_reason=request.gate_reason,
                )
                if request is not None
                else None
            ),
            plan_snapshot_id=plan.id if plan is not None else None,
            plan_generation=plan.generation if plan is not None else None,
            plan_content_hash=(
                facts.plan_head.plan_content_hash if facts is not None else None
            ),
            is_historical=snapshot.is_historical,
            groups=groups,
            hard_dependencies=hard_dependencies,
            governance=governance,
            delivery=_delivery_view(snapshot.delivery, request=request),
            execution=_execution_view(snapshot, request=request),
            available_actions=_available_actions(
                snapshot,
                governance_summary=governance_summary,
                governance_enabled=self._governance_enabled,
            ),
        )

    def _groups(
        self, snapshot: ResultInputQuerySnapshot
    ) -> tuple[ResultInputGroupView, ...]:
        facts = snapshot.facts
        request = snapshot.request
        if facts is None or request is None:
            return ()
        raw_policy = facts.plan_head.policy_snapshot
        if raw_policy is None:
            policy = facts.plan_snapshot.collaboration_policy
            raw_policy = policy.to_mapping() if policy is not None else {}
        raw_groups = raw_policy.get("input_groups", ())
        if not isinstance(raw_groups, (list, tuple)):
            return ()
        configured = tuple(
            InputGroupV1.from_mapping(item)
            for item in raw_groups[:MAX_RESULT_INPUT_GROUPS]
        )
        evaluator = _result_input_evaluator()
        selection_groups = {
            item.group_key: item for item in (snapshot.selection.groups if snapshot.selection else ())
        }
        result: list[ResultInputGroupView] = []
        for group in configured:
            if group.consumer_worker_item_key != request.worker_item_key:
                continue
            sources = tuple(
                _resolve_source(candidate.worker_item_key, facts)
                for candidate in group.candidates[:MAX_RESULT_INPUT_CANDIDATES]
            )
            evaluation = evaluator.evaluate_group(group=group, sources=sources)
            selected_group = selection_groups.get(group.group_key)
            result.append(
                ResultInputGroupView(
                    group_key=group.group_key,
                    title=group.title,
                    mode=group.mode.value,
                    minimum_count=group.minimum_count,
                    required_count=evaluation.required_count,
                    ready_count=evaluation.ready_count,
                    reason_code=evaluation.reason_code,
                    candidates=tuple(
                        self._candidate_view(
                            candidate,
                            state,
                            facts=facts,
                            plan=plan_for(facts),
                        )
                        for candidate, state in zip(
                            group.candidates[:MAX_RESULT_INPUT_CANDIDATES],
                            evaluation.candidate_states,
                            strict=True,
                        )
                    ),
                    selected_sources=tuple(
                        self._selected_source(source, plan=plan_for(facts))
                        for source in (
                            selected_group.sources if selected_group is not None else ()
                        )
                    ),
                    selection_id=(
                        snapshot.selection.id if selected_group is not None and snapshot.selection else None
                    ),
                    selection_hash=(
                        snapshot.selection.selection_hash
                        if selected_group is not None and snapshot.selection
                        else None
                    ),
                )
            )
        return tuple(result)

    def _hard_dependencies(
        self, snapshot: ResultInputQuerySnapshot
    ) -> tuple[ResultInputHardDependencyView, ...]:
        facts = snapshot.facts
        request = snapshot.request
        if facts is None or request is None:
            return ()
        requests = {item.id: item for item in facts.requests}
        fixed = {
            item.dependency_key: item
            for item in (snapshot.selection.hard_inputs if snapshot.selection else ())
        }
        evaluator = _result_input_evaluator()
        result: list[ResultInputHardDependencyView] = []
        for dependency in facts.dependencies:
            if dependency.downstream_request_id != request.id:
                continue
            if getattr(dependency.enforcement, "value", dependency.enforcement) != "hard":
                continue
            upstream = requests.get(dependency.upstream_request_id)
            if upstream is None:
                continue
            group = InputGroupV1(
                group_key=f"hard:{dependency.upstream_request_id}",
                title=f"Hard input {upstream.worker_item_key}",
                consumer_worker_item_key=request.worker_item_key,
                candidates=(
                    InputGroupCandidateV1(
                        worker_item_key=upstream.worker_item_key,
                        priority=1,
                    ),
                ),
                requirements=InputGroupRequirementsV1(
                    execution_status=ExecutionStatus.SUCCEEDED,
                    required_artifact_refs=dependency.required_artifact_refs,
                    required_evidence_refs=dependency.required_evidence_refs,
                    evidence_status=EvidenceClaimStatus.CANDIDATE,
                ),
                mode=InputGroupMode.ALL,
                minimum_count=None,
                selection_strategy=InputSelectionStrategy.PLAN_ORDER,
            )
            source = _resolve_source(upstream.worker_item_key, facts)
            evaluation = evaluator.evaluate_group(group=group, sources=(source,))
            state = evaluation.candidate_states[0]
            dependency_key = f"request:{dependency.upstream_request_id}"
            result.append(
                ResultInputHardDependencyView(
                    dependency_key=dependency_key,
                    upstream_request_id=upstream.id,
                    upstream_worker_item_id=source.worker_item_id,
                    upstream_worker_item_key=upstream.worker_item_key,
                    upstream_worker_item_title=_worker_title(
                        facts.plan_snapshot, upstream.worker_item_key
                    ),
                    required_artifact_refs=dependency.required_artifact_refs,
                    required_evidence_refs=dependency.required_evidence_refs,
                    ready=state.ready,
                    reason_code=state.reason_code,
                    selected_source=(
                        self._selected_source(fixed[dependency_key].source, plan=plan_for(facts))
                        if dependency_key in fixed
                        else None
                    ),
                )
            )
            if len(result) >= MAX_RESULT_INPUT_HARD_DEPENDENCIES:
                break
        return tuple(result)

    @staticmethod
    def _candidate_view(
        candidate: InputGroupCandidateV1,
        state: InputCandidateState,
        *,
        facts: CollaborationFacts,
        plan: Any,
    ) -> ResultInputCandidateView:
        source = state.source
        return ResultInputCandidateView(
            worker_item_key=candidate.worker_item_key,
            worker_item_id=source.worker_item_id if source is not None else None,
            worker_item_title=_worker_title(plan, candidate.worker_item_key),
            subtask_key=(
                _worker_subtask_key(plan, candidate.worker_item_key)
                if plan is not None
                else None
            ),
            subtask_path=source.subtask_path if source is not None else (),
            priority=candidate.priority,
            ready=state.ready,
            reason_code=(
                None
                if state.ready
                else state.reason_code or (source.reason_code if source is not None else None)
            ),
            missing_artifact_refs=state.missing_artifact_refs,
            missing_evidence_refs=state.missing_evidence_refs,
            retry_possible=state.retry_possible,
            outcome_unknown=state.outcome_unknown,
            execution_request_id=(
                source.effective_request_id if source is not None else None
            ),
            execution_id=(
                source.effective_execution_id if source is not None else None
            ),
            attempt=source.attempt if source is not None else None,
            execution_status=(
                source.execution_status.value
                if source is not None and source.execution_status is not None
                else None
            ),
            artifacts=_artifact_views(source.artifacts if source is not None else ()),
            evidence=_evidence_views(source.evidence if source is not None else ()),
        )

    @staticmethod
    def _selected_source(
        source: InputSelectionSourceV1,
        *,
        plan: Any,
    ) -> ResultInputSelectedSourceView:
        return ResultInputSelectedSourceView(
            worker_item_key=source.worker_item_key,
            worker_item_id=source.worker_item_id,
            worker_item_title=_worker_title(plan, source.worker_item_key),
            subtask_key=(
                _worker_subtask_key(plan, source.worker_item_key)
                if plan is not None
                else None
            ),
            subtask_path=source.subtask_path,
            effective_request_id=source.effective_request_id,
            effective_lineage_id=source.effective_lineage_id,
            effective_execution_id=source.effective_execution_id,
            attempt=source.attempt,
            priority=source.priority,
            inheritance_refs=source.inheritance_refs,
            source_basis_hash=source.source_basis_hash,
            artifacts=_artifact_views(source.artifacts),
            evidence=_evidence_views(source.evidence),
        )


def _result_input_evaluator() -> Any:
    from sidera_control_plane.application.result_input_evaluation import (
        ResultInputEvaluator,
    )

    return ResultInputEvaluator()


def _resolve_source(worker_item_key: str, facts: CollaborationFacts) -> ResultInputSource:
    from sidera_control_plane.application.result_input_sources import (
        resolve_result_input_source,
    )

    return resolve_result_input_source(worker_item_key=worker_item_key, facts=facts)


def plan_for(facts: CollaborationFacts) -> Any:
    return facts.plan_snapshot


def _worker_title(plan: Any, worker_item_key: str) -> str:
    if plan is None:
        return worker_item_key
    for item in plan.worker_item_plans:
        if item.key == worker_item_key:
            return item.title
    for item in plan.worker_items:
        if item.key == worker_item_key:
            return item.title
    return worker_item_key


def _worker_subtask_key(plan: Any, worker_item_key: str) -> str | None:
    if plan is None:
        return None
    for item in plan.worker_item_plans:
        if item.key == worker_item_key:
            return item.subtask_key
    for item in plan.worker_items:
        if item.key == worker_item_key:
            return item.subtask_key
    return None


def _artifact_views(
    artifacts: tuple[ResultInputArtifact, ...],
) -> tuple[ResultInputArtifactView, ...]:
    return tuple(
        ResultInputArtifactView(
            artifact_id=item.artifact_id,
            artifact_type=item.artifact_type.value,
            content_hash=item.content_hash,
            size_bytes=item.size_bytes,
            media_type=item.media_type,
        )
        for item in artifacts
    )


def _evidence_views(
    evidence: tuple[ResultInputEvidence, ...],
) -> tuple[ResultInputEvidenceView, ...]:
    return tuple(
        ResultInputEvidenceView(
            evidence_id=item.evidence_id,
            acceptance_criterion_ref=item.acceptance_criterion_ref,
            artifact_ids=item.artifact_ids,
        )
        for item in evidence
    )


def _governance_view(
    summary: WorkerItemCollaborationSummaryProjection | None,
) -> ResultInputGovernanceView:
    if summary is None:
        return ResultInputGovernanceView(
            enabled=False,
            state="unmanaged",
            waiting_summary=None,
            governance_mode="automatic",
            release_state="unreleased",
            active_hold_id=None,
            hold_resource_version=None,
            active_proposal_id=None,
            active_proposal_version=None,
            active_proposal_input_selection_id=None,
            active_proposal_input_selection_hash=None,
            attention_state="none",
            available_action_names=(),
            history=(),
        )
    proposal = summary.active_proposal
    return ResultInputGovernanceView(
        enabled=summary.enabled,
        state=summary.state,
        waiting_summary=summary.waiting_summary,
        governance_mode=summary.governance_mode,
        release_state=summary.release_state,
        active_hold_id=summary.active_hold_id,
        hold_resource_version=summary.hold_resource_version,
        active_proposal_id=proposal.proposal_id if proposal is not None else None,
        active_proposal_version=(
            proposal.resource_version if proposal is not None else None
        ),
        active_proposal_input_selection_id=(
            proposal.input_selection_id if proposal is not None else None
        ),
        active_proposal_input_selection_hash=(
            proposal.input_selection_hash if proposal is not None else None
        ),
        attention_state=summary.attention_state,
        available_action_names=summary.available_actions,
        history=tuple(
            ResultInputGovernanceHistoryView(
                kind=item.kind,
                resource_id=item.resource_id,
                status=item.status,
                summary=item.summary,
                occurred_at=item.occurred_at,
                result_code=item.result_code,
                proposal_id=item.proposal_id,
                resolution_action=item.resolution_action,
                resolution_result=item.resolution_result,
            )
            for item in summary.governance_history[:MAX_RESULT_INPUT_HISTORY]
        ),
    )


def _delivery_view(
    delivery: ResultInputDeliveryFact | None,
    *,
    request: CollaborationRequestFact | None,
) -> ResultInputDeliveryView:
    if delivery is None:
        return ResultInputDeliveryView(
            consumer_type="worker",
            consumer_id=None,
            package_id=None,
            context_version=None,
            package_hash=None,
            package_status=None,
            assembly_request_id=None,
            assembly_status=None,
            assembly_attempt=None,
            assembly_version=None,
            assembly_request_fingerprint=None,
            materialization_attempt=None,
            materialization_outcome=None,
            materialization_failure_code=None,
            materialization_occurred_at=None,
            acknowledgement_status="not_acknowledged",
            acknowledged_package_hash=None,
            acknowledged_at=None,
            reason_code=("request_not_released" if request is not None else "not_requested"),
        )
    reason = (
        delivery.assembly_error_code
        or delivery.materialization_failure_code
        or (
            "acknowledged"
            if delivery.acknowledgement_status == "acknowledged"
            else None
        )
        or ("package_ready" if delivery.package_status == "active" else None)
    )
    return ResultInputDeliveryView(
        consumer_type=delivery.consumer_type,
        consumer_id=delivery.consumer_id,
        package_id=delivery.package_id,
        context_version=delivery.context_version,
        package_hash=delivery.package_hash,
        package_status=delivery.package_status,
        assembly_request_id=delivery.assembly_request_id,
        assembly_status=delivery.assembly_status,
        assembly_attempt=delivery.assembly_attempt,
        assembly_version=delivery.assembly_version,
        assembly_request_fingerprint=delivery.assembly_request_fingerprint,
        materialization_attempt=delivery.materialization_attempt,
        materialization_outcome=delivery.materialization_outcome,
        materialization_failure_code=delivery.materialization_failure_code,
        materialization_occurred_at=delivery.materialization_occurred_at,
        acknowledgement_status=delivery.acknowledgement_status,
        acknowledged_package_hash=delivery.acknowledged_package_hash,
        acknowledged_at=delivery.acknowledged_at,
        reason_code=reason,
    )


def _execution_view(
    snapshot: ResultInputQuerySnapshot,
    *,
    request: CollaborationRequestFact | None,
) -> ResultInputExecutionView | None:
    if request is None:
        return None
    executions = (
        tuple(
            item
            for item in snapshot.facts.executions
            if request.execution_lineage_id is not None
            and item.lineage_id == request.execution_lineage_id
        )
        if snapshot.facts is not None
        else ()
    )
    latest = max(
        executions,
        key=lambda item: (
            item.attempt,
            item.finished_at
            or item.started_at
            or datetime.min.replace(tzinfo=UTC),
        ),
        default=None,
    )
    release = next(
        (
            item
            for item in (snapshot.facts.releases if snapshot.facts is not None else ())
            if item.execution_request_id == request.id
        ),
        None,
    )
    return ResultInputExecutionView(
        execution_request_id=request.id,
        request_status=request.status,
        request_version=request.version,
        collaboration_gate_mode=request.collaboration_gate_mode,
        gate_reason=request.gate_reason,
        execution_id=latest.id if latest is not None else None,
        lineage_id=latest.lineage_id if latest is not None else request.execution_lineage_id,
        attempt=latest.attempt if latest is not None else None,
        status=latest.status if latest is not None else None,
        failure_code=latest.failure_code if latest is not None else None,
        started_at=latest.started_at if latest is not None else None,
        finished_at=latest.finished_at if latest is not None else None,
        release_status="released" if release is not None else "not_released",
        released_at=release.issued_at if release is not None else None,
    )


def _available_actions(
    snapshot: ResultInputQuerySnapshot,
    *,
    governance_summary: WorkerItemCollaborationSummaryProjection | None,
    governance_enabled: bool,
) -> tuple[ResultInputActionView, ...]:
    if (
        not governance_enabled
        or governance_summary is None
        or not governance_summary.enabled
        or snapshot.is_historical
    ):
        return ()
    actions: list[ResultInputActionView] = []
    proposal = governance_summary.active_proposal
    for action in governance_summary.available_actions:
        if action == "pause":
            if snapshot.facts is None:
                continue
            actions.append(
                ResultInputActionView(
                    action=action,
                    command_type="PauseCollaborationRelease",
                    target_type="worker_item",
                    target_id=snapshot.worker_item.worker_item_id,
                    expected_version=snapshot.worker_item.version,
                    expected_fingerprint=None,
                    expected_plan_generation=snapshot.facts.plan_head.plan_generation,
                    expected_input_selection_id=None,
                    expected_input_selection_hash=None,
                )
            )
        elif action in {"approve", "reject"} and proposal is not None:
            actions.append(
                ResultInputActionView(
                    action=action,
                    command_type=(
                        "ApproveCollaborationReleaseProposal"
                        if action == "approve"
                        else "RejectCollaborationReleaseProposal"
                    ),
                    target_type="collaboration_release_proposal",
                    target_id=proposal.proposal_id,
                    expected_version=proposal.resource_version,
                    expected_fingerprint=proposal.input_selection_hash,
                    expected_plan_generation=(
                        snapshot.facts.plan_head.plan_generation
                        if snapshot.facts is not None
                        else None
                    ),
                    expected_input_selection_id=proposal.input_selection_id,
                    expected_input_selection_hash=proposal.input_selection_hash,
                )
            )
        elif action == "resume_automatic" and governance_summary.active_hold_id is not None:
            actions.append(
                ResultInputActionView(
                    action=action,
                    command_type="ResumeAutomaticCollaboration",
                    target_type="collaboration_governance_hold",
                    target_id=governance_summary.active_hold_id,
                    expected_version=governance_summary.hold_resource_version,
                    expected_fingerprint=None,
                    expected_plan_generation=(
                        snapshot.facts.plan_head.plan_generation
                        if snapshot.facts is not None
                        else None
                    ),
                    expected_input_selection_id=None,
                    expected_input_selection_hash=None,
                )
            )
    return tuple(actions)


def _consumer_view(item: ResultInputDeliveryFact) -> InputSelectionConsumerView:
    return InputSelectionConsumerView(
        consumer_type=item.consumer_type,
        consumer_id=item.consumer_id,
        execution_request_id=item.execution_request_id,
        worker_item_id=item.worker_item_id,
        package_id=item.package_id,
        context_version=item.context_version,
        package_hash=item.package_hash,
        package_status=item.package_status,
        assembly_request_id=item.assembly_request_id,
        assembly_status=item.assembly_status,
        assembly_attempt=item.assembly_attempt,
        assembly_version=item.assembly_version,
        materialization_attempt=item.materialization_attempt,
        materialization_outcome=item.materialization_outcome,
        materialization_failure_code=item.materialization_failure_code,
        acknowledgement_status=item.acknowledgement_status,
        acknowledged_at=item.acknowledged_at,
    )
