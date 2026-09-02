"""Opt-in R6.2 gate for a real OpenCode source-to-target handoff."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.opencode_acp import (
    OpenCodeACPAdapter,
    handoff_profile_supported,
)
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
from sidera_control_plane.domain.handoff_evidence import HandoffInspectionOutcome
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
    workspace_manifest_digest,
)

pytestmark = pytest.mark.integration

HASH = "a" * 64


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "SIDERA R6.2 Gate")
    _git(repository, "config", "user.email", "r6-2-gate@sidera.local")
    (repository / "README.md").write_text("R6.2 real handoff baseline\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    revision = _git(repository, "rev-parse", "HEAD")
    (repository / "SIDERA_R6_2_PRE_HANDOFF.txt").write_text("preserve\n")
    return repository, revision


def _dirty_digest(cwd: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-color",
            "--",
            ".",
        ],
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def _context_package(
    execution_id: UUID,
    *,
    handoff_instruction: str | None = None,
) -> ContextMaterializationV1:
    facts: dict[str, object] = {"workspace": "opaque-r6-2-gate-workspace"}
    if handoff_instruction is not None:
        facts["handoff"] = {
            "bundle_id": str(uuid4()),
            "bundle_hash": HASH,
            "instruction": handoff_instruction,
        }
    return ContextMaterializationV1(
        metadata=ContextMetadata(
            package_id=uuid4(),
            package_hash=HASH,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution_id,
            context_version=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        system_constraints=SystemConstraintSection(
            objective="Continue the exact handoff execution",
            constraints=(),
            acceptance_criteria=(),
        ),
        work_context=WorkContextSection(
            revision_id=uuid4(),
            revision_number=1,
            content_hash=HASH,
            snapshot_schema_version=1,
            requested_outputs=(),
            clarification_decisions=(),
            accepted_decisions=(),
            unresolved_questions=(),
            checkpoint_refs=(),
            missing_fact_codes=(),
        ),
        control_plane_facts=ControlPlaneFactSection(facts=facts),
        project_knowledge=(),
        artifact_and_evidence_refs=(),
    )


def _spec(
    *,
    workspace_id: UUID,
    task_id: UUID,
    execution_id: UUID,
    worker_id: UUID,
    fencing_token: int,
    revision: str,
    context_package: ContextMaterializationV1,
) -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=2,
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=execution_id,
        worker_id=worker_id,
        lease_id=uuid4(),
        fencing_token=fencing_token,
        goal="Continue the transferred WorkerItem without changing task topology",
        acceptance_criteria=("the real OpenCode handoff boundary is exact",),
        context_package_ref=context_package_reference(
            context_package.metadata.package_id,
            context_package.metadata.package_hash,
        ),
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://r6-2-real-opencode",
                base_revision=revision,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 300},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=UUID("10000000-0000-0000-0000-000000000001"),
        adapter_config_version=1,
        workspace_module_ref=UUID("20000000-0000-0000-0000-000000000001"),
        workspace_config_version=1,
        context_package=context_package,
    )


def _manifest(spec: ExecutionSpec, *, location_ref: str) -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://r6-2-real-opencode",
                base_revision=spec.repositories[0].base_revision,
                branch=f"sidera/r6-2/{spec.execution_id}",
                current_head=spec.repositories[0].base_revision,
            ),
        ),
        location_ref=location_ref,
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=datetime.now(UTC),
    )


def test_real_opencode_same_adapter_handoff(
    tmp_path: Path,
) -> None:
    if os.environ.get("SIDERA_R6_2_REAL_OPENCODE") != "1":
        pytest.skip("set SIDERA_R6_2_REAL_OPENCODE=1 to run the real OpenCode gate")

    executable = os.environ.get("SIDERA_OPENCODE_EXECUTABLE")
    model = os.environ.get("SIDERA_OPENCODE_MODEL")
    if not executable:
        pytest.fail("SIDERA_OPENCODE_EXECUTABLE is required for the opt-in gate")
    if not model or not model.strip():
        pytest.fail("SIDERA_OPENCODE_MODEL is required; no model fallback is allowed")
    try:
        timeout_seconds = float(
            os.environ.get("SIDERA_R6_2_REAL_OPENCODE_TIMEOUT_SECONDS", "120")
        )
    except ValueError as exc:
        pytest.fail("SIDERA_R6_2_REAL_OPENCODE_TIMEOUT_SECONDS must be numeric")
        raise AssertionError from exc
    if timeout_seconds <= 0:
        pytest.fail("real OpenCode timeout must be positive")

    from sidera_control_plane.opencode_preflight import run_preflight

    preflight = run_preflight(
        executable=executable,
        expected_version=os.environ.get("SIDERA_OPENCODE_EXPECTED_VERSION"),
        timeout_seconds=timeout_seconds,
    )
    if (
        preflight.status != "ok"
        or preflight.capability_profile is None
        or preflight.handoff_status != "supported"
    ):
        pytest.fail(f"OpenCode R6.2 preflight failed: {preflight.as_dict()}")
    assert preflight.resolved_executable is not None
    assert handoff_profile_supported(preflight.capability_profile)

    repository, revision = _repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace_id = uuid4()
    task_id = uuid4()
    source_execution_id = uuid4()
    target_execution_id = uuid4()
    source_worker_id = uuid4()
    target_worker_id = uuid4()
    assert source_worker_id != target_worker_id

    source_context = _context_package(source_execution_id)
    transition_id = uuid4()
    target_context = _context_package(
        target_execution_id,
        handoff_instruction=(
            "Resume the transferred WorkerItem from the captured checkpoint. "
            "Do not reinterpret the original request."
        ),
    )
    source_spec = _spec(
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=source_execution_id,
        worker_id=source_worker_id,
        fencing_token=1,
        revision=revision,
        context_package=source_context,
    )
    target_spec = _spec(
        workspace_id=workspace_id,
        task_id=task_id,
        execution_id=target_execution_id,
        worker_id=target_worker_id,
        fencing_token=2,
        revision=revision,
        context_package=target_context,
    )
    source_path = workspace_root / str(source_execution_id)
    target_path = workspace_root / str(target_execution_id)
    source_path.mkdir()
    shutil.copytree(repository, source_path, dirs_exist_ok=True)
    source_manifest = _manifest(
        source_spec,
        location_ref=f"git-worktree://{source_execution_id}",
    )
    target_manifest = _manifest(
        target_spec,
        location_ref=f"git-worktree://{target_execution_id}",
    )
    adapter = OpenCodeACPAdapter(
        executable=preflight.resolved_executable,
        model=model.strip(),
        workspace_root=workspace_root,
        request_timeout_seconds=timeout_seconds,
        handoff_request_timeout_seconds=timeout_seconds,
        stop_grace_seconds=3,
    )
    source_handle = None
    target_handle = None
    source_stop_operation_id = gateway_operation_id(
        source_execution_id, GatewayOperationType.STOP_SESSION
    )
    try:
        adapter.accept_context(source_context, operation_id=uuid4())
        source_handle = adapter.start_session(
            source_spec,
            source_manifest,
            operation_id=gateway_operation_id(
                source_execution_id, GatewayOperationType.START_SESSION
            ),
        )
        accepted = adapter.send_input(
            source_handle,
            "Reply with a short acknowledgement. Do not edit files or call tools.",
            (),
            operation_id=uuid4(),
        )
        assert accepted.outcome is DeliveryOutcome.DELIVERED
        assert accepted.accepted_sequence is not None

        checkpoint = adapter.capture_handoff_checkpoint(
            source_handle,
            transition_id=transition_id,
            source_input_cutoff_sequence=accepted.accepted_sequence,
            operation_id=uuid4(),
        )
        assert checkpoint.source_session_id == source_handle.execution_session_id
        assert checkpoint.accepted_sequence == accepted.accepted_sequence
        assert checkpoint.context_package_id == source_context.metadata.package_id
        assert checkpoint.context_package_hash == source_context.metadata.package_hash
        assert checkpoint.workspace_digest == workspace_manifest_digest(source_manifest)
        assert checkpoint.quiescent is True
        source_dirty_digest = _dirty_digest(source_path)

        source_write = adapter.send_input(
            source_handle,
            "Create a stale source file. This must be rejected.",
            (),
            operation_id=uuid4(),
        )
        assert source_write.outcome is DeliveryOutcome.BLOCKED
        assert source_write.failure_code == "handoff_checkpointed"

        shutil.copytree(source_path, target_path)
        assert _git(source_path, "rev-parse", "HEAD") == _git(
            target_path, "rev-parse", "HEAD"
        )
        assert _dirty_digest(target_path) == source_dirty_digest

        stopped = adapter.stop_session(
            source_handle, operation_id=source_stop_operation_id
        )
        repeated_stop = adapter.stop_session(
            source_handle, operation_id=source_stop_operation_id
        )
        assert stopped.execution_session_id == source_handle.execution_session_id
        assert stopped.accepted_sequence == checkpoint.accepted_sequence
        assert repeated_stop == stopped
        assert not adapter.active_processes()
        assert (
            adapter.inspect_handoff_operation(
                source_handle, operation_id=source_stop_operation_id
            ).outcome
            is HandoffInspectionOutcome.SUCCEEDED
        )

        adapter.accept_context(target_context, operation_id=uuid4())
        target_start_operation_id = gateway_operation_id(
            target_execution_id, GatewayOperationType.START_SESSION
        )
        target_handle = adapter.start_session(
            target_spec,
            target_manifest,
            operation_id=target_start_operation_id,
        )
        assert target_handle.execution_session_id != source_handle.execution_session_id
        assert target_handle.context_binding is not None
        assert target_handle.context_binding.operation_id == target_start_operation_id
        assert (
            target_handle.context_binding.consumer_execution_id == target_execution_id
        )
        assert target_handle.context_binding.package_id == target_context.metadata.package_id
        assert target_handle.context_binding.package_hash == target_context.metadata.package_hash
        assert target_handle.context_binding.context_version == 1
        assert len(adapter.active_processes()) == 1

        blocked = adapter.send_input(
            target_handle,
            "Write before the explicit writer gate. This must be rejected.",
            (),
            operation_id=uuid4(),
        )
        assert blocked.outcome is DeliveryOutcome.BLOCKED
        assert blocked.failure_code == "handoff_writer_disabled"

        enable_operation_id = uuid4()
        enabled = adapter.enable_handoff_writer(
            target_handle,
            fencing_token=target_spec.fencing_token,
            operation_id=enable_operation_id,
        )
        duplicate = adapter.enable_handoff_writer(
            target_handle,
            fencing_token=target_spec.fencing_token,
            operation_id=enable_operation_id,
        )
        assert enabled.outcome is DeliveryOutcome.DELIVERED
        assert duplicate == enabled
        assert enabled.execution_session_id == target_handle.execution_session_id

        marker = target_path / "SIDERA_R6_2_HANDOFF_MARKER.txt"
        resumed = adapter.send_input(
            target_handle,
            (
                "Create SIDERA_R6_2_HANDOFF_MARKER.txt in the current workspace "
                "with exactly the text r6.2 handoff ok and no other file changes."
            ),
            (),
            operation_id=uuid4(),
        )
        assert resumed.outcome is DeliveryOutcome.DELIVERED
        assert marker.read_text().removesuffix("\n") == "r6.2 handoff ok"
        artifacts = adapter.collect_artifacts(target_handle, operation_id=uuid4())
        assert len(artifacts) == 1
        assert artifacts[0].content_hash
        assert str(tmp_path) not in str(artifacts[0].provenance)

        enable_inspection = adapter.inspect_handoff_operation(
            target_handle, operation_id=enable_operation_id
        )
        assert enable_inspection.outcome is HandoffInspectionOutcome.SUCCEEDED
        assert enable_inspection.context_binding == target_handle.context_binding
        unknown_handle = target_handle.__class__(
            execution_session_id=uuid4(),
            adapter_session_id=target_handle.adapter_session_id,
            process_ref=target_handle.process_ref,
            capability_snapshot=target_handle.capability_snapshot,
            last_accepted_sequence=target_handle.last_accepted_sequence,
            started_at=target_handle.started_at,
            context_binding=target_handle.context_binding,
        )
        active_process_count = len(adapter.active_processes())
        unknown = adapter.inspect_handoff_operation(
            unknown_handle, operation_id=uuid4()
        )
        assert unknown.outcome is HandoffInspectionOutcome.UNKNOWN
        assert len(adapter.active_processes()) == active_process_count
    finally:
        _stop_for_cleanup(adapter, target_handle, operation_id=uuid4())
        _stop_for_cleanup(adapter, source_handle, operation_id=source_stop_operation_id)


def _stop_for_cleanup(
    adapter: OpenCodeACPAdapter,
    handle: object,
    *,
    operation_id: UUID,
) -> None:
    if handle is None:
        return
    try:
        adapter.stop_session(handle, operation_id=operation_id)  # type: ignore[arg-type]
    except (NotImplementedError, OSError, RuntimeError, TypeError, ValueError):
        return
