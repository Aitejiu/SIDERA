from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.e2e.r6_3_1_fake_runtime import CollaborationScenario
from tests.e2e.seed_r6_3_1_collaboration import seed_collaboration_plan

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("clean_database"),
]

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_two_upstreams_fan_in_on_required_evidence(
    database,
    workspace_id,
) -> None:
    seed = seed_collaboration_plan(database, workspace_id, now=NOW)
    scenario = CollaborationScenario(database, seed, now=NOW)

    scenario.confirm_v4_plan()
    scenario.run_collaboration_until_idle()
    assert scenario.released_keys() == {"upstream-a", "upstream-b"}

    scenario.succeed("upstream-a")
    scenario.run_collaboration_until_idle()
    assert "downstream" not in scenario.released_keys()

    scenario.succeed("upstream-b", evidence=())
    scenario.run_collaboration_until_idle()
    assert scenario.worker_state("downstream") == "waiting_evidence"

    scenario.record_required_evidence("upstream-b", "verification-report")
    scenario.run_collaboration_until_idle()
    assert "downstream" in scenario.released_keys()

    scenario.run_existing_scheduler_and_workers()
    assert scenario.run_status() == "completed"
