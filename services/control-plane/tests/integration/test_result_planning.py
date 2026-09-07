from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.persistence.models import ExecutionRequestModel


def test_confirmation_preserves_one_initial_intent_per_worker(
    result_scenario,
) -> None:
    result_scenario.seed_plan()

    assert result_scenario.head().current_candidate_id is None
    with result_scenario.database.session() as session:
        count = session.scalar(
            select(func.count())
            .select_from(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == result_scenario.workspace_id,
                ExecutionRequestModel.plan_snapshot_id
                == result_scenario.plan_snapshot_id,
            )
        )

    assert count == len(result_scenario.worker_ids)


def test_confirmation_replay_is_idempotent(result_scenario) -> None:
    result_scenario.seed_plan()
    first_root_id = result_scenario.root_id
    first_head = result_scenario.head()
    with result_scenario.database.session() as session:
        first_count = session.scalar(
            select(func.count())
            .select_from(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == result_scenario.workspace_id,
                ExecutionRequestModel.plan_snapshot_id
                == result_scenario.plan_snapshot_id,
            )
        )

    result_scenario.reconfirm()

    with result_scenario.database.session() as session:
        second_count = session.scalar(
            select(func.count())
            .select_from(ExecutionRequestModel)
            .where(
                ExecutionRequestModel.workspace_id == result_scenario.workspace_id,
                ExecutionRequestModel.plan_snapshot_id
                == result_scenario.plan_snapshot_id,
            )
        )

    assert result_scenario.root_id == first_root_id
    assert result_scenario.head() == first_head
    assert second_count == first_count == len(result_scenario.worker_ids)


def test_confirmation_rejects_missing_spec_module(result_scenario) -> None:
    with pytest.raises(ValidationError, match="module instance"):
        result_scenario.confirm_with_missing_spec_module()
