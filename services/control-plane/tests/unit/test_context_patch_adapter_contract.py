from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.domain.context_patches import (
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
    ContextPatchV1,
)
from sidera_control_plane.domain.gateway import (
    AdapterSessionHandle,
    DeliveryOutcome,
    context_patch_operation_id,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
)

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("40000000-0000-0000-0000-000000000001")
PLAN_TRANSITION_ID = UUID("60000000-0000-0000-0000-000000000001")
WORKER_TRANSITION_ID = UUID("70000000-0000-0000-0000-000000000001")
BASE_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")
TARGET_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000002")
PACKAGE_ID = UUID("80000000-0000-0000-0000-000000000001")
CONTEXT_REVISION_ID = UUID("90000000-0000-0000-0000-000000000001")


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _patch(*, created_at: datetime, expires_at: datetime) -> ContextPatchV1:
    return ContextPatchV1.create(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        plan_transition_id=PLAN_TRANSITION_ID,
        worker_transition_id=WORKER_TRANSITION_ID,
        base_plan_snapshot_id=BASE_SNAPSHOT_ID,
        base_generation=4,
        target_plan_snapshot_id=TARGET_SNAPSHOT_ID,
        target_generation=5,
        base_context_package_id=PACKAGE_ID,
        base_package_hash="a" * 64,
        base_work_context_revision_id=CONTEXT_REVISION_ID,
        base_context_hash="b" * 64,
        target_context_version=2,
        changed_requirements=("python",),
        changed_acceptance_refs=("acceptance:new",),
        changed_dependency_refs=(),
        decision_refs=("decision:1",),
        artifact_refs=("artifact:1",),
        evidence_refs=("evidence:1",),
        intervention_refs=("intervention:1",),
        unfinished_work="Finish the revised acceptance check.",
        checkpoint_ref="checkpoint://worker/1",
        proposer="user-1",
        expires_at=expires_at,
        created_at=created_at,
    )


def _spec() -> ExecutionSpec:
    return ExecutionSpec(
        schema_version=1,
        workspace_id=WORKSPACE_ID,
        task_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Apply a context patch",
        acceptance_criteria=("the patch is accepted at a safe boundary",),
        context_package_ref=None,
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=None,
        expected_outputs=("diff",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
    )


def _workspace(spec: ExecutionSpec) -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="a" * 40,
                branch=f"sidera/execution/{spec.execution_id}",
                current_head="a" * 40,
            ),
        ),
        location_ref=f"fake-workspace://{spec.execution_id}",
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=_now(),
    )


def _handle(adapter: FakeWorkerAdapter) -> AdapterSessionHandle:
    spec = _spec()
    return adapter.start_session(spec, _workspace(spec), operation_id=uuid4())


def test_fake_adapter_advertises_and_applies_context_patch_exactly_once() -> None:
    adapter = FakeWorkerAdapter()
    assert adapter.probe({}).context_patch_versions == (1,)
    handle = _handle(adapter)
    now = _now()
    materialization = ContextPatchMaterializationV1(
        schema_version=1,
        patch=_patch(created_at=now, expires_at=now + timedelta(minutes=10)),
    )
    operation_id = context_patch_operation_id(
        WORKER_TRANSITION_ID, "apply_patch", logical_attempt=1
    )

    first = adapter.apply_context_patch(
        handle, materialization, operation_id=operation_id
    )
    duplicate = adapter.apply_context_patch(
        handle, materialization, operation_id=operation_id
    )

    assert isinstance(first, ContextPatchDeliveryResult)
    assert first.outcome == "delivered"
    assert first.operation_id == operation_id
    assert first.patch_hash == materialization.patch.patch_hash
    assert first.accepted_sequence == 2
    assert duplicate == first
    assert adapter.applied_context_patches == (materialization,)


@pytest.mark.parametrize(
    ("outcome", "error_code"),
    (
        (DeliveryOutcome.BLOCKED, "prompt_busy"),
        (DeliveryOutcome.FAILED, "patch_rejected"),
        (DeliveryOutcome.UNKNOWN, "response_lost"),
    ),
)
def test_fake_adapter_exposes_patch_delivery_classification(
    outcome: DeliveryOutcome, error_code: str
) -> None:
    adapter = FakeWorkerAdapter()
    handle = _handle(adapter)
    now = _now()
    materialization = ContextPatchMaterializationV1(
        schema_version=1,
        patch=_patch(created_at=now, expires_at=now + timedelta(minutes=10)),
    )
    operation_id = uuid4()
    adapter.inject_patch_outcome(outcome, error_code=error_code)

    result = adapter.apply_context_patch(
        handle, materialization, operation_id=operation_id
    )

    assert result.outcome is outcome.value
    assert result.operation_id == operation_id
    assert result.patch_hash == materialization.patch.patch_hash
    assert result.accepted_sequence is None
    assert result.error_code == error_code
    assert adapter.applied_context_patches == ()


@pytest.mark.parametrize("outcome", [DeliveryOutcome.ACCEPTED, "accepted"])
def test_fake_patch_rejects_accepted_before_mutating_outcome_queue(outcome) -> None:
    adapter = FakeWorkerAdapter()
    handle = _handle(adapter)
    now = _now()
    materialization = ContextPatchMaterializationV1(
        schema_version=1,
        patch=_patch(created_at=now, expires_at=now + timedelta(minutes=10)),
    )
    adapter.inject_patch_outcome(DeliveryOutcome.BLOCKED, error_code="prompt_busy")
    with pytest.raises(ValueError, match="unsupported patch outcome"):
        adapter.inject_patch_outcome(outcome)

    first = adapter.apply_context_patch(handle, materialization, operation_id=uuid4())
    second = adapter.apply_context_patch(handle, materialization, operation_id=uuid4())
    assert first.outcome == "blocked" and first.error_code == "prompt_busy"
    assert second.outcome == "delivered" and second.accepted_sequence == 2
    assert adapter.applied_context_patches == (materialization,)


@pytest.mark.parametrize("outcome", [DeliveryOutcome.DELIVERED, "delivered"])
def test_fake_patch_accepts_explicit_delivered_outcome(outcome) -> None:
    adapter = FakeWorkerAdapter()
    handle = _handle(adapter)
    now = _now()
    materialization = ContextPatchMaterializationV1(
        schema_version=1,
        patch=_patch(created_at=now, expires_at=now + timedelta(minutes=10)),
    )
    adapter.inject_patch_outcome(outcome)
    result = adapter.apply_context_patch(handle, materialization, operation_id=uuid4())
    assert result.outcome == "delivered" and result.accepted_sequence == 2
    assert result.error_code is None
    assert adapter.applied_context_patches == (materialization,)


def test_fake_adapter_rejects_expired_patch_before_execution() -> None:
    adapter = FakeWorkerAdapter()
    handle = _handle(adapter)
    now = _now()
    materialization = ContextPatchMaterializationV1(
        schema_version=1,
        patch=_patch(
            created_at=now - timedelta(minutes=20),
            expires_at=now - timedelta(minutes=10),
        ),
    )

    result = adapter.apply_context_patch(
        handle, materialization, operation_id=uuid4()
    )

    assert result.outcome == "failed"
    assert result.error_code == "context_patch_expired"
    assert adapter.applied_context_patches == ()


def test_opencode_patch_capability_is_explicitly_unsupported() -> None:
    assert OpenCodeACPAdapter._PROFILE.context_patch_versions == ()


def test_patch_operation_id_is_stable_and_separate_from_input_operation() -> None:
    worker_transition_id = uuid5(NAMESPACE_URL, "worker-transition")
    first = context_patch_operation_id(
        worker_transition_id, "apply_patch", logical_attempt=1
    )
    duplicate = context_patch_operation_id(
        worker_transition_id, "apply_patch", logical_attempt=1
    )
    next_attempt = context_patch_operation_id(
        worker_transition_id, "apply_patch", logical_attempt=2
    )

    assert first == duplicate
    assert first != next_attempt
