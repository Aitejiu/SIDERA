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
    WorkItemKind,
)
from sidera_control_plane.domain.task_console_projections import (
    TaskConsoleActivityFact,
    TaskConsoleEvidenceFact,
    TaskConsoleExecutionFact,
    TaskConsolePlacementFact,
    TaskConsoleProjectionService,
    TaskConsoleTaskFact,
    TaskConsoleWorkerItemFact,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
CALLBACK_TASK_ID = UUID("30000000-0000-0000-0000-000000000002")
INTEGRATION_TASK_ID = UUID("30000000-0000-0000-0000-000000000003")
IMPLEMENTER_ID = UUID("40000000-0000-0000-0000-000000000001")
REVIEWER_ID = UUID("40000000-0000-0000-0000-000000000002")
INTEGRATOR_ID = UUID("40000000-0000-0000-0000-000000000003")
RUNNING_EXECUTION_ID = UUID("50000000-0000-0000-0000-000000000001")
WAITING_EXECUTION_ID = UUID("50000000-0000-0000-0000-000000000002")
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
                source_refs=("planner:callback-implementer",),
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
                source_refs=("planner:callback-reviewer",),
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
                source_refs=("planner:payment-integrator",),
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
        worker_item_dependencies=(),
    )


def _tasks() -> dict[UUID, TaskConsoleTaskFact]:
    return {
        CALLBACK_TASK_ID: TaskConsoleTaskFact(
            id=CALLBACK_TASK_ID,
            title="回调幂等",
            status="running",
            version=4,
            updated_at=NOW,
        ),
        INTEGRATION_TASK_ID: TaskConsoleTaskFact(
            id=INTEGRATION_TASK_ID,
            title="支付集成",
            status="pending",
            version=1,
            updated_at=NOW,
        ),
    }


def _worker_items() -> dict[UUID, TaskConsoleWorkerItemFact]:
    return {
        item_id: TaskConsoleWorkerItemFact(
            id=item_id,
            stable_key=stable_key,
            title=title,
            responsibility=responsibility,
            status="active",
            context_version=1,
            updated_at=NOW,
        )
        for item_id, stable_key, title, responsibility in (
            (
                IMPLEMENTER_ID,
                "callback-implementer",
                "回调实现",
                "实现回调幂等逻辑",
            ),
            (
                REVIEWER_ID,
                "callback-reviewer",
                "回调复核",
                "确认重试规则和验收证据",
            ),
            (
                INTEGRATOR_ID,
                "payment-integrator",
                "支付集成",
                "执行支付链路集成验证",
            ),
        )
    }


def _placements() -> tuple[TaskConsolePlacementFact, ...]:
    return tuple(
        TaskConsolePlacementFact(
            plan_snapshot_id=UUID("10000000-0000-0000-0000-000000000001"),
            plan_generation=3,
            worker_item_id=item_id,
            worker_item_key=worker_item_key,
            subtask_key=subtask_key,
            subtask_task_id=task_id,
        )
        for item_id, worker_item_key, subtask_key, task_id in (
            (
                IMPLEMENTER_ID,
                "callback-implementer",
                "callback-idempotency",
                CALLBACK_TASK_ID,
            ),
            (
                REVIEWER_ID,
                "callback-reviewer",
                "callback-idempotency",
                CALLBACK_TASK_ID,
            ),
            (INTEGRATOR_ID, "payment-integrator", "integration", INTEGRATION_TASK_ID),
        )
    )


def test_task_graph_projects_one_node_per_subtask() -> None:
    graph = TaskConsoleProjectionService().compose(
        _plan(),
        tasks=_tasks(),
        worker_items=_worker_items(),
        placements=_placements(),
        executions=(
            TaskConsoleExecutionFact(
                id=RUNNING_EXECUTION_ID,
                task_id=CALLBACK_TASK_ID,
                worker_item_id=IMPLEMENTER_ID,
                status="running",
                updated_at=NOW,
            ),
            TaskConsoleExecutionFact(
                id=WAITING_EXECUTION_ID,
                task_id=CALLBACK_TASK_ID,
                worker_item_id=REVIEWER_ID,
                status="waiting_user",
                updated_at=NOW,
            ),
        ),
        evidence=(
            TaskConsoleEvidenceFact(
                id=UUID("60000000-0000-0000-0000-000000000001"),
                task_id=CALLBACK_TASK_ID,
                acceptance_ref="acceptance:callback",
            ),
        ),
        activity=(
            TaskConsoleActivityFact(
                id=UUID("70000000-0000-0000-0000-000000000001"),
                task_id=CALLBACK_TASK_ID,
                worker_item_id=REVIEWER_ID,
                occurred_at=NOW,
                kind="input.requested",
                summary="等待确认重试规则",
                correlation_id=UUID("80000000-0000-0000-0000-000000000001"),
            ),
        ),
        last_change_cursor=17,
        as_of=NOW,
    )

    assert [node.subtask_key for node in graph.nodes] == [
        "callback-idempotency",
        "integration",
    ]
    callback = graph.nodes[0]
    assert callback.worker_item_count == 2
    assert callback.running_worker_item_count == 1
    assert callback.attention_worker_item_count == 1
    assert not hasattr(callback, "worker_id")
    assert graph.edges[0].upstream_subtask_key == "callback-idempotency"
    assert graph.edges[0].downstream_subtask_key == "integration"
    assert graph.nodes[1].state == "waiting_dependency"


def test_task_attention_projects_scheduling_wait_facts() -> None:
    graph = TaskConsoleProjectionService().compose(
        _plan(),
        tasks=_tasks(),
        worker_items=_worker_items(),
        placements=_placements(),
        executions=(
            TaskConsoleExecutionFact(
                id=WAITING_EXECUTION_ID,
                task_id=CALLBACK_TASK_ID,
                worker_item_id=REVIEWER_ID,
                status="queued",
                updated_at=NOW,
                active_block_id=UUID("90000000-0000-0000-0000-000000000001"),
                active_block_version=2,
                active_block_reason="runtime_configuration_missing",
                active_block_requires_operator=True,
                active_block_first_seen_at=datetime(2026, 8, 22, 11, 55, tzinfo=UTC),
                active_block_summary="2 个候选执行节点未配置运行环境",
            ),
        ),
        evidence=(),
        activity=(
            TaskConsoleActivityFact(
                id=UUID("70000000-0000-0000-0000-000000000001"),
                task_id=CALLBACK_TASK_ID,
                worker_item_id=REVIEWER_ID,
                occurred_at=NOW,
                kind="input.requested",
                summary="等待确认重试规则",
                correlation_id=UUID("80000000-0000-0000-0000-000000000001"),
            ),
        ),
        last_change_cursor=17,
        as_of=NOW,
    )

    item = graph.overview.attention_items[0]
    assert item.task_title == "支付重构"
    assert item.subtask_key == "callback-idempotency"
    assert item.subtask_title == "回调幂等"
    assert item.reason == "2 个候选执行节点未配置运行环境"
    assert item.waiting_reason == "runtime_configuration_missing"
    assert item.waiting_details_summary == "2 个候选执行节点未配置运行环境"
    assert item.requires_operator is True
    assert item.blocked_since == datetime(2026, 8, 22, 11, 55, tzinfo=UTC)


def test_succeeded_requires_subtask_evidence() -> None:
    plan = _plan()
    graph = TaskConsoleProjectionService().compose(
        plan,
        tasks={
            CALLBACK_TASK_ID: _tasks()[CALLBACK_TASK_ID],
            INTEGRATION_TASK_ID: TaskConsoleTaskFact(
                id=INTEGRATION_TASK_ID,
                title="支付集成",
                status="succeeded",
                version=2,
                updated_at=NOW,
            ),
        },
        worker_items=_worker_items(),
        placements=_placements(),
        executions=(
            TaskConsoleExecutionFact(
                id=UUID("50000000-0000-0000-0000-000000000003"),
                task_id=INTEGRATION_TASK_ID,
                worker_item_id=INTEGRATOR_ID,
                status="succeeded",
                updated_at=NOW,
            ),
        ),
        evidence=(),
        activity=(),
        last_change_cursor=17,
        as_of=NOW,
    )

    assert graph.nodes[1].state != "succeeded"
