from datetime import UTC, datetime
from uuid import uuid4

from tests.unit.test_planning_proposals import planning_profile_snapshot

from sidera_control_plane.application.planner_runtime import (
    CapabilityCatalogEntry,
    CapabilityCatalogSnapshot,
    ContextReference,
    PlannerInputAssembler,
    PlannerPolicyConstraints,
    PlannerRunControl,
    WorkspaceProviderCapabilitySnapshot,
)
from sidera_control_plane.domain.planning import PlanningBriefRevision

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _brief() -> PlanningBriefRevision:
    return PlanningBriefRevision.create(
        planning_request_id=uuid4(),
        revision=2,
        objective="Implement a recoverable planning loop",
        constraints=("Keep planner calls outside the API transaction",),
        acceptance_criteria=("The proposal is typed",),
        context_refs=("repo://sidera/design",),
        requested_outputs=("proposal",),
        user_feedback="Use the existing control plane",
        source_correlation=uuid4(),
        created_by="operator",
        created_at=NOW,
    )


def _catalog() -> CapabilityCatalogSnapshot:
    return CapabilityCatalogSnapshot(
        snapshot_version=4,
        captured_at=NOW,
        capabilities=(
            CapabilityCatalogEntry(name="code", description="Implement code"),
            CapabilityCatalogEntry(name="test", description="Run verification"),
        ),
        adapter_preferences=("opencode", "fake"),
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


def test_planner_input_assembler_builds_a_fixed_safe_snapshot() -> None:
    profile = planning_profile_snapshot()
    brief = _brief()
    operation_id = uuid4()
    request_id = brief.planning_request_id
    run_id = uuid4()

    planner_input = PlannerInputAssembler().assemble(
        brief=brief,
        profile_snapshot=profile,
        capability_catalog=_catalog(),
        policy_constraints=PlannerPolicyConstraints.from_profile_snapshot(profile),
        operation_id=operation_id,
        planning_request_id=request_id,
        planning_run_id=run_id,
        context_refs=(
            ContextReference(
                ref="repo://sidera/design",
                safe_summary="The design contract for the control plane.",
            ),
        ),
    )

    assert planner_input.interface_version == "sidera.planner.v1"
    assert planner_input.proposal_schema_version == 2
    assert planner_input.operation_id == operation_id
    assert planner_input.brief_revision == 2
    assert planner_input.planning_request_id == request_id
    assert planner_input.planning_run_id == run_id
    assert planner_input.context_refs[0].ref == "repo://sidera/design"
    assert planner_input.context_refs[0].safe_summary is not None
    assert planner_input.context_refs[0].safe_summary.startswith("The design")
    assert planner_input.policy_constraints.allowed_capabilities == (
        "code",
        "test",
        "docs",
    )
    assert planner_input.capability_catalog.snapshot_version == 4
    assert not hasattr(planner_input, "session")
    assert not hasattr(planner_input, "lease")


def test_planner_run_control_is_explicit_and_operation_scoped() -> None:
    operation_id = uuid4()
    control = PlannerRunControl(
        operation_id=operation_id,
        started_at=NOW,
        deadline_at=datetime(2026, 8, 23, 12, 10, tzinfo=UTC),
    )

    assert control.operation_id == operation_id
    assert control.cancel_requested is False
