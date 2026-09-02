from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.collaboration_projections import (
    CollaborationExecutionProjectionFact,
    CollaborationProjectionFacts,
    CollaborationProjectionService,
    CollaborationRequestDependencyProjectionFact,
    CollaborationRequestProjectionFact,
    CollaborationRunProjectionFact,
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
    WorkItemKind,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
UPSTREAM_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
DOWNSTREAM_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
UPSTREAM_WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")
DOWNSTREAM_WORKER_ID = UUID("40000000-0000-0000-0000-000000000002")
UPSTREAM_REQUEST_ID = UUID("50000000-0000-0000-0000-000000000001")
DOWNSTREAM_REQUEST_ID = UUID("50000000-0000-0000-0000-000000000002")
RUN_ID = UUID("60000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("70000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _plan() -> CanonicalTaskPlan:
    return CanonicalTaskPlan(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        schema_version=3,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=4,
        title="支付重构",
        goal_summary="完成支付链路重构",
        acceptance_refs=("acceptance:root",),
        subtasks=(
            SubtaskPlanItem(
                key="payment-implementation",
                task_id=UPSTREAM_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="支付实现",
                goal_summary="实现支付核心逻辑",
                acceptance_refs=("acceptance:implementation",),
                milestone_key=None,
                expected_outputs=("payment patch",),
                source_refs=(),
            ),
            SubtaskPlanItem(
                key="payment-callback",
                task_id=DOWNSTREAM_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="验证支付回调",
                goal_summary="验证支付回调链路",
                acceptance_refs=("acceptance:callback",),
                milestone_key=None,
                expected_outputs=("callback report",),
                source_refs=(),
            ),
        ),
        worker_items=(
            CanonicalWorkerItemPlan(
                key="payment-implementer",
                worker_item_id=UPSTREAM_WORKER_ID,
                legacy_identity_key=None,
                subtask_key="payment-implementation",
                title="支付实现 Worker",
                role="implementer",
                responsibility="实现支付核心逻辑",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=(),
            ),
            CanonicalWorkerItemPlan(
                key="callback-verifier",
                worker_item_id=DOWNSTREAM_WORKER_ID,
                legacy_identity_key=None,
                subtask_key="payment-callback",
                title="回调验证 Worker",
                role="reviewer",
                responsibility="验证支付回调链路",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=(),
            ),
        ),
        milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="payment-implementation",
                downstream_subtask_key="payment-callback",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(),
    )


def _facts(*, run_status: str = "active") -> CollaborationProjectionFacts:
    plan = _plan()
    return CollaborationProjectionFacts(
        plan=plan,
        run=CollaborationRunProjectionFact(
            id=RUN_ID,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            plan_snapshot_id=plan.snapshot_id,
            plan_generation=plan.generation,
            status=run_status,
            provider="reference",
            interface_version="collaboration-runtime.v1",
            config_version=3,
            strategy_label="all_required",
            policy_enabled=True,
            blocked_reason="runtime_output_invalid" if run_status == "blocked" else None,
            updated_at=NOW,
        ),
        requests=(
            CollaborationRequestProjectionFact(
                id=UPSTREAM_REQUEST_ID,
                subtask_key="payment-implementation",
                worker_item_key="payment-implementer",
                worker_item_id=UPSTREAM_WORKER_ID,
                status="materialized",
                collaboration_gate_mode="release_required",
                gate_reason="released",
                version=2,
                expected_artifact_refs=(),
                expected_evidence_refs=(),
                updated_at=NOW,
            ),
            CollaborationRequestProjectionFact(
                id=DOWNSTREAM_REQUEST_ID,
                subtask_key="payment-callback",
                worker_item_key="callback-verifier",
                worker_item_id=DOWNSTREAM_WORKER_ID,
                status="blocked",
                collaboration_gate_mode="release_required",
                gate_reason="waiting_evidence",
                version=1,
                expected_artifact_refs=(),
                expected_evidence_refs=(),
                updated_at=NOW,
            ),
        ),
        dependencies=(
            CollaborationRequestDependencyProjectionFact(
                downstream_request_id=DOWNSTREAM_REQUEST_ID,
                upstream_request_id=UPSTREAM_REQUEST_ID,
                enforcement="hard",
                required_artifact_refs=("artifact_type:report",),
                required_evidence_refs=("acceptance:implementation",),
            ),
        ),
        executions=(
            CollaborationExecutionProjectionFact(
                request_id=UPSTREAM_REQUEST_ID,
                execution_id=EXECUTION_ID,
                lineage_id=EXECUTION_ID,
                status="succeeded",
                attempt=1,
                failure_code=None,
                started_at=NOW,
                finished_at=NOW,
                updated_at=NOW,
                artifact_refs=("artifact_type:report",),
                evidence_refs=(),
            ),
        ),
        released_request_ids=(UPSTREAM_REQUEST_ID,),
        decisions=(),
        intents=(),
        applications=(),
    )


def test_root_summary_names_the_blocked_subtask_not_runtime_ids() -> None:
    facts = _facts(run_status="blocked")

    summary = CollaborationProjectionService().root_summary(facts)

    assert summary.status == "attention"
    assert summary.blocked_subtask_count == 1
    assert summary.blocked_subtasks[0].title == "验证支付回调"
    assert summary.detail_href.endswith(f"/tasks/{ROOT_TASK_ID}/collaboration")
    assert str(RUN_ID) not in summary.blocked_subtasks[0].reason


def test_subtask_summary_exposes_fan_in_and_worker_waiting_state() -> None:
    facts = _facts()

    summary = CollaborationProjectionService().subtask_summary(
        facts, subtask_key="payment-callback"
    )
    worker = CollaborationProjectionService().worker_summary(
        facts, worker_item_id=DOWNSTREAM_WORKER_ID
    )

    assert summary is not None
    assert summary.required_input_count == 1
    assert summary.satisfied_input_count == 0
    assert summary.waiting_upstream_subtasks[0].title == "支付实现"
    assert summary.missing_evidence_refs == ("acceptance:implementation",)
    assert worker is not None
    assert worker.state == "waiting_evidence"
    assert worker.upstream_links[0].href.endswith("/subtasks/payment-implementation")
