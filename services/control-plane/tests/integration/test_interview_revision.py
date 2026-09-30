from __future__ import annotations

import os
from dataclasses import replace
from uuid import UUID

import pytest

if os.environ.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
    raise RuntimeError(
        "test_interview_revision.py must run through the isolated demo test runner"
    )

from sidera_control_plane.adapters.demo_planner import (
    DemoPlannerAdapter,
    build_revision_proposal,
)
from sidera_control_plane.application.planner_runtime import (
    PlannerProposalResult,
    PlannerRunControl,
)
from sidera_control_plane.demo.test_runner import validate_interview_test_context
from sidera_control_plane.domain.plan_revisions import IdentityStrategy
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.database import Database
from tests.unit.test_interview_planner import _planner_input
from tests.unit.test_interview_revision import _context

pytestmark = pytest.mark.integration


def test_revision_is_bound_to_current_snapshot_and_round_trips_as_v6(
    database: Database,
    clean_database: None,
) -> None:
    del database, clean_database
    manifest = validate_interview_test_context(os.environ)
    context = _context(manifest)
    base_snapshot = TaskPlanSnapshot.from_payload(context.plan_snapshot_payload)
    adapter = DemoPlannerAdapter(manifest)
    planner_input = _planner_input(manifest, feedback="还需要支持 TSV 文件")
    planner_input = replace(
        planner_input,
        kind=PlanningRequestKind.REVISION,
        planning_context=context,
    )
    result = adapter.plan(
        planner_input,
        PlannerRunControl(
            operation_id=planner_input.operation_id,
            started_at=planner_input.brief.created_at,
            deadline_at=planner_input.brief.created_at,
            cancel_requested=False,
        ),
    )

    assert isinstance(result, PlannerProposalResult)
    payload = result.proposal.payload
    assert isinstance(payload, PlanningRevisionProposalPayloadV6)
    assert payload.base_descriptor.plan_snapshot_id == base_snapshot.id
    assert payload.base_descriptor.plan_generation == base_snapshot.generation
    assert payload.base_descriptor.plan_content_hash == context.base_descriptor.plan_content_hash
    assert {item.key for item in payload.subtasks} >= {"validation", "reporting", "integration", "parse-tsv"}
    assert {item.key for item in payload.worker_items} >= {
        "validate",
        "report",
        "review-report",
        "integrate",
        "parse-tsv",
    }
    identity = {item.target_worker_item_key: item for item in payload.worker_item_identity_decisions}
    assert identity["report"].strategy is IdentityStrategy.REUSE
    assert identity["review-report"].strategy is IdentityStrategy.REUSE
    assert identity["report"].existing_worker_item_id == base_snapshot.worker_item_plans[1].worker_item_id
    assert identity["parse-tsv"].strategy is IdentityStrategy.CREATE
    assert ("parse-tsv", "integration") in {
        (item.upstream_subtask_key, item.downstream_subtask_key)
        for item in payload.subtask_dependencies
    }

    parsed = PlanningRevisionProposalPayloadV6.from_mapping(payload.to_mapping())
    assert parsed.to_mapping() == payload.to_mapping()
    assert base_snapshot.generation == 1
    assert UUID(str(context.base_descriptor.root_task_id)) == base_snapshot.root_task_id


def test_revision_proposal_does_not_mutate_the_previous_snapshot() -> None:
    from pathlib import Path

    from tests.unit.test_interview_revision import _manifest

    manifest = _manifest(Path("/tmp"))
    context = _context(manifest)
    before = TaskPlanSnapshot.from_payload(context.plan_snapshot_payload).to_payload()
    proposal = build_revision_proposal(manifest, context)
    assert TaskPlanSnapshot.from_payload(context.plan_snapshot_payload).to_payload() == before
    assert proposal.base_descriptor.plan_generation == 1
