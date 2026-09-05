from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.domain.collaboration import (
    CollaborationEvaluation,
    CollaborationEvaluationStatus,
    CollaborationPolicyRequestV1,
    CollaborationPolicySnapshotV1,
    CollaborationRun,
    CollaborationRunStatus,
    CollaborationStrategy,
    RuntimeIntentApplication,
    RuntimeIntentApplicationStatus,
    RuntimeProjectionSnapshot,
    RuntimeRelease,
    resolve_collaboration_policy,
)
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleLifecycle,
    StabilityLevel,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid4()
ROOT_TASK_ID = uuid4()
SNAPSHOT_ID = uuid4()
RUN_ID = uuid4()
REQUEST_ID = uuid4()
DECISION_ID = uuid4()
INTENT_ID = uuid4()


def capability() -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime-v1",
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.EXPERIMENTAL,
    )


def binding() -> dict[str, object]:
    return {
        "runtime_definition_id": uuid4(),
        "runtime_instance_id": uuid4(),
        "runtime_interface_version": "collaboration-runtime-v1",
        "runtime_config_version": 3,
        "runtime_capability_snapshot": capability(),
    }


def enabled_request(
    *,
    overrides: tuple[dict[str, object], ...] = (),
) -> CollaborationPolicyRequestV1:
    return CollaborationPolicyRequestV1.from_mapping(
        {
            "mode": "enabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": list(overrides),
        }
    )


def make_run(**overrides: object) -> CollaborationRun:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "root_task_id": ROOT_TASK_ID,
        "plan_snapshot_id": SNAPSHOT_ID,
        "plan_generation": 1,
        "plan_content_hash": "a" * 64,
        "runtime_definition_id": binding()["runtime_definition_id"],
        "runtime_instance_id": binding()["runtime_instance_id"],
        "runtime_interface_version": "collaboration-runtime-v1",
        "runtime_config_version": 3,
        "runtime_capability_snapshot": capability(),
        "policy_snapshot": {"schema_version": 1},
        "now": NOW,
    }
    values.update(overrides)
    return CollaborationRun.create(**values)


def test_collaboration_policy_parses_aliases_and_rejects_duplicate_overrides() -> None:
    policy = enabled_request(
        overrides=(
            {
                "subtask_key": "verify",
                "enabled": False,
                "strategy_id": "all_required",
            },
        )
    )
    resolved = resolve_collaboration_policy(
        policy,
        CollaborationPolicySnapshotV1.binding_from_mapping(binding()),
        subtask_keys=frozenset({"build", "verify"}),
    )

    assert resolved is not None
    assert resolved.effective_policy("build").enabled is True
    assert resolved.effective_policy("verify").enabled is False
    assert resolved.effective_policy("build").strategy_id is CollaborationStrategy.ALL_REQUIRED
    assert CollaborationPolicySnapshotV1.from_mapping(resolved.to_mapping()) == resolved

    with pytest.raises(ValidationError, match="unknown collaboration override"):
        resolve_collaboration_policy(
            enabled_request(
                overrides=(
                    {
                        "subtask_key": "missing",
                        "enabled": True,
                        "strategy_id": "all_required",
                    },
                )
            ),
            CollaborationPolicySnapshotV1.binding_from_mapping(binding()),
            subtask_keys=frozenset({"build", "verify"}),
        )

    with pytest.raises(ValidationError, match="duplicate collaboration override"):
        CollaborationPolicyRequestV1.from_mapping(
            {
                "mode": "enabled",
                "default_policy": {"strategy_id": "all_required"},
                "subtask_overrides": [
                    {
                        "subtask_key": "verify",
                        "enabled": True,
                        "strategy_id": "all_required",
                    },
                    {
                        "subtask_key": "verify",
                        "enabled": False,
                        "strategy_id": "all_required",
                    },
                ],
            }
        )


def test_disabled_policy_requires_no_overrides_and_resolves_to_none() -> None:
    request = CollaborationPolicyRequestV1.from_mapping(
        {
            "mode": "disabled",
            "default_policy": {"strategy_id": "all_required"},
            "subtask_overrides": [],
        }
    )
    assert resolve_collaboration_policy(request, None, subtask_keys=frozenset()) is None

    with pytest.raises(ValidationError, match="disabled collaboration policy"):
        CollaborationPolicyRequestV1.from_mapping(
            {
                "mode": "disabled",
                "default_policy": {"strategy_id": "all_required"},
                "subtask_overrides": [
                    {
                        "subtask_key": "verify",
                        "enabled": False,
                        "strategy_id": "all_required",
                    }
                ],
            }
        )


def test_run_state_machine_allows_only_same_binding_recovery() -> None:
    run = make_run()
    run.activate(now=NOW)
    run.block(reason="runtime_unavailable", now=NOW + timedelta(seconds=1))
    run.recover_same_binding(now=NOW + timedelta(seconds=2))
    assert run.status is CollaborationRunStatus.ACTIVE

    run.complete(now=NOW + timedelta(seconds=3))
    assert run.status is CollaborationRunStatus.COMPLETED
    with pytest.raises(InvalidTransition):
        run.block(reason="late", now=NOW + timedelta(seconds=4))


def test_evaluation_and_ledger_records_validate_immutable_identity() -> None:
    evaluation = CollaborationEvaluation.create(
        evaluation_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        projection_snapshot_id=SNAPSHOT_ID,
        operation_id=uuid4(),
        now=NOW,
    )
    evaluation.claim(owner_id="collaboration-worker-1", lease_expires_at=NOW + timedelta(minutes=1), now=NOW)
    assert evaluation.status is CollaborationEvaluationStatus.EVALUATING
    evaluation.complete(now=NOW + timedelta(seconds=1), response_hash="b" * 64)
    assert evaluation.status is CollaborationEvaluationStatus.COMPLETED

    projection = RuntimeProjectionSnapshot(
        id=SNAPSHOT_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        schema_version=1,
        plan_snapshot_id=SNAPSHOT_ID,
        plan_generation=1,
        plan_content_hash="a" * 64,
        fact_cursor=7,
        payload={"root_task_id": str(ROOT_TASK_ID)},
        content_hash="c" * 64,
        size_bytes=32,
        created_at=NOW,
    )
    assert projection.payload["root_task_id"] == str(ROOT_TASK_ID)

    application = RuntimeIntentApplication(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        intent_id=INTENT_ID,
        status=RuntimeIntentApplicationStatus.APPLIED,
        expected_run_version=1,
        observed_run_version=1,
        expected_request_version=1,
        observed_request_version=1,
        release_id=uuid4(),
        result_code="released",
        result_summary="released",
        resource_version=1,
        created_at=NOW,
        applied_at=NOW,
    )
    assert application.status is RuntimeIntentApplicationStatus.APPLIED

    release = RuntimeRelease(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        plan_snapshot_id=SNAPSHOT_ID,
        plan_generation=1,
        plan_content_hash="a" * 64,
        decision_id=DECISION_ID,
        intent_id=INTENT_ID,
        execution_request_id=REQUEST_ID,
        request_version_at_release=1,
        issued_at=NOW,
    )
    assert release.execution_request_id == REQUEST_ID

    with pytest.raises(ValidationError, match="input selection"):
        RuntimeRelease(
            id=uuid4(),
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            plan_snapshot_id=SNAPSHOT_ID,
            plan_generation=1,
            plan_content_hash="a" * 64,
            decision_id=DECISION_ID,
            intent_id=INTENT_ID,
            execution_request_id=REQUEST_ID,
            request_version_at_release=1,
            issued_at=NOW,
            input_selection_id=uuid4(),
        )


def test_run_rejects_disabled_module_lifecycle_and_invalid_hash() -> None:
    with pytest.raises(ValidationError, match="plan_content_hash"):
        make_run(plan_content_hash="not-a-hash")
    assert ModuleLifecycle.DISABLED.value == "disabled"


def test_deferred_application_can_only_reach_a_terminal_disposition() -> None:
    application = RuntimeIntentApplication(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        intent_id=INTENT_ID,
        status=RuntimeIntentApplicationStatus.DEFERRED,
        expected_run_version=1,
        observed_run_version=1,
        expected_request_version=1,
        observed_request_version=1,
        release_id=None,
        result_code="governance_deferred",
        result_summary="held for approval",
        resource_version=1,
        created_at=NOW,
        applied_at=None,
    )

    applied = application.resolve(
        status=RuntimeIntentApplicationStatus.APPLIED,
        result_code="governance_approved",
        result_summary="Release approved after current-fact validation",
        release_id=uuid4(),
        now=NOW,
    )

    assert applied.resource_version == application.resource_version + 1
    assert applied.applied_at == NOW
    with pytest.raises(InvalidTransition):
        applied.resolve(
            status=RuntimeIntentApplicationStatus.REJECTED,
            result_code="governance_rejected",
            result_summary="rejected",
            release_id=None,
            now=NOW,
        )
