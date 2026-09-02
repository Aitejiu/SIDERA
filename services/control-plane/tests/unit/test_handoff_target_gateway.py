from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from tests.unit.test_context_materialization import FakeCatalog, _manifest, _revision, _run

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.worker_gateway import WorkerGateway
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPurpose,
    ControlPlaneFactSection,
)
from sidera_control_plane.domain.gateway import DeliveryOutcome
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
HASH = "a" * 64


def _target_materialization(*, execution_id: UUID):
    revision = _revision()
    materialization = _run(
        ContextMaterializer(FakeCatalog()),
        _manifest(revision),
        revision,
    )
    return replace(
        materialization,
        metadata=replace(
            materialization.metadata,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=execution_id,
            context_version=1,
        ),
        control_plane_facts=ControlPlaneFactSection(
            facts={
                "handoff": {
                    "instruction": "continue the transferred execution",
                }
            }
        ),
    )


def _spec(*, execution_id: UUID, worker_id: UUID, materialization) -> ExecutionSpec:
    from sidera_control_plane.domain.context_packages import context_package_reference

    return ExecutionSpec(
        schema_version=2,
        workspace_id=UUID("10000000-0000-0000-0000-000000000001"),
        task_id=UUID("20000000-0000-0000-0000-000000000001"),
        execution_id=execution_id,
        worker_id=worker_id,
        lease_id=UUID("30000000-0000-0000-0000-000000000001"),
        fencing_token=8,
        goal="Continue the transferred execution",
        acceptance_criteria=("the target resumes safely",),
        context_package_ref=context_package_reference(
            materialization.metadata.package_id,
            materialization.metadata.package_hash,
        ),
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://sidera",
                base_revision=HASH[:40],
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=UUID("40000000-0000-0000-0000-000000000001"),
        adapter_config_version=1,
        workspace_module_ref=UUID("50000000-0000-0000-0000-000000000001"),
        workspace_config_version=1,
        context_package=materialization,
    )


def _ready_workspace(spec: ExecutionSpec) -> ExecutionWorkspaceManifest:
    manifest = ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision=HASH[:40],
                branch=f"sidera/handoff/{spec.execution_id}",
                current_head=HASH[:40],
            ),
        ),
        location_ref="git-worktree://handoff/target",
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=NOW,
    )
    manifest.mark_preparing()
    manifest.mark_ready()
    return manifest


def test_fake_target_session_is_bound_and_read_only_until_writer_enable() -> None:
    execution_id = UUID("60000000-0000-0000-0000-000000000001")
    worker_id = UUID("70000000-0000-0000-0000-000000000001")
    materialization = _target_materialization(execution_id=execution_id)
    spec = _spec(
        execution_id=execution_id,
        worker_id=worker_id,
        materialization=materialization,
    )
    adapter = FakeWorkerAdapter()
    operation_id = UUID("80000000-0000-0000-0000-000000000001")

    handle = adapter.start_session(spec, _ready_workspace(spec), operation_id=operation_id)

    assert handle.context_binding is not None
    assert handle.context_binding.operation_id == operation_id
    assert handle.context_binding.execution_session_id == handle.execution_session_id
    assert handle.context_binding.consumer_execution_id == execution_id
    assert handle.context_binding.package_id == materialization.metadata.package_id
    assert handle.context_binding.package_hash == materialization.metadata.package_hash
    assert handle.context_binding.context_version == 1

    blocked = adapter.send_input(
        handle,
        "this must not become a new prompt",
        (),
        operation_id=uuid4(),
    )
    assert blocked.outcome is DeliveryOutcome.BLOCKED
    assert blocked.failure_code == "handoff_writer_disabled"


def test_fake_target_writer_enable_is_idempotent_and_releases_one_resume() -> None:
    execution_id = UUID("60000000-0000-0000-0000-000000000002")
    worker_id = UUID("70000000-0000-0000-0000-000000000002")
    materialization = _target_materialization(execution_id=execution_id)
    spec = _spec(
        execution_id=execution_id,
        worker_id=worker_id,
        materialization=materialization,
    )
    adapter = FakeWorkerAdapter()
    start_operation_id = UUID("80000000-0000-0000-0000-000000000002")
    handle = adapter.start_session(
        spec,
        _ready_workspace(spec),
        operation_id=start_operation_id,
    )
    enable_operation_id = UUID("90000000-0000-0000-0000-000000000002")

    first = adapter.enable_handoff_writer(
        handle,
        fencing_token=spec.fencing_token,
        operation_id=enable_operation_id,
    )
    duplicate = adapter.enable_handoff_writer(
        handle,
        fencing_token=spec.fencing_token,
        operation_id=enable_operation_id,
    )

    assert first.outcome is DeliveryOutcome.DELIVERED
    assert duplicate == first
    assert first.execution_session_id == handle.execution_session_id
    assert adapter.handoff_resume_count == 1
    assert adapter.handoff_resume_instructions == ("continue the transferred execution",)

    accepted = adapter.send_input(
        handle,
        "follow-up after the handoff resume",
        (),
        operation_id=uuid4(),
    )
    assert accepted.outcome is DeliveryOutcome.DELIVERED


def test_worker_gateway_exposes_target_handoff_operations() -> None:
    assert callable(WorkerGateway.start_handoff_target)
    assert callable(WorkerGateway.enable_handoff_target_writer)
