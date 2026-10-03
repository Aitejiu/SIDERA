from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane.application.context_requests import (
    execution_context_manifest_schema_version,
    resolve_execution_context_authorization,
)
from sidera_control_plane.domain.collaboration import CollaborationGateMode
from sidera_control_plane.domain.execution import (
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.execution_requests import ExecutionRequestStatus


def fixed_planning_inputs():
    from dataclasses import replace
    from datetime import UTC, datetime
    from uuid import UUID

    from sidera_control_plane.application.work_context_commands import (
        WorkContextRevisionFactory,
    )
    from sidera_control_plane.domain.planning import PlanningRun
    from sidera_control_plane.domain.work_context import WorkContextRevisionReason
    from tests.unit.test_opencode_planner import _planner_input

    now = datetime(2026, 10, 3, tzinfo=UTC)
    planner_input, original = _planner_input()
    snapshot = replace(
        original,
        workspace_id=UUID(int=1),
        profile_id=UUID(int=2),
        planner_module_instance_id=UUID(int=3),
    )
    brief = replace(
        planner_input.brief,
        planning_request_id=UUID(int=4),
        source_correlation=UUID(int=5),
        created_at=now,
        objective="Fixed goal",
        constraints=("Fixed constraint",),
        acceptance_criteria=("Done",),
        requested_outputs=("proposal",),
    )
    run = PlanningRun.create(
        run_id=UUID(int=8),
        planning_request_id=brief.planning_request_id,
        workspace_id=snapshot.workspace_id,
        brief_revision=brief.revision,
        attempt=1,
        profile_snapshot=snapshot,
        queued_at=now,
    )
    revision = WorkContextRevisionFactory().create(
        work_context_id=UUID(int=6),
        workspace_id=snapshot.workspace_id,
        brief=brief,
        revision_number=1,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        actor="test",
        created_at=now,
        revision_id=UUID(int=7),
    )
    return run, brief, revision, now


def test_native_planning_context_request_pins_profile_not_legacy_module():
    from dataclasses import replace
    from uuid import UUID

    from sidera_agents.contracts import ProfileRef

    from sidera_control_plane.application.context_requests import (
        build_planning_context_assembly_request,
    )
    from tests.unit.test_agent_planner import native_snapshot

    run, brief, revision, now = fixed_planning_inputs()
    snapshot = replace(
        native_snapshot(run.profile_snapshot),
        planner_agent_ref=ProfileRef(
            workspace_id=run.workspace_id, profile_id=UUID(int=9), revision=2
        ),
    )
    run = replace(
        run,
        profile_snapshot=snapshot,
        planner_module_instance_id=None,
        planner_module_config_version=None,
        capability_snapshot=None,
    )
    request = build_planning_context_assembly_request(
        run=run,
        brief=brief,
        work_context_revision=revision,
        ttl_seconds=1800,
        created_at=now,
    )
    assert request.budget["planner_agent_ref"] == snapshot.planner_agent_ref.model_dump(
        mode="json"
    )
    assert request.budget["profile_id"] == str(snapshot.profile_id)
    assert request.budget["execution_mode"] == "native_profile"
    assert "planner_module_instance_id" not in request.budget
    assert request.fact_scope_refs == (
        "planning_request",
        "planning_run",
        "brief",
        "planning_profile",
        "planner_agent",
    )
    later = replace(
        snapshot,
        planner_agent_ref=replace_profile_revision(snapshot.planner_agent_ref, 3),
    )
    changed = build_planning_context_assembly_request(
        run=replace(run, profile_snapshot=later),
        brief=brief,
        work_context_revision=revision,
        ttl_seconds=1800,
        created_at=now,
    )
    assert changed.request_fingerprint != request.request_fingerprint


def replace_profile_revision(ref, revision):
    return ref.model_copy(update={"revision": revision})


def test_fixed_legacy_planning_context_request_retains_pre_edit_fingerprint():
    from sidera_control_plane.application.context_requests import (
        build_planning_context_assembly_request,
    )

    run, brief, revision, now = fixed_planning_inputs()
    request = build_planning_context_assembly_request(
        run=run,
        brief=brief,
        work_context_revision=revision,
        ttl_seconds=1800,
        created_at=now,
    )
    assert (
        request.request_fingerprint
        == "e972a0d750d3ac7ccb5408fddb1b4c601623a725313e23fba25754970477e4ca"
    )
    assert "execution_mode" not in request.budget
    assert "planner_module" in request.fact_scope_refs


@pytest.mark.parametrize("derivation_kind", ("verification", "rework"))
def test_result_derived_execution_context_uses_v3_without_selection(
    derivation_kind: str,
) -> None:
    workspace_id = uuid4()
    worker_item_id = uuid4()
    request = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        worker_item_id=worker_item_id,
        status=ExecutionRequestStatus.MATERIALIZED,
        input_selection_id=None,
        result_derivation_kind=derivation_kind,
        collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
        source_execution_id=None,
        source_session_id=None,
        execution_lineage_id=None,
    )
    execution = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        worker_item_id=worker_item_id,
        lineage_id=uuid4(),
        source_execution_id=None,
        source_session_id=None,
    )
    uow = SimpleNamespace(
        execution_requests=SimpleNamespace(
            get_effective_by_execution_lineage=lambda *_args, **_kwargs: request
        ),
    )

    assert execution_context_manifest_schema_version(uow, execution) == 3


def _linked_execution_context(*, gate_mode: CollaborationGateMode):
    workspace_id = uuid4()
    worker_item_id = uuid4()
    child_execution_id = uuid4()
    source_execution_id = uuid4()
    source_session_id = uuid4()
    child_request = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        worker_item_id=worker_item_id,
        status=ExecutionRequestStatus.MATERIALIZED,
        input_selection_id=None,
        result_derivation_kind=None,
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        collaboration_gate_mode=gate_mode,
        execution_lineage_id=child_execution_id,
    )
    source_request = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        worker_item_id=worker_item_id,
        status=ExecutionRequestStatus.MATERIALIZED,
        input_selection_id=None,
        result_derivation_kind="rework",
        source_execution_id=None,
        source_session_id=None,
        collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
        execution_lineage_id=source_execution_id,
    )
    source_execution = SimpleNamespace(
        id=source_execution_id,
        workspace_id=workspace_id,
        worker_item_id=worker_item_id,
        lineage_id=source_execution_id,
        source_execution_id=None,
        source_session_id=None,
        status=ExecutionStatus.SUCCEEDED,
    )
    source_session = SimpleNamespace(
        id=source_session_id,
        workspace_id=workspace_id,
        execution_id=source_execution_id,
        status=ExecutionSessionStatus.CLOSED,
    )
    execution = SimpleNamespace(
        id=child_execution_id,
        workspace_id=workspace_id,
        worker_item_id=worker_item_id,
        lineage_id=child_execution_id,
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
    )
    requests_by_lineage = {
        child_execution_id: child_request,
        source_execution_id: source_request,
    }
    uow = SimpleNamespace(
        execution_requests=SimpleNamespace(
            get_effective_by_execution_lineage=lambda lineage_id, _workspace_id: (
                requests_by_lineage.get(lineage_id)
            )
        ),
        executions=SimpleNamespace(
            get=lambda execution_id, _workspace_id: (
                source_execution if execution_id == source_execution_id else None
            )
        ),
        sessions=SimpleNamespace(
            get_any=lambda session_id, _workspace_id: (
                source_session if session_id == source_session_id else None
            )
        ),
    )
    return execution, child_request, source_request, uow


def test_release_required_revision_uses_its_own_context_authority() -> None:
    execution, child_request, _source_request, uow = _linked_execution_context(
        gate_mode=CollaborationGateMode.RELEASE_REQUIRED
    )

    authorization = resolve_execution_context_authorization(uow, execution)

    assert authorization is not None
    assert authorization.request is child_request
    assert authorization.inherited is False
    assert execution_context_manifest_schema_version(uow, execution) == 1


def test_unmanaged_source_link_still_inherits_context_authority() -> None:
    execution, _child_request, source_request, uow = _linked_execution_context(
        gate_mode=CollaborationGateMode.UNMANAGED
    )

    authorization = resolve_execution_context_authorization(uow, execution)

    assert authorization is not None
    assert authorization.request is source_request
    assert authorization.source_execution_id == source_request.execution_lineage_id
    assert authorization.inherited is True
    assert execution_context_manifest_schema_version(uow, execution) == 3
