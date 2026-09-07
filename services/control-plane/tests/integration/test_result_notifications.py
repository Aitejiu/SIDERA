from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.notification_policy import NotificationObservation
from sidera_control_plane.application.result_notifications import (
    result_notification_facts,
)

pytestmark = pytest.mark.integration

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("00000000-0000-0000-0000-000000000010")
SUBTASK_ID = UUID("00000000-0000-0000-0000-000000000011")
WORKER_ITEM_ID = UUID("00000000-0000-0000-0000-000000000012")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000020")


def _event(event_type: str) -> dict[str, object]:
    return {
        "event_type": event_type,
        "event_id": str(uuid4()),
        "workspace_id": str(WORKSPACE_ID),
        "root_task_id": str(ROOT_TASK_ID),
        "correlation_id": str(uuid4()),
        "cursor": 42,
        "resource_version": 3,
        "occurred_at": datetime(2026, 9, 8, tzinfo=UTC),
    }


def _overview(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "workspace_id": str(WORKSPACE_ID),
        "root_task_id": str(ROOT_TASK_ID),
        "task_title": "交付结果通知",
        "task_status": "running",
        "task_version": 3,
        "result_version": 4,
        "result_stage": "awaiting_acceptance",
        "current_candidate": {
            "candidate_id": str(CANDIDATE_ID),
            "candidate_hash": "a" * 64,
        },
        "acceptance": None,
        "active_operation": None,
        "criteria": [
            {
                "criterion_key": "tests",
                "text": "检查通过",
                "mode": "automatic",
                "observed_status": "passed",
                "verification_status": "passed",
            }
        ],
        "blocking_reasons": [],
        "available_actions": [
            {
                "action": "accept_result",
                "command_type": "CompleteTask",
            }
        ],
        "last_change_cursor": 42,
    }
    value.update(overrides)
    return value


def test_acceptance_notification_names_root_and_candidate_without_completion(
    result_scenario,
) -> None:
    del result_scenario
    observations = result_notification_facts(
        _event("IntegrationCandidatePrepared"), _overview()
    )

    assert len(observations) == 1
    observation = observations[0]
    assert isinstance(observation, NotificationObservation)
    assert observation.root_task_id == ROOT_TASK_ID
    assert observation.subtask_id is None
    assert observation.worker_item_id is None
    assert observation.topic == f"result:{CANDIDATE_ID}:awaiting_acceptance"
    assert observation.requires_operator is True
    assert observation.deep_link.endswith(
        f"/tasks/{ROOT_TASK_ID}?view=results&candidate={CANDIDATE_ID}"
    )
    assert observation.authoritative_fingerprint == result_notification_facts(
        _event("IntegrationCandidatePrepared"), _overview()
    )[0].authoritative_fingerprint


def test_local_result_failure_targets_the_declared_worker_item() -> None:
    overview = _overview(
        result_stage="attention",
        blocking_reasons=["verification_failed"],
        attention_items=[
            {
                "cause_key": "result:verification_failed",
                "subtask_id": str(SUBTASK_ID),
                "subtask_title": "实现登录校验",
                "worker_item_id": str(WORKER_ITEM_ID),
                "worker_item_title": "登录实现 Worker",
                "resource_version": 9,
            }
        ],
    )
    observations = result_notification_facts(
        _event("VerificationResultFinished"), overview
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.root_task_id == ROOT_TASK_ID
    assert observation.subtask_id == SUBTASK_ID
    assert observation.worker_item_id == WORKER_ITEM_ID
    assert observation.topic == "result:verification_failed"
    assert observation.subtask_path == ("实现登录校验",)
    assert observation.worker_item_title == "登录实现 Worker"
    assert observation.requires_operator is True


def test_replayed_event_has_one_stable_observation_and_logs_are_silent() -> None:
    event = _event("VerificationResultFinished")
    overview = _overview(
        result_stage="attention", blocking_reasons=["verification_failed"]
    )
    first = result_notification_facts(event, overview)
    second = result_notification_facts(event, overview)

    assert len(first) == len(second) == 1
    assert first[0].authoritative_fingerprint == second[0].authoritative_fingerprint
    assert result_notification_facts(
        _event("WorkerTraceChunkAppended"), overview
    ) == ()


@pytest.mark.parametrize("event_type", ("TaskAcceptanceRecorded", "TaskCompleted"))
def test_acceptance_and_completion_events_do_not_duplicate_root_completion(
    event_type: str,
) -> None:
    observations = result_notification_facts(
        _event(event_type), _overview(result_stage="finalizing")
    )

    assert all(item.kind != "task_completed" for item in observations)
    assert result_notification_facts(_event("TaskCompleted"), _overview()) == ()
