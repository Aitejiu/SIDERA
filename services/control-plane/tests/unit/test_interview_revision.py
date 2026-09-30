from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.application.planner_runtime import (
    PlannerProposalResult,
    PlannerRunControl,
)
from sidera_control_plane.application.revision_planning_context import (
    RevisionPlanningContextV1,
)
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.domain.plan_revisions import (
    IdentityStrategy,
    RevisionBaseDescriptorV1,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
    SubtaskPlanItem,
    TaskPlanSnapshot,
    WorkerItemDependency,
    WorkerItemPlanItem,
    WorkItemKind,
)
from tests.unit.test_interview_planner import _planner_input


def _manifest(tmp_path: Path) -> DemoManifest:
    from tests.unit.test_interview_planner import _manifest as build_manifest

    return build_manifest(tmp_path, scenario="revision")


def _context(manifest: DemoManifest) -> RevisionPlanningContextV1:
    from sidera_control_plane.adapters.demo_planner import build_initial_proposal
    from sidera_control_plane.domain.collaboration import (
        resolve_collaboration_policy_v2,
    )

    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    root_task_id = uuid4()
    planner_input = _planner_input(manifest)
    initial_proposal = build_initial_proposal(manifest)
    snapshot = TaskPlanSnapshot.create(
        snapshot_id=uuid4(),
        schema_version=5,
        workspace_id=manifest.workspace_id,
        root_task_id=root_task_id,
        generation=1,
        parent_snapshot_id=None,
        source_type="proposal",
        source_ref="proposal://csv",
        title="CSV 批量导入",
        goal_summary="完成 CSV 导入、报告、复核和集成验证。",
        acceptance_refs=("acceptance:csv-import",),
        published_by="demo-planner",
        published_at=now,
        subtasks=(
            SubtaskPlanItem(
                key="validation",
                task_id=uuid4(),
                kind=WorkItemKind.CHILD,
                title="CSV 数据校验",
                goal_summary="确认字段校验结果。",
                acceptance_refs=("acceptance:validation",),
                milestone_key=None,
                expected_outputs=("artifact_type:report",),
                source_refs=("brief:current",),
            ),
            SubtaskPlanItem(
                key="reporting",
                task_id=uuid4(),
                kind=WorkItemKind.CHILD,
                title="报告与复核",
                goal_summary="生成并复核报告。",
                acceptance_refs=("acceptance:report",),
                milestone_key=None,
                expected_outputs=("artifact_type:report",),
                source_refs=("brief:current",),
            ),
            SubtaskPlanItem(
                key="integration",
                task_id=uuid4(),
                kind=WorkItemKind.INTEGRATION,
                title="集成验证",
                goal_summary="运行最终验证。",
                acceptance_refs=("acceptance:integration",),
                milestone_key=None,
                expected_outputs=("artifact_type:diff",),
                source_refs=("brief:current",),
            ),
        ),
        worker_item_plans=(
            WorkerItemPlanItem(
                key="validate",
                worker_item_id=uuid4(),
                subtask_key="validation",
                title="CSV 校验 WorkerItem",
                role="validator",
                responsibility="执行字段校验。",
                capability_requirements=("python", "demo:worker-item:validate"),
                worker_policy_ref="policy:demo",
                configuration_ref="config:demo-validation",
                concurrency_limit=1,
                source_refs=("brief:current",),
            ),
            WorkerItemPlanItem(
                key="report",
                worker_item_id=uuid4(),
                subtask_key="reporting",
                title="错误报告 WorkerItem",
                role="reporter",
                responsibility="生成错误报告。",
                capability_requirements=("python", "demo:worker-item:report"),
                worker_policy_ref="policy:demo",
                configuration_ref="config:demo-report",
                concurrency_limit=1,
                source_refs=("brief:current",),
            ),
            WorkerItemPlanItem(
                key="review-report",
                worker_item_id=uuid4(),
                subtask_key="reporting",
                title="报告复核 WorkerItem",
                role="reviewer",
                responsibility="复核错误报告。",
                capability_requirements=(
                    "python",
                    "demo:worker-item:review-report",
                ),
                worker_policy_ref="policy:demo",
                configuration_ref="config:demo-review",
                concurrency_limit=1,
                source_refs=("brief:current",),
            ),
            WorkerItemPlanItem(
                key="integrate",
                worker_item_id=uuid4(),
                subtask_key="integration",
                title="集成验证 WorkerItem",
                role="integrator",
                responsibility="运行最终验证。",
                capability_requirements=("python", "demo:worker-item:integrate"),
                worker_policy_ref="policy:demo",
                configuration_ref="config:demo-integration",
                concurrency_limit=1,
                source_refs=("brief:current",),
            ),
        ),
        subtask_milestones=(),
        subtask_dependencies=(
            SubtaskDependency(
                upstream_subtask_key="validation",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:validation",),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
            SubtaskDependency(
                upstream_subtask_key="reporting",
                downstream_subtask_key="integration",
                kind=DependencyKind.HARD,
                required_evidence_refs=("acceptance:report",),
                enforcement=DependencyEnforcement.CONTROL_PLANE,
            ),
        ),
        worker_item_dependencies=(
            WorkerItemDependency(
                upstream_worker_item_key="report",
                downstream_worker_item_key="review-report",
                kind="dependency",
                required_artifact_refs=("artifact_type:report",),
            ),
        ),
        collaboration_policy=resolve_collaboration_policy_v2(
            initial_proposal.collaboration_policy_request,
            planner_input.profile_snapshot.collaboration_runtime_binding,
            subtask_keys=frozenset({"validation", "reporting", "integration"}),
        ),
        integration_spec=initial_proposal.integration_spec,
    )
    descriptor = RevisionBaseDescriptorV1(
        schema_version=1,
        workspace_id=manifest.workspace_id,
        root_task_id=root_task_id,
        plan_snapshot_id=snapshot.id,
        plan_generation=snapshot.generation,
        plan_content_hash=plan_snapshot_content_hash(snapshot),
        root_task_version=1,
        captured_at=now,
    )
    return RevisionPlanningContextV1(
        schema_version=1,
        base_descriptor=descriptor,
        plan_snapshot_payload=snapshot.to_payload(),
        tasks=(),
        worker_items=(),
        executions=(),
        sessions=(),
        context_package_refs=(),
        adapter_capabilities=(),
        max_context_bytes=128_000,
    )


def test_revision_proposal_reuses_report_chain_and_adds_tsv_branch(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import build_revision_proposal

    manifest = _manifest(tmp_path)
    context = _context(manifest)
    proposal = build_revision_proposal(manifest, context)

    assert isinstance(proposal, PlanningRevisionProposalPayloadV6)
    assert proposal.kind is PlanningRequestKind.REVISION
    assert proposal.base_descriptor == context.base_descriptor
    assert {item.key for item in proposal.subtasks} == {
        "validation",
        "reporting",
        "integration",
        "parse-tsv",
    }
    assert {item.key for item in proposal.worker_items} == {
        "validate",
        "report",
        "review-report",
        "integrate",
        "parse-tsv",
    }

    base_workers = {
        item.key: item.worker_item_id
        for item in TaskPlanSnapshot.from_payload(context.plan_snapshot_payload).worker_item_plans
    }
    decisions = {
        item.target_worker_item_key: item
        for item in proposal.worker_item_identity_decisions
    }
    for key in ("report", "review-report"):
        assert decisions[key].strategy is IdentityStrategy.REUSE
        assert decisions[key].existing_worker_item_id == base_workers[key]
    assert decisions["parse-tsv"].strategy is IdentityStrategy.CREATE

    assert (
        "parse-tsv",
        "integration",
    ) in {
        (item.upstream_subtask_key, item.downstream_subtask_key)
        for item in proposal.subtask_dependencies
    }
    integration_group = next(
        group
        for group in proposal.collaboration_policy_request.input_groups
        if group.group_key == "integration-inputs"
    )
    assert [item.worker_item_key for item in integration_group.candidates] == [
        "validate",
        "report",
        "parse-tsv",
    ]
    assert integration_group.minimum_count == 3
    assert proposal.integration_spec.base_commit == manifest.base_commit
    assert proposal.integration_spec.repository_ref == opaque_repository_ref(
        manifest.repository
    )
    assert "TSV" in proposal.decision_summary.understanding
    assert "phone_optional" in proposal.decision_summary.metadata["preserved_contracts"]


def test_demo_planner_accepts_revision_context_without_switching_before_confirmation(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import DemoPlannerAdapter

    manifest = _manifest(tmp_path)
    context = _context(manifest)
    original = _planner_input(manifest, feedback="还需要支持 TSV 文件")
    planner_input = replace(
        original,
        kind=PlanningRequestKind.REVISION,
        context_materialization=None,
        planning_context=context,
    )
    control = PlannerRunControl(
        operation_id=planner_input.operation_id,
        started_at=datetime.now(UTC),
        deadline_at=datetime.now(UTC),
    )

    result = DemoPlannerAdapter(manifest).plan(planner_input, control)

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.kind is PlanningRequestKind.REVISION
    assert result.proposal.payload.base_descriptor == context.base_descriptor
