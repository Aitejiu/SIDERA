from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogSnapshot,
    PlannerClarificationResult,
    PlannerInput,
    PlannerPolicyConstraints,
    PlannerProposalResult,
    PlannerRunControl,
)
from sidera_control_plane.demo.bootstrap import _resources
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.manifest import stable_id
from sidera_control_plane.demo.sample_repository import create_sample_repository
from sidera_control_plane.domain.modules import WorkspaceProviderCapabilityProfile
from sidera_control_plane.domain.planning import PlanningBriefRevision


def _manifest(tmp_path: Path, *, scenario: str = "mainline") -> DemoManifest:
    run_id = uuid4()
    project_root = Path(__file__).resolve().parents[4]
    run_root = project_root / ".sidera" / "interview-demo" / str(run_id)
    sample_parent = tmp_path / "sample"
    sample_parent.mkdir(parents=True, exist_ok=True)
    repository, base_commit = create_sample_repository(sample_parent)
    return DemoManifest(
        run_id=run_id,
        workspace_id=uuid4(),
        scenario=scenario,  # type: ignore[arg-type]
        run_root=run_root,
        repository=repository,
        base_commit=base_commit,
        code_commit=base_commit,
        database_name=f"sidera_interview_test_{run_id.hex}",
        knowledge_database_name=f"sidera_interview_knowledge_{run_id.hex}",
        endpoints={
            "control_plane": "http://127.0.0.1:15432",
            "knowledge": "http://127.0.0.1:15433",
            "main_agent": "http://127.0.0.1:15434",
            "web": "http://127.0.0.1:15435",
        },
        module_ids={
            key: stable_id(run_id, f"module:{key}")
            for key in (
                "worker_primary",
                "worker_secondary",
                "workspace",
                "planner",
                "collaboration",
                "verification",
                "notification",
            )
        },
        worker_ids={
            key: stable_id(run_id, f"worker:{key}")
            for key in (
                "validation",
                "report",
                "review",
                "integration",
                "replacement",
                "verification",
            )
        },
        planning_profile_id=stable_id(run_id, "profile:planning"),
    )


def _planner_input(
    manifest: DemoManifest,
    *,
    feedback: str | None = None,
    objective: str = "为项目增加 CSV 批量导入功能",
) -> PlannerInput:
    resources = _resources(manifest)
    capabilities = resources.profile.snapshot(
        resources.definitions["planner"].capabilities  # type: ignore[arg-type]
    )
    request_id = uuid4()
    brief = PlanningBriefRevision.create(
        planning_request_id=request_id,
        revision=1,
        objective=objective,
        constraints=("手机号规则在执行阶段确认",),
        acceptance_criteria=("CSV 导入完成并通过验证",),
        context_refs=(),
        requested_outputs=("proposal",),
        user_feedback=feedback,
        source_correlation=uuid4(),
        created_by="interview-test",
        created_at=datetime.now(UTC),
    )
    return PlannerInput(
        interface_version=capabilities.planner_interface_version,
        proposal_schema_version=capabilities.proposal_schema_version,
        operation_id=uuid4(),
        planning_request_id=request_id,
        planning_run_id=uuid4(),
        brief_revision=brief.revision,
        brief=brief,
        profile_snapshot=capabilities,
        capability_catalog=CapabilityCatalogSnapshot(
            snapshot_version=1,
            captured_at=datetime.now(UTC),
            capabilities=(),
            adapter_preferences=capabilities.adapter_preferences,
            budget_level=capabilities.execution_budget_level,
            max_concurrency=capabilities.budget.max_concurrency,
            workspace_providers=(),
        ),
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(capabilities),
        context_refs=(),
    )


def test_mainline_worker_ownership_and_dependencies_use_real_demo_identity(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import build_initial_proposal

    manifest = _manifest(tmp_path)
    proposal = build_initial_proposal(manifest)

    assert {
        item.key: item.subtask_key for item in proposal.worker_items
    } == {
        "validate": "validation",
        "report": "reporting",
        "review-report": "reporting",
        "integrate": "integration",
    }
    assert {
        item.key: item.capability_requirements for item in proposal.worker_items
    } == {
        "validate": ("python", "demo:worker-item:validate"),
        "report": ("python", "demo:worker-item:report"),
        "review-report": ("python", "demo:worker-item:review-report"),
        "integrate": ("python", "demo:worker-item:integrate"),
    }
    assert proposal.integration_spec.base_commit == manifest.base_commit
    assert proposal.integration_spec.repository_ref == opaque_repository_ref(
        manifest.repository
    )
    assert proposal.integration_spec.verification_binding.adapter_module_ref == (
        manifest.module_ids["verification"]
    )
    assert proposal.integration_spec.verification_binding.workspace_module_ref == (
        manifest.module_ids["workspace"]
    )
    assert {
        (item.upstream_subtask_key, item.downstream_subtask_key)
        for item in proposal.subtask_dependencies
    } == {("validation", "integration"), ("reporting", "integration")}
    assert {
        (item.upstream_worker_item_key, item.downstream_worker_item_key)
        for item in proposal.worker_item_dependencies
    } == {("report", "review-report")}


def test_closure_sample_proposal_keeps_sources_parallel_until_integration(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import (
        build_closure_sample_proposal,
    )

    proposal = build_closure_sample_proposal(_manifest(tmp_path))

    assert {item.key for item in proposal.worker_items} == {
        "source-a",
        "source-b",
        "integration",
    }
    assert {
        item.key: item.subtask_key for item in proposal.worker_items
    } == {
        "source-a": "implementation",
        "source-b": "implementation",
        "integration": "integration",
    }
    assert proposal.worker_item_dependencies == ()
    assert all(not intent.activation_dependencies for intent in proposal.execution_intents)
    assert {
        (item.upstream_subtask_key, item.downstream_subtask_key)
        for item in proposal.subtask_dependencies
    } == {("implementation", "integration")}
    assert proposal.subtask_dependencies[0].required_evidence_refs == ()
    closure_inputs = next(
        group
        for group in proposal.collaboration_policy_request.input_groups
        if group.group_key == "closure-inputs"
    )
    assert closure_inputs.consumer_worker_item_key == "integration"
    assert tuple(
        candidate.worker_item_key for candidate in closure_inputs.candidates
    ) == ("source-a", "source-b")
    assert closure_inputs.minimum_count == 2
    assert closure_inputs.requirements.required_artifact_refs == ("artifact_type:external",)
    assert closure_inputs.requirements.required_evidence_refs == ()
    assert proposal.integration_spec.integration_worker_item_key == "integration"
    assert proposal.decision_summary.metadata["sample_version"] == "closure-sample-v1"


def test_closure_sample_proposal_passes_create_input_producer_validation(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import build_closure_sample_proposal
    from sidera_control_plane.domain.proposal_validation import validate_proposal

    manifest = _manifest(tmp_path)
    resources = _resources(manifest)
    profile_snapshot = resources.profile.snapshot(
        resources.definitions["planner"].capabilities  # type: ignore[arg-type]
    )

    validate_proposal(
        build_closure_sample_proposal(manifest),
        profile_snapshot=profile_snapshot,
        brief_revision=1,
    )


def test_report_review_receives_the_selected_report_artifact_as_context_input(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import build_initial_proposal

    proposal = build_initial_proposal(_manifest(tmp_path))
    review_group = next(
        group
        for group in proposal.collaboration_policy_request.input_groups
        if group.consumer_worker_item_key == "review-report"
    )

    assert review_group.to_mapping() == {
        "group_key": "review-report-input",
        "title": "报告复核输入",
        "consumer_worker_item_key": "review-report",
        "candidates": [{"worker_item_key": "report", "priority": 1}],
        "requirements": {
            "execution_status": "succeeded",
            "required_artifact_refs": ["artifact_type:report"],
            "required_evidence_refs": ["acceptance:report"],
            "evidence_status": "candidate",
        },
        "mode": "at_least",
        "minimum_count": 1,
        "selection_strategy": "plan_order",
    }


def test_integration_input_group_selects_both_successful_report_artifacts(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import build_initial_proposal

    proposal = build_initial_proposal(_manifest(tmp_path))
    integration_group = next(
        group
        for group in proposal.collaboration_policy_request.input_groups
        if group.consumer_worker_item_key == "integrate"
    )

    assert integration_group.to_mapping() == {
        "group_key": "integration-inputs",
        "title": "集成前置结果",
        "consumer_worker_item_key": "integrate",
        "candidates": [
            {"worker_item_key": "validate", "priority": 1},
            {"worker_item_key": "report", "priority": 2},
        ],
        "requirements": {
            "execution_status": "succeeded",
            "required_artifact_refs": ["artifact_type:report"],
            "required_evidence_refs": [],
            "evidence_status": "candidate",
        },
        "mode": "at_least",
        "minimum_count": 2,
        "selection_strategy": "plan_order",
    }


def test_integrator_uses_subtask_dependencies_and_input_groups(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import build_initial_proposal

    proposal = build_initial_proposal(_manifest(tmp_path))
    integration_intent = next(
        intent
        for intent in proposal.execution_intents
        if intent.worker_item_key == "integrate"
    )

    assert integration_intent.activation_dependencies == ()
    assert {
        (dependency.upstream_subtask_key, dependency.downstream_subtask_key)
        for dependency in proposal.subtask_dependencies
    } == {("validation", "integration"), ("reporting", "integration")}
    integration_group = next(
        group
        for group in proposal.collaboration_policy_request.input_groups
        if group.consumer_worker_item_key == "integrate"
    )
    assert {candidate.worker_item_key for candidate in integration_group.candidates} == {
        "validate",
        "report",
    }


def test_demo_workspace_supports_code_input_for_integration_verification(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import build_initial_proposal

    manifest = _manifest(tmp_path)
    resources = _resources(manifest)
    binding = build_initial_proposal(manifest).integration_spec.verification_binding
    workspace = resources.definitions["workspace"]

    assert binding.workspace_module_ref == resources.instances["workspace"].id
    assert isinstance(workspace.capabilities, WorkspaceProviderCapabilityProfile)
    assert workspace.capabilities.supports_code_input_version(1)


def test_demo_planner_returns_proposal_and_supports_controls(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import DemoPlannerAdapter

    manifest = _manifest(tmp_path)
    adapter = DemoPlannerAdapter(manifest)
    planner_input = _planner_input(manifest)
    control = PlannerRunControl(
        operation_id=planner_input.operation_id,
        started_at=datetime.now(UTC),
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    result = adapter.plan(planner_input, control)

    assert isinstance(result, PlannerProposalResult)
    assert result.proposal.payload.integration_spec.base_commit == manifest.base_commit

    controls_manifest = _manifest(tmp_path / "controls", scenario="controls")
    controls = DemoPlannerAdapter(controls_manifest)
    clarification_input = _planner_input(controls_manifest)
    clarification = controls.plan(
        clarification_input,
        PlannerRunControl(
            operation_id=clarification_input.operation_id,
            started_at=datetime.now(UTC),
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )
    assert isinstance(clarification, PlannerClarificationResult)

    rejected_input = _planner_input(controls_manifest, feedback="拒绝当前提案")
    rejected = controls.plan(
        rejected_input,
        PlannerRunControl(
            operation_id=rejected_input.operation_id,
            started_at=datetime.now(UTC),
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )
    assert getattr(rejected, "code", None) == "proposal_rejected"

    cancelled_input = _planner_input(controls_manifest, feedback="继续")
    controls.cancel(cancelled_input.operation_id)
    cancelled = controls.plan(
        cancelled_input,
        PlannerRunControl(
            operation_id=cancelled_input.operation_id,
            started_at=datetime.now(UTC),
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )
    assert getattr(cancelled, "code", None) == "cancelled"


def test_demo_planner_selects_closure_sample_from_the_explicit_brief(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_planner import DemoPlannerAdapter

    manifest = _manifest(tmp_path)
    adapter = DemoPlannerAdapter(manifest)
    planner_input = _planner_input(
        manifest,
        objective=(
            "closure-sample-v1: 请规划 source-a、source-b 和 integration，"
            "其中 integration 依赖 source-a 与 source-b。"
        ),
    )
    result = adapter.plan(
        planner_input,
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=datetime.now(UTC),
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )

    assert isinstance(result, PlannerProposalResult)
    assert {item.key for item in result.proposal.payload.worker_items} == {
        "source-a",
        "source-b",
        "integration",
    }


def test_demo_planner_is_registered_only_when_manifest_is_explicit(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.config import Settings
    from sidera_control_plane.planning_worker import build_planning_worker

    manifest = _manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    settings = Settings(
        database_url="postgresql+psycopg://sidera@127.0.0.1:55432/sidera",
        interview_demo_manifest=manifest_path,
    )
    runtime = build_planning_worker(settings)
    try:
        assert "interview-demo-planner" in runtime.worker._runner._adapter_factory._builders
        assert set(runtime.worker._runner._adapter_factory._builders) == {
            "interview-demo-planner"
        }
    finally:
        runtime.catalog.close()
        runtime.database.dispose()

    default_runtime = build_planning_worker(
        Settings(database_url="postgresql+psycopg://sidera@127.0.0.1:55432/sidera")
    )
    try:
        assert set(default_runtime.worker._runner._adapter_factory._builders) == {
            "fake",
            "opencode-acp-planner",
        }
    finally:
        default_runtime.catalog.close()
        default_runtime.database.dispose()
