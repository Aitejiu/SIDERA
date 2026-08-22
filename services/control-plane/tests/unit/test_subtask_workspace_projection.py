from datetime import UTC, datetime
from uuid import UUID

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
from sidera_control_plane.domain.subtask_projections import (
    SubtaskActivityFact,
    SubtaskArtifactFact,
    SubtaskCheckpointFact,
    SubtaskEvidenceFact,
    SubtaskExecutionFact,
    SubtaskPlacementFact,
    SubtaskSessionFact,
    SubtaskTaskFact,
    SubtaskWorkerFact,
    SubtaskWorkerItemFact,
    SubtaskWorkspaceProjectionService,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
CALLBACK_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
INTEGRATION_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
IMPLEMENTER_ID = UUID("40000000-0000-0000-0000-000000000001")
REVIEWER_ID = UUID("40000000-0000-0000-0000-000000000002")
INTEGRATOR_ID = UUID("40000000-0000-0000-0000-000000000003")
EXECUTION_ID = UUID("50000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("60000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("70000000-0000-0000-0000-000000000001")
ARTIFACT_ID = UUID("80000000-0000-0000-0000-000000000001")
EVIDENCE_ID = UUID("80000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _plan() -> CanonicalTaskPlan:
    return CanonicalTaskPlan(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
        schema_version=2,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        generation=3,
        title="支付重构",
        goal_summary="完成支付回调链路重构",
        acceptance_refs=("acceptance:root",),
        subtasks=(
            SubtaskPlanItem(
                key="callback-idempotency",
                task_id=CALLBACK_TASK_ID,
                kind=WorkItemKind.CHILD,
                title="回调幂等",
                goal_summary="保证支付回调可安全重试",
                acceptance_refs=("acceptance:callback",),
                milestone_key="implementation",
                expected_outputs=("callback patch",),
                source_refs=("planner:callback",),
            ),
            SubtaskPlanItem(
                key="integration",
                task_id=INTEGRATION_TASK_ID,
                kind=WorkItemKind.INTEGRATION,
                title="支付集成",
                goal_summary="完成支付链路集成验证",
                acceptance_refs=("acceptance:integration",),
                milestone_key="verification",
                expected_outputs=("integration report",),
                source_refs=("planner:integration",),
            ),
        ),
        worker_items=(
            CanonicalWorkerItemPlan(
                key="callback-implementer",
                worker_item_id=IMPLEMENTER_ID,
                legacy_identity_key=None,
                subtask_key="callback-idempotency",
                title="回调实现",
                role="implementer",
                responsibility="实现回调幂等逻辑",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=(),
            ),
            CanonicalWorkerItemPlan(
                key="callback-reviewer",
                worker_item_id=REVIEWER_ID,
                legacy_identity_key=None,
                subtask_key="callback-idempotency",
                title="回调复核",
                role="reviewer",
                responsibility="确认重试规则和验收证据",
                capability_requirements=("python",),
                worker_policy_ref=None,
                configuration_ref=None,
                concurrency_limit=1,
                source_refs=(),
            ),
            CanonicalWorkerItemPlan(
                key="payment-integrator",
                worker_item_id=INTEGRATOR_ID,
                legacy_identity_key=None,
                subtask_key="integration",
                title="支付集成",
                role="integrator",
                responsibility="执行支付链路集成验证",
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
                upstream_subtask_key="callback-idempotency",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=(),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(
            WorkerItemDependency(
                upstream_worker_item_key="callback-implementer",
                downstream_worker_item_key="callback-reviewer",
                kind="review",
                required_artifact_refs=("report",),
            ),
        ),
    )


def _tasks() -> dict[UUID, SubtaskTaskFact]:
    return {
        CALLBACK_TASK_ID: SubtaskTaskFact(
            id=CALLBACK_TASK_ID,
            title="回调幂等",
            status="running",
            version=4,
            updated_at=NOW,
        ),
        INTEGRATION_TASK_ID: SubtaskTaskFact(
            id=INTEGRATION_TASK_ID,
            title="支付集成",
            status="pending",
            version=1,
            updated_at=NOW,
        ),
    }


def _worker_items() -> dict[UUID, SubtaskWorkerItemFact]:
    return {
        IMPLEMENTER_ID: SubtaskWorkerItemFact(
            id=IMPLEMENTER_ID,
            stable_key="callback-implementer",
            title="回调实现",
            responsibility="实现回调幂等逻辑",
            status="active",
            context_version=2,
            updated_at=NOW,
        ),
        REVIEWER_ID: SubtaskWorkerItemFact(
            id=REVIEWER_ID,
            stable_key="callback-reviewer",
            title="回调复核",
            responsibility="确认重试规则和验收证据",
            status="active",
            context_version=1,
            updated_at=NOW,
        ),
        INTEGRATOR_ID: SubtaskWorkerItemFact(
            id=INTEGRATOR_ID,
            stable_key="payment-integrator",
            title="支付集成",
            responsibility="执行支付链路集成验证",
            status="active",
            context_version=1,
            updated_at=NOW,
        ),
    }


def _placements() -> tuple[SubtaskPlacementFact, ...]:
    return tuple(
        SubtaskPlacementFact(
            plan_snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
            plan_generation=3,
            worker_item_id=item_id,
            worker_item_key=worker_item_key,
            subtask_key=subtask_key,
            subtask_task_id=task_id,
            role=role,
        )
        for item_id, worker_item_key, subtask_key, task_id, role in (
            (
                IMPLEMENTER_ID,
                "callback-implementer",
                "callback-idempotency",
                CALLBACK_TASK_ID,
                "implementer",
            ),
            (
                REVIEWER_ID,
                "callback-reviewer",
                "callback-idempotency",
                CALLBACK_TASK_ID,
                "reviewer",
            ),
            (
                INTEGRATOR_ID,
                "payment-integrator",
                "integration",
                INTEGRATION_TASK_ID,
                "integrator",
            ),
        )
    )


def test_subtask_workspace_contains_only_local_worker_items() -> None:
    workspace = SubtaskWorkspaceProjectionService().compose(
        _plan(),
        subtask_key="callback-idempotency",
        tasks=_tasks(),
        worker_items=_worker_items(),
        placements=_placements(),
        executions=(
            SubtaskExecutionFact(
                id=EXECUTION_ID,
                task_id=CALLBACK_TASK_ID,
                worker_item_id=IMPLEMENTER_ID,
                status="running",
                updated_at=NOW,
                session_id=SESSION_ID,
                assigned_worker_id=WORKER_ID,
            ),
        ),
        sessions=(
            SubtaskSessionFact(
                id=SESSION_ID,
                execution_id=EXECUTION_ID,
                worker_id=WORKER_ID,
                status="active",
                last_activity_at=NOW,
                version=4,
            ),
        ),
        workers=(
            SubtaskWorkerFact(
                id=WORKER_ID,
                display_name="OpenCode callback worker",
                adapter_type="opencode",
            ),
        ),
        artifacts=(
            SubtaskArtifactFact(
                id=ARTIFACT_ID,
                task_id=CALLBACK_TASK_ID,
                execution_id=EXECUTION_ID,
                artifact_type="report",
                media_type="text/plain",
                size_bytes=256,
                content_available=True,
            ),
        ),
        evidence=(
            SubtaskEvidenceFact(
                id=EVIDENCE_ID,
                task_id=CALLBACK_TASK_ID,
                execution_id=EXECUTION_ID,
                acceptance_ref="acceptance:callback",
                conclusion="回调测试证据",
                artifact_ids=(ARTIFACT_ID,),
            ),
        ),
        checkpoints=(
            SubtaskCheckpointFact(
                execution_id=EXECUTION_ID,
                sequence=3,
                title="回调测试完成",
                artifact_ids=(ARTIFACT_ID,),
                occurred_at=NOW,
            ),
        ),
        activity=(
            SubtaskActivityFact(
                id=UUID("90000000-0000-0000-0000-000000000001"),
                task_id=CALLBACK_TASK_ID,
                worker_item_id=IMPLEMENTER_ID,
                occurred_at=NOW,
                kind="assistant.summary",
                summary="运行回调测试",
                correlation_id=UUID("a0000000-0000-0000-0000-000000000001"),
            ),
        ),
        last_change_cursor=19,
        as_of=NOW,
    )

    assert workspace.subtask.key == "callback-idempotency"
    assert {item.worker_item_id for item in workspace.worker_items} == {
        IMPLEMENTER_ID,
        REVIEWER_ID,
    }
    assert workspace.upstream_subtasks == ()
    assert workspace.downstream_subtasks[0].subtask_key == "integration"
    assert workspace.worker_graph.edges[0].kind == "review"
    assert all(
        edge.upstream_worker_item_id in {IMPLEMENTER_ID, REVIEWER_ID}
        and edge.downstream_worker_item_id in {IMPLEMENTER_ID, REVIEWER_ID}
        for edge in workspace.worker_graph.edges
    )
    assert workspace.worker_items[0].current_step == "运行回调测试"
    assert workspace.worker_items[0].current_agent_summary == "OpenCode callback worker"
    assert workspace.artifacts[0].artifact_id == ARTIFACT_ID
    assert workspace.evidence[0].acceptance_ref == "acceptance:callback"
    assert workspace.checkpoints[0].sequence == 3
    assert workspace.attention_items == ()
    assert workspace.recent_activity[0].summary == "运行回调测试"
    assert not hasattr(workspace, "trace_events")
    assert not hasattr(workspace, "intervention_composer_uri")
