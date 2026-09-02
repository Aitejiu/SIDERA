from types import SimpleNamespace
from uuid import uuid4

from sidera_control_plane.application.collaboration_decisions import (
    CollaborationGateFact,
    ComposedActivationGate,
)
from sidera_control_plane.domain.collaboration import (
    CollaborationGateMode,
    CollaborationRunStatus,
)
from sidera_control_plane.domain.execution_requests import ActivationDecision


def test_composed_gate_reports_dependencies_before_missing_release() -> None:
    decision = ComposedActivationGate.evaluate(
        plan_transition_allows=True,
        static_decision=ActivationDecision(
            ready=False,
            reason="dependencies_blocked",
        ),
        collaboration=CollaborationGateFact.release_pending(),
    )

    assert decision.reason == "dependencies_blocked"


def test_composed_gate_reports_plan_generation_before_dependencies() -> None:
    decision = ComposedActivationGate.evaluate(
        plan_transition_allows=False,
        static_decision=ActivationDecision(
            ready=False,
            reason="dependencies_blocked",
        ),
        collaboration=CollaborationGateFact.release_pending(),
    )

    assert decision.reason == "plan_generation_stale"


def test_composed_gate_reports_missing_release_after_static_facts_are_ready() -> None:
    decision = ComposedActivationGate.evaluate(
        plan_transition_allows=True,
        static_decision=ActivationDecision(ready=True, reason="ready"),
        collaboration=CollaborationGateFact.release_pending(),
    )

    assert not decision.ready
    assert decision.reason == "collaboration_release_pending"


def test_release_is_allowed_only_for_the_exact_active_run_and_plan_head() -> None:
    workspace_id = uuid4()
    root_task_id = uuid4()
    plan_snapshot_id = uuid4()
    run_id = uuid4()
    request_id = uuid4()
    plan_hash = "a" * 64
    request = SimpleNamespace(
        id=request_id,
        workspace_id=workspace_id,
        plan_snapshot_id=plan_snapshot_id,
        plan_generation=3,
        version=2,
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
    )
    run = SimpleNamespace(
        id=run_id,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        plan_snapshot_id=plan_snapshot_id,
        plan_generation=3,
        plan_content_hash=plan_hash,
        status=CollaborationRunStatus.ACTIVE,
    )
    head = SimpleNamespace(
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        active_snapshot_id=plan_snapshot_id,
        active_generation=3,
        active_snapshot_hash=plan_hash,
    )
    release = SimpleNamespace(
        workspace_id=workspace_id,
        run_id=run_id,
        plan_snapshot_id=plan_snapshot_id,
        plan_generation=3,
        plan_content_hash=plan_hash,
        execution_request_id=request_id,
        request_version_at_release=1,
    )

    fact = CollaborationGateFact.for_request(
        request,
        run=run,
        release=release,
        plan_head=head,
    )

    assert fact.allows
    assert (
        CollaborationGateFact.for_request(
            request,
            run=SimpleNamespace(**{**run.__dict__, "plan_generation": 2}),
            release=release,
            plan_head=head,
        ).reason
        == "collaboration_release_pending"
    )


def test_release_required_without_run_is_pending_and_blocked_run_is_blocked() -> None:
    request = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        plan_snapshot_id=uuid4(),
        plan_generation=1,
        version=1,
        collaboration_gate_mode=CollaborationGateMode.RELEASE_REQUIRED,
    )

    assert (
        CollaborationGateFact.for_request(
            request,
            run=None,
            release=None,
            plan_head=None,
        ).reason
        == "collaboration_run_pending"
    )
    blocked_run = SimpleNamespace(
        id=uuid4(),
        status=CollaborationRunStatus.BLOCKED,
    )
    assert (
        CollaborationGateFact.for_request(
            request,
            run=blocked_run,
            release=None,
            plan_head=None,
        ).reason
        == "collaboration_run_blocked"
    )
