from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.plan_convergence_projections import (
    PlanConvergenceFacts,
    PlanConvergenceProjectionService,
    PlanHeadProjection,
    PlanRevisionProjection,
    PlanTransitionProjectionFact,
    WorkerTransitionProjectionFact,
)
from sidera_control_plane.domain.plan_normalization import (
    CanonicalTaskPlan,
    CanonicalWorkerItemPlan,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskPlanItem,
    WorkerItemDependency,
    WorkItemKind,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
BACKEND_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
MIGRATION_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
BACKEND_WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")
MIGRATION_WORKER_ID = UUID("40000000-0000-0000-0000-000000000002")
PLAN_TRANSITION_ID = UUID("50000000-0000-0000-0000-000000000001")
BACKEND_TRANSITION_ID = UUID("60000000-0000-0000-0000-000000000001")
MIGRATION_TRANSITION_ID = UUID("60000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _plan() -> CanonicalTaskPlan:
    return CanonicalTaskPlan(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=3,
        title="支付平台",
        goal_summary="完成支付平台迁移",
        acceptance_refs=(),
        subtasks=(
            SubtaskPlanItem(
                key="backend",
                task_id=BACKEND_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="Backend",
                goal_summary="更新后端",
                acceptance_refs=(),
                milestone_key=None,
                expected_outputs=(),
                source_refs=(),
            ),
            SubtaskPlanItem(
                key="migration",
                task_id=MIGRATION_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="Migration",
                goal_summary="执行迁移",
                acceptance_refs=(),
                milestone_key=None,
                expected_outputs=(),
                source_refs=(),
            ),
        ),
        worker_items=(
            CanonicalWorkerItemPlan(
                key="backend-worker",
                worker_item_id=BACKEND_WORKER_ID,
                legacy_identity_key=None,
                subtask_key="backend",
                title="Backend worker",
                role="implementer",
                responsibility="更新后端",
                capability_requirements=(),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=(),
            ),
            CanonicalWorkerItemPlan(
                key="migration-worker",
                worker_item_id=MIGRATION_WORKER_ID,
                legacy_identity_key=None,
                subtask_key="migration",
                title="Migration worker",
                role="implementer",
                responsibility="执行迁移",
                capability_requirements=(),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=(),
            ),
        ),
        milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="backend",
                downstream_subtask_key="migration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(
            WorkerItemDependency(
                upstream_worker_item_key="backend-worker",
                downstream_worker_item_key="migration-worker",
                kind="handoff",
                required_artifact_refs=(),
            ),
        ),
    )


def _facts() -> PlanConvergenceFacts:
    return PlanConvergenceFacts(
        head=PlanHeadProjection(
            active_snapshot_id=UUID("10000000-0000-0000-0000-000000000002"),
            active_generation=4,
            active_snapshot_hash="a" * 64,
            version=2,
            updated_at=NOW,
        ),
        revision=PlanRevisionProjection(
            planning_request_id=UUID("70000000-0000-0000-0000-000000000001"),
            status="proposal_ready",
            proposal_revision=2,
            proposal_content_hash="b" * 64,
            base_generation=3,
            target_generation=4,
            version=3,
            correlation_id=UUID("80000000-0000-0000-0000-000000000001"),
            updated_at=NOW,
            subtask_path=("Backend", "Migration"),
            summary="新的计划方案已就绪",
            action="review_revision",
        ),
        transition=PlanTransitionProjectionFact(
            id=PLAN_TRANSITION_ID,
            from_generation=3,
            to_generation=4,
            status="degraded",
            started_at=NOW,
            completed_at=None,
            version=2,
        ),
        workers=(
            WorkerTransitionProjectionFact(
                id=BACKEND_TRANSITION_ID,
                plan_transition_id=PLAN_TRANSITION_ID,
                worker_item_id=BACKEND_WORKER_ID,
                subtask_key="backend",
                action="patch",
                status="blocked_unknown",
                writer_state="unknown",
                target_context_version=4,
                current_context_version=4,
                acknowledged_context_version=4,
                patch_id=UUID("90000000-0000-0000-0000-000000000001"),
                patch_acknowledged=True,
                target_execution_id=None,
                target_session_id=None,
                queued_input_ids=(),
                superseded_input_ids=(),
                failure_code=None,
                started_at=NOW,
                completed_at=NOW,
                version=2,
            ),
            WorkerTransitionProjectionFact(
                id=MIGRATION_TRANSITION_ID,
                plan_transition_id=PLAN_TRANSITION_ID,
                worker_item_id=MIGRATION_WORKER_ID,
                subtask_key="migration",
                action="continue",
                status="blocked_unknown",
                writer_state="unknown",
                target_context_version=5,
                current_context_version=4,
                acknowledged_context_version=4,
                patch_id=None,
                patch_acknowledged=False,
                target_execution_id=UUID("a0000000-0000-0000-0000-000000000001"),
                target_session_id=None,
                queued_input_ids=(),
                superseded_input_ids=(
                    UUID("b0000000-0000-0000-0000-000000000001"),
                ),
                failure_code="gateway_outcome_unknown",
                started_at=NOW,
                completed_at=None,
                version=4,
            ),
        ),
    )


def test_root_projection_exposes_revision_attention_and_convergence() -> None:
    projection = PlanConvergenceProjectionService().compose_root(_plan(), _facts())

    assert projection.revision_attention is not None
    assert projection.revision_attention.subtask_path == ("Backend", "Migration")
    assert projection.convergence is not None
    assert projection.convergence.target_generation == 4
    assert projection.convergence.converged_count == 0
    assert projection.convergence.unknown_count == 2
    assert projection.convergence.confirmation_blocked is True


def test_subtask_projection_reports_blocked_predecessor_without_worker_id() -> None:
    projection = PlanConvergenceProjectionService().compose_subtask(
        _plan(), "migration", _facts()
    )

    assert projection.status == "blocked_unknown"
    assert projection.blocked_predecessor_paths == (("Backend",),)


def test_worker_projection_keeps_transition_separate_from_trace() -> None:
    projection = PlanConvergenceProjectionService().compose_worker(
        _plan(), MIGRATION_WORKER_ID, _facts()
    )

    assert projection is not None
    assert projection.target_generation == 4
    assert projection.status == "blocked_unknown"
    assert projection.continuation_execution_id == UUID(
        "a0000000-0000-0000-0000-000000000001"
    )
    assert projection.superseded_input_ids == (
        UUID("b0000000-0000-0000-0000-000000000001"),
    )
    assert projection.trace_href is None
