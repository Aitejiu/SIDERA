from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from test_planning_proposals import planning_profile_snapshot

from sidera_control_plane.adapters.fake_planner import (
    FakePlannerAdapter,
    FakePlannerConfig,
    FakeScenario,
)
from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogEntry,
    CapabilityCatalogSnapshot,
    ContextReference,
    PlannerClarificationResult,
    PlannerFailure,
    PlannerInputAssembler,
    PlannerInvalidResult,
    PlannerPolicyConstraints,
    PlannerProposalResult,
    PlannerRunControl,
    PlannerTimeout,
    PlannerUnknownOutcome,
    WorkspaceProviderCapabilitySnapshot,
)
from sidera_control_plane.domain.planning import PlanningBriefRevision

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _input(*, user_feedback: str | None = None):
    profile = planning_profile_snapshot()
    brief = PlanningBriefRevision.create(
        planning_request_id=uuid4(),
        revision=1,
        objective="Implement a planner",
        constraints=(),
        acceptance_criteria=("The planner returns a proposal",),
        context_refs=("repo://sidera",),
        requested_outputs=("proposal",),
        user_feedback=user_feedback,
        source_correlation=uuid4(),
        created_by="operator",
        created_at=NOW,
    )
    catalog = CapabilityCatalogSnapshot(
        snapshot_version=1,
        captured_at=NOW,
        capabilities=(CapabilityCatalogEntry("code", "Implement code"),),
        adapter_preferences=("fake",),
        budget_level="standard",
        max_concurrency=2,
        workspace_providers=(
            WorkspaceProviderCapabilitySnapshot(
                provider="git",
                isolation="filesystem",
                writable=True,
                git_worktree=True,
                stability="stable",
            ),
        ),
    )
    return PlannerInputAssembler().assemble(
        brief=brief,
        profile_snapshot=profile,
        capability_catalog=catalog,
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(profile),
        operation_id=uuid4(),
        planning_request_id=brief.planning_request_id,
        planning_run_id=uuid4(),
        context_refs=(ContextReference("repo://sidera", "Repository context"),),
    )


def _control(planner_input) -> PlannerRunControl:
    return PlannerRunControl(
        operation_id=planner_input.operation_id,
        started_at=NOW,
        deadline_at=datetime(2026, 8, 23, 12, 10, tzinfo=UTC),
    )


def test_fake_planner_requires_explicit_scenario_and_is_deterministic() -> None:
    planner_input = _input()
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="proposal"))

    first = adapter.plan(planner_input, _control(planner_input))
    second = adapter.plan(planner_input, _control(planner_input))

    assert isinstance(first, PlannerProposalResult)
    assert isinstance(second, PlannerProposalResult)
    assert first.proposal.content_hash == second.proposal.content_hash
    assert first.proposal.canonical_payload == second.proposal.canonical_payload
    assert first.proposal.payload.to_mapping() == second.proposal.payload.to_mapping()
    assert first.proposal.payload.schema_version == 2


def test_fake_planner_clarification_then_feedback_returns_valid_proposal() -> None:
    planner_input = _input()
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="clarification"))

    clarification = adapter.plan(planner_input, _control(planner_input))
    assert isinstance(clarification, PlannerClarificationResult)
    assert clarification.clarifications[0].blocking is True

    answered_input = replace(
        planner_input,
        brief=replace(planner_input.brief, user_feedback="Use the fake adapter"),
    )
    proposal = adapter.plan(answered_input, _control(answered_input))

    assert isinstance(proposal, PlannerProposalResult)
    assert proposal.proposal.content_hash


def test_fake_planner_scale_is_deterministic_and_has_a_bounded_large_graph() -> None:
    planner_input = _input()
    planner_input = replace(
        planner_input,
        profile_snapshot=replace(
            planner_input.profile_snapshot,
            budget=replace(planner_input.profile_snapshot.budget, max_nodes=500),
        ),
    )
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="scale"))

    result = adapter.plan(planner_input, _control(planner_input))

    assert isinstance(result, PlannerProposalResult)
    assert len(result.proposal.payload.subtasks) == 200
    assert len(result.proposal.payload.worker_items) == 200
    assert len(result.proposal.payload.execution_intents) == 200
    assert len(result.proposal.payload.subtask_dependencies) == 199
    assert len(result.proposal.payload.worker_item_dependencies) == 199
    assert result.proposal.content_hash


@pytest.mark.parametrize(
    ("scenario", "expected_type"),
    [
        ("invalid_schema", PlannerInvalidResult),
        ("timeout", PlannerTimeout),
        ("unknown_outcome", PlannerUnknownOutcome),
    ],
)
def test_fake_planner_returns_structured_failures(scenario: str, expected_type) -> None:
    planner_input = _input()
    result = FakePlannerAdapter(
        FakePlannerConfig(scenario=cast(FakeScenario, scenario))
    ).plan(
        planner_input, _control(planner_input)
    )

    assert isinstance(result, expected_type)
    assert isinstance(result, PlannerFailure)
    assert result.code
    assert result.safe_message
    assert result.provider_ref == "fake-planner"


def test_fake_planner_cancel_is_explicitly_operation_scoped() -> None:
    adapter = FakePlannerAdapter(FakePlannerConfig(scenario="proposal"))
    operation_id = uuid4()

    adapter.cancel(operation_id)

    assert operation_id in adapter.cancelled_operations
