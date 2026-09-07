from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.planner_runtime import (
    PlannerClarificationResult,
    PlannerExplicitFailure,
    PlannerFailure,
    PlannerInput,
    PlannerInvalidResult,
    PlannerProposalResult,
    PlannerResult,
    PlannerRunControl,
    PlannerTimeout,
    PlannerUnknownOutcome,
)
from sidera_control_plane.application.revision_planning_context import (
    RevisionPlanningContextV1,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyRequestV1,
    CollaborationPolicyRequestV2,
    resolve_collaboration_policy,
    resolve_collaboration_policy_v2,
)
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextMaterializationV1,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.plan_revisions import (
    ContextDisposition,
    IdentityStrategy,
    SubtaskIdentityDecision,
    WorkerItemIdentityDecision,
)
from sidera_control_plane.domain.planning import (
    PlanningClarification,
    PlanningRequestKind,
)
from sidera_control_plane.domain.planning_proposals import (
    ActivationDependency,
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    PlanningProposalPayloadV4,
    PlanningProposalPayloadV6,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkerItemPlanItem,
    WorkItemKind,
)
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer

FakeScenario = Literal[
    "proposal",
    "revision",
    "revision_control_room",
    "clarification",
    "scale",
    "invalid_schema",
    "timeout",
    "unknown_outcome",
]
FakeCollaborationMode = Literal["disabled", "enabled"]


@dataclass(frozen=True, slots=True)
class FakePlannerConfig:
    scenario: FakeScenario
    provider_ref: str = "fake-planner"
    collaboration_mode: FakeCollaborationMode = "disabled"

    def __post_init__(self) -> None:
        if self.scenario not in {
            "proposal",
            "revision",
            "revision_control_room",
            "clarification",
            "scale",
            "invalid_schema",
            "timeout",
            "unknown_outcome",
        }:
            raise ValueError(f"unsupported fake planner scenario: {self.scenario}")
        if not self.provider_ref.strip():
            raise ValueError("fake planner provider_ref is required")
        if self.collaboration_mode not in {"disabled", "enabled"}:
            raise ValueError(
                f"unsupported fake collaboration mode: {self.collaboration_mode}"
            )


class FakePlannerAdapter:
    _CAPABILITIES = PlannerAdapterCapabilityProfile(
        structured_output=StructuredOutputCapability.JSON_SCHEMA,
        clarification=True,
        idempotency=IdempotencyCapability.OPERATION_ID,
        cancellation=CancellationCapability.RUN,
        usage_reporting=UsageReportingCapability.TOKENS,
        max_context_bytes=128_000,
        stability=StabilityLevel.STABLE,
        interface_version="sidera.planner.v1",
        proposal_schema_versions=(2, 3, 4, 6),
        context_package_versions=(1,),
    )

    def __init__(self, config: FakePlannerConfig) -> None:
        self.config = config
        self._cancelled_operations: set[UUID] = set()

    @property
    def cancelled_operations(self) -> frozenset[UUID]:
        return frozenset(self._cancelled_operations)

    def probe(self, config: Mapping[str, object]) -> PlannerAdapterCapabilityProfile:
        configured_scenario = config.get("scenario")
        if (
            configured_scenario is not None
            and configured_scenario != self.config.scenario
        ):
            raise ValueError("fake planner scenario must be explicitly configured")
        return self._CAPABILITIES

    def accept_context(
        self,
        materialization: ContextMaterializationV1,
        *,
        operation_id: UUID,
    ) -> ContextAcknowledgement:
        if not isinstance(operation_id, UUID):
            raise TypeError("fake planner operation_id must be a UUID")
        if (
            materialization.schema_version
            not in self._CAPABILITIES.context_package_versions
        ):
            raise ValueError("fake planner context materialization is unsupported")
        if materialization.total_byte_count > self._CAPABILITIES.max_context_bytes:
            raise ValueError("fake planner context materialization exceeds its limit")
        return ContextAcknowledgement(
            package_id=materialization.metadata.package_id,
            package_hash=materialization.metadata.package_hash,
            consumer_type=materialization.metadata.consumer_type,
            consumer_id=materialization.metadata.consumer_id,
            context_version=materialization.metadata.context_version,
            adapter_module_id=UUID(int=0),
            adapter_config_version=1,
            acknowledged_at=datetime.now(UTC),
            schema_version=materialization.schema_version,
            operation_id=operation_id,
        )

    def plan(
        self, planner_input: PlannerInput, control: PlannerRunControl
    ) -> PlannerResult:
        if control.operation_id != planner_input.operation_id:
            return self._failure(
                PlannerExplicitFailure,
                code="operation_id_mismatch",
                safe_message="planner operation id does not match input",
                retryable=False,
                unknown_outcome=False,
            )
        if (
            control.cancel_requested
            or control.operation_id in self._cancelled_operations
        ):
            return self._failure(
                PlannerExplicitFailure,
                code="cancelled",
                safe_message="planner operation was cancelled",
                retryable=False,
                unknown_outcome=False,
            )
        if (
            self.config.scenario == "clarification"
            and not planner_input.brief.user_feedback
        ):
            return PlannerClarificationResult(
                clarifications=(
                    PlanningClarification.create(
                        clarification_id=uuid5(
                            NAMESPACE_URL,
                            f"sidera.fake.planner:{planner_input.operation_id}:clarification",
                        ),
                        planning_request_id=planner_input.planning_request_id,
                        planning_run_id=planner_input.planning_run_id,
                        question="Should the fake planner use the standard code adapter?",
                        rationale="The request does not select an execution adapter.",
                        answer_type="choice",
                        options=("fake", "opencode"),
                        blocking=True,
                        created_at=control.started_at,
                    ),
                )
            )
        if self.config.scenario == "invalid_schema":
            return self._failure(
                PlannerInvalidResult,
                code="invalid_result",
                safe_message="fake planner produced an invalid proposal schema",
                retryable=True,
                unknown_outcome=False,
            )
        if self.config.scenario == "timeout":
            return self._failure(
                PlannerTimeout,
                code="timeout",
                safe_message="fake planner timed out",
                retryable=True,
                unknown_outcome=False,
            )
        if self.config.scenario == "unknown_outcome":
            return self._failure(
                PlannerUnknownOutcome,
                code="unknown_outcome",
                safe_message="fake planner outcome is unknown",
                retryable=False,
                unknown_outcome=True,
            )
        if planner_input.kind is PlanningRequestKind.REVISION:
            if not isinstance(
                planner_input.planning_context, RevisionPlanningContextV1
            ):
                return self._failure(
                    PlannerInvalidResult,
                    code="invalid_result",
                    safe_message="revision planner context is invalid",
                    retryable=True,
                    unknown_outcome=False,
                )
            try:
                base_plan = TaskPlanSnapshot.from_payload(
                    dict(planner_input.planning_context.plan_snapshot_payload)
                )
                payload_builder = (
                    _revision_control_room_payload
                    if self.config.scenario == "revision_control_room"
                    else _revision_proposal_payload
                )
                payload = payload_builder(planner_input.planning_context, base_plan)
                if planner_input.proposal_schema_version == 4:
                    payload = _revision_proposal_payload_v4(
                        payload, self.config.collaboration_mode
                    )
                elif planner_input.proposal_schema_version == 6:
                    payload = _revision_proposal_payload_v6(
                        payload, self.config.collaboration_mode
                    )
                collaboration_policy = (
                    resolve_collaboration_policy_v2(
                        payload.collaboration_policy_request,
                        planner_input.profile_snapshot.collaboration_runtime_binding,
                        subtask_keys=frozenset(item.key for item in payload.subtasks),
                    )
                    if isinstance(payload, PlanningRevisionProposalPayloadV6)
                    else resolve_collaboration_policy(
                        payload.collaboration_policy_request,
                        planner_input.profile_snapshot.collaboration_runtime_binding,
                        subtask_keys=frozenset(item.key for item in payload.subtasks),
                    )
                    if isinstance(payload, PlanningRevisionProposalPayloadV4)
                    else None
                )
                derived_diff = RevisionDiffNormalizer().normalize(
                    base_plan,
                    payload,
                    collaboration_policy=collaboration_policy,
                )
            except (DomainError, TypeError, ValueError):
                return self._failure(
                    PlannerInvalidResult,
                    code="invalid_result",
                    safe_message="fake planner produced an invalid revision proposal",
                    retryable=True,
                    unknown_outcome=False,
                )
            proposal = ProposalRevision.create(
                planning_request_id=planner_input.planning_request_id,
                revision=1,
                brief_revision=planner_input.brief_revision,
                planning_run_id=planner_input.planning_run_id,
                kind=PlanningRequestKind.REVISION,
                payload=payload,
                profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                    planner_input.profile_snapshot
                ),
                profile_snapshot=planner_input.profile_snapshot,
                created_at=control.started_at,
                derived_diff=derived_diff,
            )
            return PlannerProposalResult(
                proposal=replace(
                    proposal,
                    id=uuid5(
                        NAMESPACE_URL,
                        f"sidera.fake.planner:{planner_input.operation_id}:proposal",
                    ),
                )
            )

        legacy_payload = (
            _scale_proposal_payload()
            if self.config.scenario == "scale"
            else _proposal_payload()
        )
        payload = (
            _proposal_payload_v4(legacy_payload, self.config.collaboration_mode)
            if planner_input.proposal_schema_version == 4
            else _proposal_payload_v6(legacy_payload, self.config.collaboration_mode)
            if planner_input.proposal_schema_version == 6
            else legacy_payload
        )
        proposal = ProposalRevision.create(
            planning_request_id=planner_input.planning_request_id,
            revision=1,
            brief_revision=planner_input.brief_revision,
            planning_run_id=planner_input.planning_run_id,
            kind=PlanningRequestKind.CREATE,
            payload=payload,
            profile_descriptor=PlanningProfileDescriptor.from_snapshot(
                planner_input.profile_snapshot
            ),
            profile_snapshot=planner_input.profile_snapshot,
            created_at=control.started_at,
        )
        return PlannerProposalResult(
            proposal=replace(
                proposal,
                id=uuid5(
                    NAMESPACE_URL,
                    f"sidera.fake.planner:{planner_input.operation_id}:proposal",
                ),
            )
        )

    def cancel(self, operation_id: UUID) -> None:
        self._cancelled_operations.add(operation_id)

    def _failure(
        self,
        failure_type: type[PlannerFailure],
        *,
        code: str,
        safe_message: str,
        retryable: bool,
        unknown_outcome: bool,
    ) -> PlannerFailure:
        return failure_type(
            code=code,
            safe_message=safe_message,
            retryable=retryable,
            unknown_outcome=unknown_outcome,
            provider_ref=self.config.provider_ref,
        )


def _proposal_payload() -> PlanningProposalPayload:
    worker = ProposedWorkerItem(
        key="implementation-worker",
        subtask_key="implementation",
        title="Implementation worker",
        role="developer",
        responsibility="Implement the requested change.",
        capability_requirements=("code",),
        worker_policy_ref="policy:standard",
        configuration_ref=None,
        concurrency_limit=1,
        source_refs=("brief:current",),
    )
    return PlanningProposalPayload(
        schema_version=2,
        kind=PlanningRequestKind.CREATE,
        root_task=ProposedRootTask(
            title="Planner generated task",
            goal_summary="Deliver the requested implementation.",
            acceptance_refs=("acceptance:implementation",),
        ),
        subtasks=(
            ProposedSubtask(
                key="implementation",
                kind=WorkItemKind.CHILD,
                title="Implement the request",
                goal_summary="Implement the requested change.",
                acceptance_refs=("acceptance:implementation",),
                expected_outputs=("code",),
                milestone_key=None,
                source_refs=("brief:current",),
            ),
        ),
        worker_items=(worker,),
        subtask_milestones=(),
        subtask_dependencies=(),
        worker_item_dependencies=(),
        execution_intents=(
            ExecutionIntent(
                worker_item_key=worker.key,
                purpose=worker.responsibility,
                context_refs=("context:brief",),
                expected_artifact_refs=("artifact_type:diff",),
                required_evidence_refs=("acceptance:implementation",),
                capability_requirements=worker.capability_requirements,
                adapter_preferences=("fake",),
                budget_class="standard",
                deadline_seconds=60,
                not_before_seconds=None,
                max_attempts=1,
                activation_dependencies=(),
            ),
        ),
        decision_summary=DecisionSummary(
            understanding="Implement the requested change with one worker.",
            assumptions=("The repository context is available.",),
            risks=(),
            estimated_cost_microunits=100,
            estimated_runtime_seconds=60,
            estimated_concurrency=1,
            scope_includes=("requested implementation",),
            scope_excludes=(),
            metadata={},
            estimated_tokens=100,
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="retry_then_block",
        ),
        base_descriptor=None,
    )


def _collaboration_policy_request(
    mode: FakeCollaborationMode,
) -> CollaborationPolicyRequestV1:
    return CollaborationPolicyRequestV1.from_mapping(
        {
            "mode": mode,
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
        }
    )


def _proposal_payload_v4(
    legacy: PlanningProposalPayload,
    mode: FakeCollaborationMode,
) -> PlanningProposalPayloadV4:
    return PlanningProposalPayloadV4(
        schema_version=4,
        kind=legacy.kind,
        root_task=legacy.root_task,
        subtasks=legacy.subtasks,
        worker_items=legacy.worker_items,
        subtask_milestones=legacy.subtask_milestones,
        subtask_dependencies=legacy.subtask_dependencies,
        worker_item_dependencies=legacy.worker_item_dependencies,
        execution_intents=legacy.execution_intents,
        decision_summary=legacy.decision_summary,
        impact_policy=legacy.impact_policy,
        collaboration_policy_request=_collaboration_policy_request(mode),
    )


def _collaboration_policy_request_v2(
    mode: FakeCollaborationMode,
) -> CollaborationPolicyRequestV2:
    return CollaborationPolicyRequestV2.from_mapping(
        {
            "schema_version": 2,
            "mode": mode,
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
            "input_groups": [],
        }
    )


def _integration_spec(worker_keys: tuple[str, ...], integration_worker: str) -> IntegrationSpecV1:
    return IntegrationSpecV1.from_mapping(
        {
            "schema_version": 1,
            "repository_ref": "repo://fake-planner",
            "base_commit": "a" * 40,
            "integration_worker_item_key": integration_worker,
            "input_group_keys": [],
            "criteria": [
                {
                    "key": "implementation",
                    "text": "The implementation is ready",
                    "mode": "automatic",
                    "check_keys": ["unit"],
                }
            ],
            "checks": [
                {
                    "key": "unit",
                    "version": 1,
                    "argv": ["python3", "-m", "unittest"],
                    "cwd": ".",
                    "environment_ref": "env://fake-planner",
                    "prepare_argvs": [],
                    "timeout_seconds": 60,
                    "success_exit_codes": [0],
                    "report_paths": [],
                    "required": True,
                }
            ],
            "verification_binding": {
                "adapter_module_ref": "00000000-0000-0000-0000-000000000010",
                "adapter_config_version": 1,
                "workspace_module_ref": "00000000-0000-0000-0000-000000000011",
                "workspace_config_version": 1,
            },
            "rework_policy": {
                "max_auto_rework_rounds": 2,
                "max_auto_rework_seconds": 1800,
                "allowed_worker_item_keys": list(worker_keys),
            },
            "apply_policy": {
                "apply_on_accept": False,
                "mode": "fast_forward_only",
                "target_ref": None,
            },
            "finalization_policy": "stop_unused",
        }
    )


def _proposal_payload_v6(
    legacy: PlanningProposalPayload,
    mode: FakeCollaborationMode,
) -> PlanningProposalPayloadV6:
    worker_keys = tuple(item.key for item in legacy.worker_items)
    integration_worker = worker_keys[-1]
    return PlanningProposalPayloadV6(
        schema_version=6,
        kind=legacy.kind,
        root_task=legacy.root_task,
        subtasks=legacy.subtasks,
        worker_items=legacy.worker_items,
        subtask_milestones=legacy.subtask_milestones,
        subtask_dependencies=legacy.subtask_dependencies,
        worker_item_dependencies=legacy.worker_item_dependencies,
        execution_intents=legacy.execution_intents,
        decision_summary=legacy.decision_summary,
        impact_policy=legacy.impact_policy,
        collaboration_policy_request=_collaboration_policy_request_v2(mode),
        integration_spec=_integration_spec(worker_keys, integration_worker),
    )


def _revision_proposal_payload_v6(
    legacy: PlanningRevisionProposalPayloadV3,
    mode: FakeCollaborationMode,
) -> PlanningRevisionProposalPayloadV6:
    worker_keys = tuple(item.key for item in legacy.worker_items)
    integration_worker = worker_keys[-1]
    return PlanningRevisionProposalPayloadV6(
        schema_version=6,
        kind=legacy.kind,
        base_descriptor=legacy.base_descriptor,
        root_task=legacy.root_task,
        subtasks=legacy.subtasks,
        worker_items=legacy.worker_items,
        subtask_milestones=legacy.subtask_milestones,
        subtask_dependencies=legacy.subtask_dependencies,
        worker_item_dependencies=legacy.worker_item_dependencies,
        execution_intents=legacy.execution_intents,
        subtask_identity_decisions=legacy.subtask_identity_decisions,
        worker_item_identity_decisions=legacy.worker_item_identity_decisions,
        retired_subtask_ids=legacy.retired_subtask_ids,
        retired_worker_item_ids=legacy.retired_worker_item_ids,
        decision_summary=legacy.decision_summary,
        impact_policy=legacy.impact_policy,
        collaboration_policy_request=_collaboration_policy_request_v2(mode),
        integration_spec=_integration_spec(worker_keys, integration_worker),
    )


def _revision_proposal_payload_v4(
    legacy: PlanningRevisionProposalPayloadV3,
    mode: FakeCollaborationMode,
) -> PlanningRevisionProposalPayloadV4:
    return PlanningRevisionProposalPayloadV4(
        schema_version=4,
        kind=legacy.kind,
        base_descriptor=legacy.base_descriptor,
        root_task=legacy.root_task,
        subtasks=legacy.subtasks,
        worker_items=legacy.worker_items,
        subtask_milestones=legacy.subtask_milestones,
        subtask_dependencies=legacy.subtask_dependencies,
        worker_item_dependencies=legacy.worker_item_dependencies,
        execution_intents=legacy.execution_intents,
        subtask_identity_decisions=legacy.subtask_identity_decisions,
        worker_item_identity_decisions=legacy.worker_item_identity_decisions,
        retired_subtask_ids=legacy.retired_subtask_ids,
        retired_worker_item_ids=legacy.retired_worker_item_ids,
        decision_summary=legacy.decision_summary,
        impact_policy=legacy.impact_policy,
        collaboration_policy_request=_collaboration_policy_request(mode),
    )


def _scale_proposal_payload() -> PlanningProposalPayload:
    count = 200
    subtasks = tuple(
        ProposedSubtask(
            key=f"work-{index:03d}",
            kind=WorkItemKind.CHILD,
            title=f"Scale subtask {index:03d}",
            goal_summary="Exercise the bounded large proposal path.",
            acceptance_refs=("acceptance:scale",),
            expected_outputs=("artifact_type:diff",),
            milestone_key=None,
            source_refs=("brief:current",),
        )
        for index in range(1, count + 1)
    )
    workers = tuple(
        ProposedWorkerItem(
            key=f"scale-worker-{index:03d}",
            subtask_key=f"work-{index:03d}",
            title=f"Scale WorkerItem {index:03d}",
            role="developer",
            responsibility="Exercise the large proposal worker row.",
            capability_requirements=("code",),
            worker_policy_ref="policy:standard",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:current",),
        )
        for index in range(1, count + 1)
    )
    intents = tuple(
        ExecutionIntent(
            worker_item_key=f"scale-worker-{index:03d}",
            purpose="Exercise the large proposal execution intent.",
            context_refs=("context:brief",),
            expected_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=("acceptance:scale",),
            capability_requirements=("code",),
            adapter_preferences=("fake",),
            budget_class="standard",
            deadline_seconds=60,
            not_before_seconds=None,
            max_attempts=1,
            activation_dependencies=(),
        )
        for index in range(1, count + 1)
    )
    subtask_dependencies = tuple(
        SubtaskDependency(
            upstream_subtask_key=f"work-{index:03d}",
            downstream_subtask_key=f"work-{index + 1:03d}",
            kind=DependencyKind.HARD,
            required_evidence_refs=(),
            enforcement=DependencyEnforcement.INFORMATIONAL,
        )
        for index in range(1, count)
    )
    worker_dependencies = tuple(
        WorkerItemDependency(
            upstream_worker_item_key=f"scale-worker-{index:03d}",
            downstream_worker_item_key=f"scale-worker-{index + 1:03d}",
            kind="hard",
            required_artifact_refs=(),
        )
        for index in range(1, count)
    )
    return PlanningProposalPayload(
        schema_version=2,
        kind=PlanningRequestKind.CREATE,
        root_task=ProposedRootTask(
            title="Scale planner generated task",
            goal_summary="Exercise a 200-subtask proposal without unbounded expansion.",
            acceptance_refs=("acceptance:scale",),
        ),
        subtasks=subtasks,
        worker_items=workers,
        subtask_milestones=(),
        subtask_dependencies=subtask_dependencies,
        worker_item_dependencies=worker_dependencies,
        execution_intents=intents,
        decision_summary=DecisionSummary(
            understanding="Exercise a bounded 200-subtask proposal.",
            assumptions=("The large graph is deterministic.",),
            risks=(),
            estimated_cost_microunits=20_000,
            estimated_runtime_seconds=600,
            estimated_concurrency=4,
            scope_includes=("large proposal",),
            scope_excludes=(),
            metadata={"fixture": "scale"},
            estimated_tokens=20_000,
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="retry_then_block",
        ),
        base_descriptor=None,
    )


def _revision_proposal_payload(
    context: RevisionPlanningContextV1,
    base_plan: TaskPlanSnapshot,
) -> PlanningRevisionProposalPayloadV3:
    """Produce a stable revision fixture with a small fan-out/fan-in graph."""
    subtasks = tuple(
        ProposedSubtask(
            key=item.key,
            kind=item.kind,
            title=item.title,
            goal_summary=item.goal_summary,
            acceptance_refs=item.acceptance_refs,
            expected_outputs=item.expected_outputs,
            milestone_key=item.milestone_key,
            source_refs=item.source_refs,
        )
        for item in base_plan.subtasks
    )
    workers = tuple(
        ProposedWorkerItem(
            key=item.key,
            subtask_key=item.subtask_key,
            title=item.title,
            role=item.role,
            responsibility=item.responsibility,
            capability_requirements=item.capability_requirements,
            worker_policy_ref=item.worker_policy_ref,
            configuration_ref=item.configuration_ref,
            concurrency_limit=item.concurrency_limit,
            source_refs=item.source_refs,
        )
        for item in base_plan.worker_item_plans
    )
    fanout = (
        ProposedSubtask(
            key="revision-fan-out-a",
            kind=WorkItemKind.CHILD,
            title="Revision fan-out A",
            goal_summary="Prepare the first independent revision branch.",
            acceptance_refs=("acceptance:revision-fan-out-a",),
            expected_outputs=("artifact_type:diff",),
            milestone_key=None,
            source_refs=("brief:revision",),
        ),
        ProposedSubtask(
            key="revision-fan-out-b",
            kind=WorkItemKind.CHILD,
            title="Revision fan-out B",
            goal_summary="Prepare the second independent revision branch.",
            acceptance_refs=("acceptance:revision-fan-out-b",),
            expected_outputs=("artifact_type:diff",),
            milestone_key=None,
            source_refs=("brief:revision",),
        ),
        ProposedSubtask(
            key="revision-fan-in",
            kind=WorkItemKind.INTEGRATION,
            title="Revision fan-in",
            goal_summary="Integrate the independent revision branches.",
            acceptance_refs=("acceptance:revision-fan-in",),
            expected_outputs=("artifact_type:diff",),
            milestone_key=None,
            source_refs=("brief:revision",),
        ),
    )
    subtasks = (*subtasks, *fanout)
    fanout_workers = (
        ProposedWorkerItem(
            key="revision-worker-a",
            subtask_key="revision-fan-out-a",
            title="Revision worker A",
            role="implementation",
            responsibility="Implement the first independent revision branch.",
            capability_requirements=("code",),
            worker_policy_ref="policy:standard",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:revision",),
        ),
        ProposedWorkerItem(
            key="revision-worker-b",
            subtask_key="revision-fan-out-b",
            title="Revision worker B",
            role="implementation",
            responsibility="Implement the second independent revision branch.",
            capability_requirements=("code",),
            worker_policy_ref="policy:standard",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:revision",),
        ),
        ProposedWorkerItem(
            key="revision-worker-merge",
            subtask_key="revision-fan-in",
            title="Revision integration worker",
            role="integration",
            responsibility="Integrate both revision branches.",
            capability_requirements=("code",),
            worker_policy_ref="policy:standard",
            configuration_ref=None,
            concurrency_limit=1,
            source_refs=("brief:revision",),
        ),
    )
    workers = (*workers, *fanout_workers)
    subtask_dependencies = (
        *base_plan.subtask_dependencies,
        SubtaskDependency(
            upstream_subtask_key="revision-fan-out-a",
            downstream_subtask_key="revision-fan-in",
            kind=DependencyKind.HARD,
            required_evidence_refs=(),
            enforcement=DependencyEnforcement.CONTROL_PLANE,
        ),
        SubtaskDependency(
            upstream_subtask_key="revision-fan-out-b",
            downstream_subtask_key="revision-fan-in",
            kind=DependencyKind.HARD,
            required_evidence_refs=(),
            enforcement=DependencyEnforcement.CONTROL_PLANE,
        ),
    )
    # Cross-branch ordering belongs to the Subtask DAG; WorkerItem edges stay
    # within one Subtask so the worker workspace remains locally scoped.
    worker_dependencies = tuple(base_plan.worker_item_dependencies)
    subtask_by_key = {item.key: item for item in subtasks}
    intents = tuple(
        ExecutionIntent(
            worker_item_key=worker.key,
            purpose=worker.responsibility,
            context_refs=("context:revision",),
            expected_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=subtask_by_key[worker.subtask_key].acceptance_refs,
            capability_requirements=worker.capability_requirements,
            adapter_preferences=("fake",),
            budget_class="standard",
            deadline_seconds=120,
            not_before_seconds=None,
            max_attempts=1,
            activation_dependencies=(),
        )
        for worker in workers
    )
    subtask_identity_decisions = tuple(
        SubtaskIdentityDecision(
            target_subtask_key=item.key,
            strategy=(
                IdentityStrategy.REUSE
                if item.task_id is not None
                else IdentityStrategy.CREATE
            ),
            existing_subtask_task_id=item.task_id,
            derived_from_subtask_task_ids=(),
            reason="preserve the existing subtask identity"
            if item.task_id is not None
            else "create the new revision branch",
        )
        for item in base_plan.subtasks
    ) + tuple(
        SubtaskIdentityDecision(
            target_subtask_key=item.key,
            strategy=IdentityStrategy.CREATE,
            existing_subtask_task_id=None,
            derived_from_subtask_task_ids=(),
            reason="create the new revision branch",
        )
        for item in fanout
    )
    worker_identity_decisions = tuple(
        WorkerItemIdentityDecision(
            target_worker_item_key=item.key,
            strategy=(
                IdentityStrategy.REUSE
                if item.worker_item_id is not None
                else IdentityStrategy.CREATE
            ),
            existing_worker_item_id=item.worker_item_id,
            derived_from_worker_item_ids=(),
            target_subtask_key=item.subtask_key,
            context_disposition=ContextDisposition.UNCHANGED,
            change_reasons=(),
        )
        for item in base_plan.worker_item_plans
    ) + tuple(
        WorkerItemIdentityDecision(
            target_worker_item_key=item.key,
            strategy=IdentityStrategy.CREATE,
            existing_worker_item_id=None,
            derived_from_worker_item_ids=(),
            target_subtask_key=item.subtask_key,
            context_disposition=ContextDisposition.UNCHANGED,
            change_reasons=(),
        )
        for item in fanout_workers
    )
    return PlanningRevisionProposalPayloadV3(
        schema_version=3,
        kind=PlanningRequestKind.REVISION,
        base_descriptor=context.base_descriptor,
        root_task=ProposedRootTask(
            title=base_plan.title,
            goal_summary=base_plan.goal_summary,
            acceptance_refs=base_plan.acceptance_refs,
        ),
        subtasks=subtasks,
        worker_items=workers,
        subtask_milestones=tuple(base_plan.subtask_milestones),
        subtask_dependencies=subtask_dependencies,
        worker_item_dependencies=worker_dependencies,
        execution_intents=intents,
        subtask_identity_decisions=subtask_identity_decisions,
        worker_item_identity_decisions=worker_identity_decisions,
        retired_subtask_ids=(),
        retired_worker_item_ids=(),
        decision_summary=DecisionSummary(
            understanding="Preserve the active plan and add a deterministic fan-out/fan-in revision branch.",
            assumptions=("The exact base snapshot remains authoritative.",),
            risks=("The integration worker depends on both new branches.",),
            estimated_cost_microunits=300,
            estimated_runtime_seconds=120,
            estimated_concurrency=2,
            scope_includes=("revision branches", "revision integration"),
            scope_excludes=(),
            metadata={"fixture": "revision-fan-out-fan-in"},
            estimated_tokens=500,
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block_affected_branch",
            revision_operation="revise",
        ),
    )


def _revision_control_room_payload(
    context: RevisionPlanningContextV1,
    base_plan: TaskPlanSnapshot,
) -> PlanningRevisionProposalPayloadV3:
    """Build the deterministic preserve/patch/create/retire release fixture."""
    base_subtasks = {item.key: item for item in base_plan.subtasks}
    base_workers = {item.key: item for item in base_plan.worker_item_plans}
    target_subtask_keys = ("subtask-a", "subtask-b", "subtask-c", "subtask-e")

    def target_subtask(key: str) -> ProposedSubtask:
        if key == "subtask-e":
            return ProposedSubtask(
                key=key,
                kind=WorkItemKind.CHILD,
                title="Subtask E",
                goal_summary="Add the revised verification branch.",
                acceptance_refs=("acceptance:subtask-e",),
                expected_outputs=("artifact_type:diff",),
                milestone_key=None,
                source_refs=("brief:current", "revision:control-room"),
            )
        item = base_subtasks[key]
        return ProposedSubtask(
            key=item.key,
            kind=item.kind,
            title=item.title,
            goal_summary=item.goal_summary,
            acceptance_refs=item.acceptance_refs,
            expected_outputs=item.expected_outputs,
            milestone_key=item.milestone_key,
            source_refs=item.source_refs,
        )

    def target_worker(key: str) -> ProposedWorkerItem:
        if key == "worker-e":
            return ProposedWorkerItem(
                key=key,
                subtask_key="subtask-e",
                title="Worker E",
                role="developer",
                responsibility="Produce the new verification branch.",
                capability_requirements=("code",),
                worker_policy_ref="policy:r6.1-fake",
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=("brief:current", "revision:control-room"),
            )
        item = base_workers[key]
        return ProposedWorkerItem(
            key=item.key,
            subtask_key=item.subtask_key,
            title=item.title,
            role=item.role,
            responsibility=(
                "Produce the revised branch B evidence."
                if key == "worker-b"
                else item.responsibility
            ),
            capability_requirements=item.capability_requirements,
            worker_policy_ref=item.worker_policy_ref,
            configuration_ref=item.configuration_ref,
            concurrency_limit=item.concurrency_limit,
            source_refs=item.source_refs,
        )

    subtasks = tuple(target_subtask(key) for key in target_subtask_keys)
    workers = tuple(
        target_worker(key) for key in ("worker-a", "worker-b", "worker-c", "worker-e")
    )
    retired_subtask_id = base_subtasks["subtask-d"].task_id
    retired_worker_item_id = base_workers["worker-d"].worker_item_id
    if retired_subtask_id is None or retired_worker_item_id is None:
        raise ValueError("revision fixture requires materialized retired identities")
    subtask_decisions = tuple(
        SubtaskIdentityDecision(
            target_subtask_key=key,
            strategy=IdentityStrategy.REUSE,
            existing_subtask_task_id=base_subtasks[key].task_id,
            derived_from_subtask_task_ids=(),
            reason="preserve the existing subtask identity",
        )
        for key in ("subtask-a", "subtask-b", "subtask-c")
    ) + (
        SubtaskIdentityDecision(
            target_subtask_key="subtask-e",
            strategy=IdentityStrategy.CREATE,
            existing_subtask_task_id=None,
            derived_from_subtask_task_ids=(),
            reason="new verification branch",
        ),
    )
    worker_decisions = (
        _reuse_worker_decision(
            key="worker-a",
            worker=base_workers["worker-a"],
            target_subtask_key="subtask-a",
            disposition=ContextDisposition.UNCHANGED,
            reasons=("stable parallel branch",),
        ),
        _reuse_worker_decision(
            key="worker-b",
            worker=base_workers["worker-b"],
            target_subtask_key="subtask-b",
            disposition=ContextDisposition.PATCH_REQUIRED,
            reasons=("updated branch requirement",),
        ),
        _reuse_worker_decision(
            key="worker-c",
            worker=base_workers["worker-c"],
            target_subtask_key="subtask-c",
            disposition=ContextDisposition.UNCHANGED,
            reasons=("preserve fan-in branch",),
        ),
        WorkerItemIdentityDecision(
            target_worker_item_key="worker-e",
            strategy=IdentityStrategy.CREATE,
            existing_worker_item_id=None,
            derived_from_worker_item_ids=(),
            target_subtask_key="subtask-e",
            context_disposition=ContextDisposition.UNCHANGED,
            change_reasons=("new verification branch",),
        ),
    )
    intents = tuple(
        ExecutionIntent(
            worker_item_key=worker.key,
            purpose=worker.responsibility,
            context_refs=("context:running-plan",),
            expected_artifact_refs=("artifact_type:diff",),
            required_evidence_refs=(
                f"acceptance:{worker.subtask_key.removeprefix('subtask-')}",
            ),
            capability_requirements=worker.capability_requirements,
            adapter_preferences=("fake",),
            budget_class="standard",
            deadline_seconds=60,
            not_before_seconds=None,
            max_attempts=2,
            activation_dependencies=(
                ActivationDependency(
                    upstream_worker_item_key="worker-a",
                    downstream_worker_item_key="worker-c",
                    kind="hard",
                    required_artifact_refs=("artifact_type:diff",),
                ),
                ActivationDependency(
                    upstream_worker_item_key="worker-b",
                    downstream_worker_item_key="worker-c",
                    kind="hard",
                    required_artifact_refs=("artifact_type:diff",),
                ),
            )
            if worker.key == "worker-c"
            else (),
        )
        for worker in workers
    )
    return PlanningRevisionProposalPayloadV3(
        schema_version=3,
        kind=PlanningRequestKind.REVISION,
        base_descriptor=context.base_descriptor,
        root_task=ProposedRootTask(
            title=base_plan.title,
            goal_summary=base_plan.goal_summary,
            acceptance_refs=base_plan.acceptance_refs,
        ),
        subtasks=subtasks,
        worker_items=workers,
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="subtask-a",
                downstream_subtask_key="subtask-c",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="subtask-b",
                downstream_subtask_key="subtask-c",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
        execution_intents=intents,
        subtask_identity_decisions=subtask_decisions,
        worker_item_identity_decisions=worker_decisions,
        retired_subtask_ids=(retired_subtask_id,),
        retired_worker_item_ids=(retired_worker_item_id,),
        decision_summary=DecisionSummary(
            understanding="Preserve the running graph and revise one branch.",
            assumptions=("A and B remain parallel.",),
            risks=("C remains blocked until both upstream branches provide evidence.",),
            estimated_cost_microunits=400,
            estimated_runtime_seconds=120,
            estimated_concurrency=2,
            scope_includes=("worker-b patch", "worker-e creation"),
            scope_excludes=("worker-a replacement",),
            metadata={"fixture": "r6.1-control-room"},
            estimated_tokens=400,
        ),
        impact_policy=ImpactPolicy(
            activate_immediately=True,
            budget_level="standard",
            failure_policy="block_affected_branch",
            revision_operation="revise",
        ),
    )


def _reuse_worker_decision(
    *,
    key: str,
    worker: WorkerItemPlanItem,
    target_subtask_key: str,
    disposition: ContextDisposition,
    reasons: tuple[str, ...],
) -> WorkerItemIdentityDecision:
    return WorkerItemIdentityDecision(
        target_worker_item_key=key,
        strategy=IdentityStrategy.REUSE,
        existing_worker_item_id=worker.worker_item_id,
        derived_from_worker_item_ids=(),
        target_subtask_key=target_subtask_key,
        context_disposition=disposition,
        change_reasons=reasons,
    )
