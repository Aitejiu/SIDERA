from __future__ import annotations

from dataclasses import dataclass, replace
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
from sidera_control_plane.domain.modules import (
    CancellationCapability,
    IdempotencyCapability,
    PlannerAdapterCapabilityProfile,
    StabilityLevel,
    StructuredOutputCapability,
    UsageReportingCapability,
)
from sidera_control_plane.domain.planning import (
    PlanningClarification,
    PlanningRequestKind,
)
from sidera_control_plane.domain.planning_proposals import (
    DecisionSummary,
    ExecutionIntent,
    ImpactPolicy,
    PlanningProfileDescriptor,
    PlanningProposalPayload,
    ProposalRevision,
    ProposedRootTask,
    ProposedSubtask,
    ProposedWorkerItem,
)
from sidera_control_plane.domain.plans import WorkItemKind

FakeScenario = Literal[
    "proposal",
    "clarification",
    "invalid_schema",
    "timeout",
    "unknown_outcome",
]


@dataclass(frozen=True, slots=True)
class FakePlannerConfig:
    scenario: FakeScenario
    provider_ref: str = "fake-planner"

    def __post_init__(self) -> None:
        if self.scenario not in {
            "proposal",
            "clarification",
            "invalid_schema",
            "timeout",
            "unknown_outcome",
        }:
            raise ValueError(f"unsupported fake planner scenario: {self.scenario}")
        if not self.provider_ref.strip():
            raise ValueError("fake planner provider_ref is required")


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
        proposal_schema_versions=(2,),
    )

    def __init__(self, config: FakePlannerConfig) -> None:
        self.config = config
        self._cancelled_operations: set[UUID] = set()

    @property
    def cancelled_operations(self) -> frozenset[UUID]:
        return frozenset(self._cancelled_operations)

    def probe(self, config: dict[str, object]) -> PlannerAdapterCapabilityProfile:
        configured_scenario = config.get("scenario")
        if configured_scenario is not None and configured_scenario != self.config.scenario:
            raise ValueError("fake planner scenario must be explicitly configured")
        return self._CAPABILITIES

    def plan(self, planner_input: PlannerInput, control: PlannerRunControl) -> PlannerResult:
        if control.operation_id != planner_input.operation_id:
            return self._failure(
                PlannerExplicitFailure,
                code="operation_id_mismatch",
                safe_message="planner operation id does not match input",
                retryable=False,
                unknown_outcome=False,
            )
        if control.cancel_requested or control.operation_id in self._cancelled_operations:
            return self._failure(
                PlannerExplicitFailure,
                code="cancelled",
                safe_message="planner operation was cancelled",
                retryable=False,
                unknown_outcome=False,
            )
        if self.config.scenario == "clarification" and not planner_input.brief.user_feedback:
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
        proposal = ProposalRevision.create(
            planning_request_id=planner_input.planning_request_id,
            revision=1,
            brief_revision=planner_input.brief_revision,
            planning_run_id=planner_input.planning_run_id,
            kind=PlanningRequestKind.CREATE,
            payload=_proposal_payload(),
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
