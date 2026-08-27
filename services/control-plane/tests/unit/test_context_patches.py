from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.context_patches import (
    ContextPatchAcknowledgement,
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
    ContextPatchV1,
)
from sidera_control_plane.domain.errors import ValidationError

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")
PLAN_TRANSITION_ID = UUID("60000000-0000-0000-0000-000000000001")
WORKER_TRANSITION_ID = UUID("70000000-0000-0000-0000-000000000001")
BASE_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000001")
TARGET_SNAPSHOT_ID = UUID("10000000-0000-0000-0000-000000000002")
PACKAGE_ID = UUID("80000000-0000-0000-0000-000000000001")
CONTEXT_REVISION_ID = UUID("90000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("a0000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("b0000000-0000-0000-0000-000000000001")


def patch() -> ContextPatchV1:
    return ContextPatchV1.create(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        worker_item_id=WORKER_ID,
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
        artifact_refs=("artifact_type:patch",),
        evidence_refs=("acceptance:new",),
        intervention_refs=("intervention:1",),
        unfinished_work="Finish the revised acceptance check.",
        checkpoint_ref="checkpoint://worker/1",
        proposer="user-1",
        expires_at=NOW + timedelta(minutes=10),
        created_at=NOW,
    )


def test_context_patch_hash_and_materialization_are_exact() -> None:
    value = patch()
    assert len(value.patch_hash) == 64
    assert value.patch_hash == value.content_hash
    assert value.scope == "worker_item"

    materialized = ContextPatchMaterializationV1(schema_version=1, patch=value)
    assert materialized.to_mapping()["schema_version"] == 1

    with pytest.raises(ValidationError, match="context patch hash"):
        raw = {
            field: getattr(value, field)
            for field in value.__dataclass_fields__
            if field != "patch_hash"
        }
        ContextPatchV1(
            **raw,
            patch_hash="c" * 64,
        )


def test_context_patch_checks_expiry_and_exact_base_package() -> None:
    value = patch()
    value.assert_deliverable(
        now=NOW,
        acknowledged_package_id=PACKAGE_ID,
        acknowledged_package_hash="a" * 64,
    )

    with pytest.raises(ValidationError, match="base package"):
        value.assert_deliverable(
            now=NOW,
            acknowledged_package_id=uuid4(),
            acknowledged_package_hash="a" * 64,
        )
    with pytest.raises(ValidationError, match="expired"):
        value.assert_deliverable(
            now=NOW + timedelta(minutes=11),
            acknowledged_package_id=PACKAGE_ID,
            acknowledged_package_hash="a" * 64,
        )


def test_context_patch_acknowledgement_requires_exact_patch_execution_and_session() -> None:
    value = patch()
    acknowledgement = ContextPatchAcknowledgement.create(
        patch=value,
        execution_id=EXECUTION_ID,
        session_id=SESSION_ID,
        operation_id=uuid4(),
        accepted_sequence=3,
        adapter_ref="fake://adapter",
        acknowledged_at=NOW,
    )
    acknowledgement.assert_matches(
        value,
        execution_id=EXECUTION_ID,
        session_id=SESSION_ID,
    )

    with pytest.raises(ValidationError, match="patch acknowledgement hash"):
        acknowledgement.assert_matches(
            value,
            execution_id=EXECUTION_ID,
            session_id=SESSION_ID,
            patch_hash="d" * 64,
        )


def test_context_patch_delivery_result_requires_sequence_only_for_delivery() -> None:
    operation_id = uuid4()
    delivered = ContextPatchDeliveryResult(
        outcome="delivered",
        operation_id=operation_id,
        patch_hash="a" * 64,
        accepted_sequence=4,
        error_code=None,
    )
    assert delivered.accepted_sequence == 4

    with pytest.raises(ValidationError, match="accepted_sequence"):
        ContextPatchDeliveryResult(
            outcome="delivered",
            operation_id=operation_id,
            patch_hash="a" * 64,
            accepted_sequence=None,
            error_code=None,
        )
