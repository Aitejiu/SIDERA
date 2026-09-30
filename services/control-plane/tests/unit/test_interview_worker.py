from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.demo.bootstrap import _resources
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.manifest import stable_id
from sidera_control_plane.demo.sample_repository import create_sample_repository
from sidera_control_plane.demo.state import DemoStateStore
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextMaterializationV1,
    ContextMetadata,
    ContextPurpose,
    ControlPlaneFactSection,
    SystemConstraintSection,
    WorkContextSection,
    context_package_reference,
)
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
)


def _manifest(tmp_path: Path) -> DemoManifest:
    run_root = tmp_path / "run"
    run_root.mkdir(parents=True)
    repository, base_commit = create_sample_repository(run_root)
    run_id = uuid4()
    return DemoManifest(
        run_id=run_id,
        workspace_id=uuid4(),
        scenario="mainline",
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


def _adapter(manifest: DemoManifest, workspace_root: Path):
    from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapter

    instance = _resources(manifest).instances["worker_primary"]
    return DemoWorkerAdapter(
        manifest,
        instance,
        lambda _workspace: workspace_root,
    )


def _spec_and_workspace(
    manifest: DemoManifest,
    tmp_path: Path,
    *,
    execution_id=None,
) -> tuple[ExecutionSpec, ExecutionWorkspaceManifest]:
    execution_id = execution_id or uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    checkout = WorkspaceCheckout(
        repository_ref=f"repo://interview/{manifest.run_id}",
        base_revision=manifest.base_commit,
        branch=f"demo/{execution_id}",
        current_head=manifest.base_commit,
    )
    workspace = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=manifest.workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        checkouts=(checkout,),
        location_ref=f"interview-demo://workspace/{execution_id}",
        retention_seconds=3600,
        fencing_token=1,
        created_at=now,
    )
    instance = _resources(manifest).instances["worker_primary"]
    spec = ExecutionSpec(
        schema_version=1,
        workspace_id=manifest.workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        worker_id=manifest.worker_ids["validation"],
        lease_id=uuid4(),
        fencing_token=1,
        goal="Validate the CSV sample",
        acceptance_criteria=("acceptance:validation",),
        context_package_ref=None,
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref=f"repo://interview/{manifest.run_id}",
                base_revision=manifest.base_commit,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=("tests",),
                readable_paths=("tests",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={},
        budget={},
        deadline=now + timedelta(minutes=5),
        expected_outputs=("artifact_type:report",),
        adapter_module_ref=instance.id,
        adapter_config_version=1,
        workspace_module_ref=manifest.module_ids["workspace"],
        workspace_config_version=1,
    )
    return spec, workspace


def _with_execution_context(spec: ExecutionSpec) -> ExecutionSpec:
    package_id = uuid4()
    package_hash = "a" * 64
    materialization = ContextMaterializationV1(
        metadata=ContextMetadata(
            package_id=package_id,
            package_hash=package_hash,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=spec.execution_id,
            context_version=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        system_constraints=SystemConstraintSection(
            objective="Validate the CSV sample",
            constraints=(),
            acceptance_criteria=(),
        ),
        work_context=WorkContextSection(
            revision_id=uuid4(),
            revision_number=1,
            content_hash="b" * 64,
            snapshot_schema_version=1,
            requested_outputs=(),
            clarification_decisions=(),
            accepted_decisions=(),
            unresolved_questions=(),
            checkpoint_refs=(),
            missing_fact_codes=(),
        ),
        control_plane_facts=ControlPlaneFactSection(facts=()),
        project_knowledge=(),
        artifact_and_evidence_refs=(),
    )
    return replace(
        spec,
        schema_version=2,
        context_package_ref=context_package_reference(package_id, package_hash),
        context_package=materialization,
    )


def test_demo_worker_rejects_foreign_module_and_advertises_durable_observation(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapter

    manifest = _manifest(tmp_path)
    instance = _resources(manifest).instances["worker_primary"]
    adapter = DemoWorkerAdapter(manifest, instance, lambda _workspace: tmp_path)

    assert adapter.probe({}).resume.value == "durable"

    foreign = replace(instance, id=uuid4())
    with pytest.raises(ValueError, match="module identity"):
        DemoWorkerAdapter(manifest, foreign, lambda _workspace: tmp_path)


def test_demo_worker_persists_sessions_replays_events_and_stops_only_target(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    adapter = _adapter(manifest, workspace_root)
    spec_one, workspace_one = _spec_and_workspace(
        manifest, tmp_path, execution_id=uuid4()
    )
    spec_two, workspace_two = _spec_and_workspace(
        manifest, tmp_path, execution_id=uuid4()
    )
    start_one = uuid4()
    start_two = uuid4()

    first = adapter.start_session(spec_one, workspace_one, operation_id=start_one)
    second = adapter.start_session(spec_two, workspace_two, operation_id=start_two)
    restarted = _adapter(manifest, workspace_root)
    replay = restarted.start_session(spec_one, workspace_one, operation_id=start_one)

    assert replay.execution_session_id == first.execution_session_id
    assert tuple(event.sequence for event in restarted.read_events(first)) == (1,)
    assert tuple(event.sequence for event in restarted.read_events(replay)) == (1,)

    stopped = restarted.stop_session(first, operation_id=uuid4())
    assert stopped.outcome is DeliveryOutcome.DELIVERED
    first_events = restarted.read_events(replace(first, last_accepted_sequence=0))
    assert tuple(event.sequence for event in first_events) == (1, 2)
    assert first_events[-1].event_type == "session.stopped"

    second_events = restarted.read_events(replace(second, last_accepted_sequence=0))
    assert tuple(event.sequence for event in second_events) == (1,)
    assert all(event.event_type != "session.stopped" for event in second_events)
    assert restarted.collect_artifacts(second, operation_id=uuid4()) == ()


def test_demo_worker_runs_closure_source_and_emits_turn_completion(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "worktrees" / "closure-source-a"
    workspace_root.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(workspace_root),
            manifest.base_commit,
        ],
        cwd=manifest.repository,
        capture_output=True,
        check=True,
    )
    adapter = _adapter(manifest, workspace_root)
    spec, workspace = _spec_and_workspace(manifest, tmp_path)
    spec = replace(
        _with_execution_context(spec),
        goal="并行生成两个相互独立的源成果文件。",
        request_purpose="只生成 out/source-a.json，不修改其他成果或 README。",
        expected_outputs=("out/source-a.json",),
        acceptance_criteria=("acceptance:closure-source-a",),
    )
    handle = adapter.start_session(spec, workspace, operation_id=uuid4())
    gates = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    gates.set_gate("parallel", True)
    gates.set_gate("before-artifact", True)

    operation_id = gateway_operation_id(
        spec.execution_id,
        GatewayOperationType.SEND_INPUT,
    )
    delivery = adapter.send_input(
        handle,
        spec.goal,
        (),
        operation_id=operation_id,
    )
    events = tuple(adapter.read_events(handle))
    candidates = adapter.collect_artifacts(handle, operation_id=uuid4())

    assert delivery.outcome is DeliveryOutcome.DELIVERED
    completion = next(event for event in events if event.event_type == "turn.completed")
    assert completion.payload == {
        "schema_version": 1,
        "operation_id": str(operation_id),
        "stop_reason": "end_turn",
    }
    assert len(candidates) == 1
    assert candidates[0].artifact_type.value == "external"
    assert candidates[0].content is not None
    assert b"SIDERA_CLOSURE_SOURCE_A_V1" in candidates[0].content


def test_demo_worker_can_run_validation_on_replacement_node_outside_handoff(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    adapter = _adapter(manifest, workspace_root)
    spec, workspace = _spec_and_workspace(manifest, tmp_path)
    replacement_spec = replace(spec, worker_id=manifest.worker_ids["replacement"])

    handle = adapter.start_session(
        replacement_spec,
        workspace,
        operation_id=uuid4(),
    )

    assert handle.execution_session_id is not None


def test_demo_worker_blocks_input_when_session_has_no_execution_context(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    adapter = _adapter(manifest, workspace_root)
    spec, workspace = _spec_and_workspace(manifest, tmp_path)
    handle = adapter.start_session(spec, workspace, operation_id=uuid4())

    delivery = adapter.send_input(
        handle,
        "手机号允许为空，有值时才校验格式",
        (),
        operation_id=uuid4(),
    )

    assert delivery.outcome is DeliveryOutcome.BLOCKED
    assert delivery.failure_code == "worker_context_missing"


def test_demo_worker_acknowledges_initial_prompt_without_treating_it_as_intervention(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "worktrees" / "validation"
    workspace_root.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(workspace_root),
            manifest.base_commit,
        ],
        cwd=manifest.repository,
        capture_output=True,
        check=True,
    )
    adapter = _adapter(manifest, workspace_root)
    spec, workspace = _spec_and_workspace(manifest, tmp_path)
    handle = adapter.start_session(
        _with_execution_context(spec),
        workspace,
        operation_id=uuid4(),
    )
    operation_id = gateway_operation_id(
        spec.execution_id,
        GatewayOperationType.SEND_INPUT,
    )
    prompt = "Validate the CSV sample\n\nAcceptance criteria: valid rows are reported"
    DemoStateStore(manifest.run_root / "state", manifest.run_id).set_gate(
        "parallel", True
    )

    delivery = adapter.send_input(
        handle,
        prompt,
        (),
        operation_id=operation_id,
    )
    replay = adapter.send_input(
        handle,
        prompt,
        (),
        operation_id=operation_id,
    )
    events = tuple(adapter.read_events(replace(handle, last_accepted_sequence=0)))

    assert delivery.outcome is DeliveryOutcome.DELIVERED
    assert replay == delivery
    assert sum(event.event_type == "prompt.accepted" for event in events) == 1
    assert not any(event.event_type == "input.accepted" for event in events)
    assert any(
        event.payload.get("step_key") == "waiting-confirmation" for event in events
    )


def test_demo_worker_applies_and_deduplicates_a_targeted_phone_policy_input(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "worktrees" / "validation"
    workspace_root.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(workspace_root),
            manifest.base_commit,
        ],
        cwd=manifest.repository,
        capture_output=True,
        check=True,
    )
    adapter = _adapter(manifest, workspace_root)
    spec, workspace = _spec_and_workspace(manifest, tmp_path)
    handle = adapter.start_session(
        _with_execution_context(spec),
        workspace,
        operation_id=uuid4(),
    )
    gate_state = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    gate_state.set_gate("parallel", True)
    gate_state.set_gate("before-artifact", False)
    adapter.read_events(handle)
    assert adapter.collect_artifacts(handle, operation_id=uuid4()) == ()

    input_operation_id = uuid4()
    first = adapter.send_input(
        handle,
        "手机号允许为空，有值时才校验格式",
        (),
        operation_id=input_operation_id,
    )
    replay = adapter.send_input(
        handle,
        "手机号允许为空，有值时才校验格式",
        (),
        operation_id=input_operation_id,
    )

    assert first.outcome is DeliveryOutcome.DELIVERED
    assert replay == first
    assert adapter.collect_artifacts(handle, operation_id=uuid4()) == ()
    gate_state.set_gate("before-artifact", True)
    events = tuple(adapter.read_events(replace(handle, last_accepted_sequence=0)))
    assert tuple(event.sequence for event in events) == tuple(range(1, len(events) + 1))
    assert sum(event.event_type == "input.accepted" for event in events) == 1
    summaries = tuple(str(event.payload.get("trace_summary", "")) for event in events)
    assert any("等待确认" in summary for summary in summaries)
    assert any("更新校验" in summary for summary in summaries)

    candidates = adapter.collect_artifacts(handle, operation_id=uuid4())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.content is not None
    assert hashlib.sha256(candidate.content).hexdigest() == candidate.content_hash
    assert candidate.size_bytes == len(candidate.content)
    body = json.loads(candidate.content)
    assert body["phone_policy"] == "optional"
    bob = next(row for row in body["rows"] if row["row"]["name"] == "Bob")
    assert bob["valid"] is True
    assert candidate.provenance["simulation"] is True
    assert candidate.provenance["worker_item_key"] == "validate"
    assert candidate.provenance["input_operation_id"] == str(input_operation_id)
    assert candidate.provenance["acceptance_criterion_refs"] == [
        "acceptance:validation"
    ]
    assert candidate.provenance["content_hash"] == candidate.content_hash


def test_demo_worker_preserves_unsupported_input_without_producing_an_artifact(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "worktrees" / "validation"
    workspace_root.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(workspace_root),
            manifest.base_commit,
        ],
        cwd=manifest.repository,
        capture_output=True,
        check=True,
    )
    adapter = _adapter(manifest, workspace_root)
    spec, workspace = _spec_and_workspace(manifest, tmp_path)
    handle = adapter.start_session(
        _with_execution_context(spec),
        workspace,
        operation_id=uuid4(),
    )
    gate_state = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    gate_state.set_gate("parallel", True)
    gate_state.set_gate("before-artifact", True)
    adapter.read_events(handle)

    operation_id = uuid4()
    text = "顺便把生产库迁移一下"
    first = adapter.send_input(handle, text, (), operation_id=operation_id)
    replay = adapter.send_input(handle, text, (), operation_id=operation_id)

    assert first.outcome is DeliveryOutcome.BLOCKED
    assert first.failure_code == "input_not_supported"
    assert replay == first
    events = tuple(adapter.read_events(replace(handle, last_accepted_sequence=0)))
    accepted = [event for event in events if event.event_type == "input.accepted"]
    assert len(accepted) == 1
    assert accepted[0].payload["text"] == text
    assert any(
        event.payload.get("handled") is False
        and event.payload.get("input_operation_id") == str(operation_id)
        for event in events
    )
    assert adapter.collect_artifacts(handle, operation_id=uuid4()) == ()


def test_demo_review_worker_reads_selected_report_from_context_and_records_source(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.domain.context_packages import (
        ContextMaterializationV1,
        ContextMaterializationV2,
        MaterializedResultInput,
        ResultInputSourceProvenanceV1,
    )

    manifest = _manifest(tmp_path)
    workspace_root = tmp_path / "worktrees" / "review"
    workspace_root.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(workspace_root),
            manifest.base_commit,
        ],
        cwd=manifest.repository,
        capture_output=True,
        check=True,
    )
    adapter = _adapter(manifest, workspace_root)
    spec, workspace = _spec_and_workspace(manifest, tmp_path)
    spec = replace(
        spec,
        worker_id=manifest.worker_ids["review"],
        acceptance_criteria=("acceptance:report",),
    )
    contextual_spec = _with_execution_context(spec)
    legacy = contextual_spec.context_package
    assert isinstance(legacy, ContextMaterializationV1)
    artifact_id = uuid4()
    lineage_id = uuid4()
    report_body = json.dumps(
        {
            "schema_version": 1,
            "simulation": True,
            "report": {
                "accepted": 3,
                "rejected": 1,
                "errors": [{"row_number": 4, "code": "phone_invalid"}],
            },
        },
        sort_keys=True,
    )
    body_bytes = report_body.encode("utf-8")
    result_input = MaterializedResultInput(
        artifact_id=artifact_id,
        content_hash=hashlib.sha256(body_bytes).hexdigest(),
        size_bytes=len(body_bytes),
        media_type="application/json",
        sources=(
            ResultInputSourceProvenanceV1(
                worker_item_key="report",
                worker_item_id=uuid4(),
                subtask_path=("reporting",),
                effective_request_id=uuid4(),
                allowed_lineage_ids=(lineage_id,),
                effective_lineage_id=lineage_id,
                effective_execution_id=uuid4(),
                attempt=1,
                inheritance_refs=(),
                priority=1,
                source_basis_hash="d" * 64,
            ),
        ),
        group_keys=("review-report-input",),
        hard_refs=("artifact_type:report",),
        body_text=report_body,
    )
    materialization = ContextMaterializationV2(
        metadata=legacy.metadata,
        system_constraints=legacy.system_constraints,
        work_context=legacy.work_context,
        control_plane_facts=legacy.control_plane_facts,
        project_knowledge=legacy.project_knowledge,
        artifact_and_evidence_refs=legacy.artifact_and_evidence_refs,
        result_inputs=(result_input,),
    )
    review_spec = replace(
        spec,
        schema_version=2,
        context_package_ref=context_package_reference(
            materialization.metadata.package_id,
            materialization.metadata.package_hash,
        ),
        context_package=materialization,
    )
    handle = adapter.start_session(review_spec, workspace, operation_id=uuid4())
    gates = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    gates.set_gate("parallel", True)
    gates.set_gate("before-artifact", True)

    events = adapter.read_events(handle)
    assert any(event.event_type == "session.completed" for event in events)
    candidates = adapter.collect_artifacts(handle, operation_id=uuid4())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.content is not None
    review_text = candidate.content.decode("utf-8")
    assert f"Source artifact: `{artifact_id}`" in review_text
    assert "Accepted rows: 3" in review_text
    assert "Rejected rows: 1" in review_text
    assert "Review: passed" in review_text
    assert candidate.provenance["source_artifact_ids"] == [str(artifact_id)]
    assert candidate.provenance["acceptance_criterion_refs"] == ["acceptance:report"]


def test_demo_worker_rejects_invalid_input_session_and_unimplemented_mutations(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    adapter = _adapter(manifest, tmp_path / "workspaces")
    with pytest.raises(RuntimeError, match="session handle is invalid"):
        adapter.send_input(None, "input", (), operation_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="unsupported"):
        adapter.collect_knowledge_candidates(None, operation_id=uuid4())  # type: ignore[arg-type]


def test_demo_worker_accepts_a_materialized_runtime_head_for_code_input(
    tmp_path: Path,
) -> None:
    from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
    from sidera_control_plane.demo.worker_steps import _assert_workspace

    manifest = _manifest(tmp_path)
    workspace = tmp_path / "workspaces" / "materialized"
    workspace.parent.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(workspace),
            manifest.base_commit,
        ],
        cwd=manifest.repository,
        capture_output=True,
        check=True,
    )
    (workspace / "runtime.txt").write_text("materialized\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "runtime.txt",
        ],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=SIDERA test",
            "-c",
            "user.email=test@sidera.local",
            "commit",
            "-m",
            "materialized runtime head",
        ],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    runtime_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    task_id = uuid4()
    execution_id = uuid4()
    repository_ref = opaque_repository_ref(manifest.repository)
    session = {
        "workspace_id": str(manifest.workspace_id),
        "task_id": str(task_id),
        "execution_id": str(execution_id),
        "fencing_token": 1,
        "workspace_path": str(workspace),
        "workspace_runtime_head": runtime_head,
        "workspace_manifest": {
            "workspace_id": str(manifest.workspace_id),
            "task_id": str(task_id),
            "execution_id": str(execution_id),
            "fencing_token": 1,
            "repository_ref": repository_ref,
            "base_revision": manifest.base_commit,
            "checkout": {
                "repository_ref": repository_ref,
                "base_revision": manifest.base_commit,
                "branch": f"sidera/execution/{execution_id}",
                "current_head": manifest.base_commit,
            },
        },
    }

    _assert_workspace(manifest, session)


def test_demo_worker_inventory_entry_is_explicitly_simulated() -> None:
    from sidera_control_plane.gateway_worker import worker_adapter_inventory

    entry = next(
        item
        for item in worker_adapter_inventory()
        if item.adapter_type == "interview-demo-worker"
    )
    assert entry.is_fake is True
    assert entry.delegates_to_opencode is False
    assert entry.is_local_verification is False
