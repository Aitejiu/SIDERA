from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from sidera_control_plane.persistence.models import (
    NotificationPolicyStateModel,
    NotificationThreadModel,
)

from .result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_repeated_input_failure_creates_one_attention_fact(
    scenario: ResultInputScenario,
) -> None:
    scenario.run_to_released_summary()
    blocked = scenario.block_selected_body()

    assert getattr(blocked, "code", None) == "result_input_unavailable"
    scenario.run_notifications()
    scenario.run_notifications()

    facts = scenario.notification_facts("result_inputs_blocked")
    assert len(facts) == 1
    assert facts[0].policy_version == 2
    with scenario.database.session() as session:
        state = session.scalar(
            select(NotificationPolicyStateModel).where(
                NotificationPolicyStateModel.workspace_id == scenario.workspace_id,
                NotificationPolicyStateModel.policy_version == 2,
                NotificationPolicyStateModel.topic
                == "result_inputs_blocked:result_input_unavailable",
            )
        )
    assert state is not None
    assert state.subject_type == "execution_request"
    assert state.subject_id == str(scenario.request_ids["summary"])
    assert not scenario.notification_facts("execution_attention")


def test_input_failure_recovery_closes_the_same_thread_once(
    scenario: ResultInputScenario,
) -> None:
    scenario.run_to_released_summary()
    scenario.block_selected_body()
    scenario.run_notifications()

    scenario.restore_artifact(scenario.source_artifact_id("b"))
    started = scenario.start_worker("summary")
    assert getattr(started, "session_id", None) is not None
    scenario.run_notifications()
    scenario.run_notifications()

    facts = scenario.notification_facts("result_inputs_blocked")
    assert sorted(fact.transition for fact in facts) == ["opened", "recovered"]
    assert len({fact.semantic_fingerprint for fact in facts}) == 2
    recovered = next(fact for fact in facts if fact.transition == "recovered")
    with scenario.database.session() as session:
        thread = session.scalar(
            select(NotificationThreadModel).where(
                NotificationThreadModel.workspace_id == scenario.workspace_id,
                NotificationThreadModel.topic
                == "result_inputs_blocked:result_input_unavailable",
            )
        )
    assert thread is not None
    assert thread.business_status == "resolved"
    assert thread.latest_fact_id == recovered.id


def test_integrity_failure_keeps_reason_ownership_with_result_input_policy(
    scenario: ResultInputScenario,
) -> None:
    scenario.run_to_released_summary()
    failed = scenario.block_selected_body(failure_mode="integrity")
    assert getattr(failed, "code", None) == "result_input_integrity_failed"

    scenario.run_notifications()

    facts = scenario.notification_facts("result_inputs_blocked")
    assert len(facts) == 1
    assert facts[0].topic == "result_inputs_blocked:result_input_integrity_failed"
    assert facts[0].policy_version == 2
    assert not scenario.notification_facts("execution_attention")
