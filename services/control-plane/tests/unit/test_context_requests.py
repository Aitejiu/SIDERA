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


def derived_code_case(*, kind: str, hard_only: bool = False, branch: str = "code"):
    from dataclasses import replace
    from uuid import UUID

    from sidera_control_plane.domain.execution import ExecutionSession

    from .test_activation_reconciler_unit import MaterializationUnitOfWork
    from .test_execution_code_inputs import code_input_case, rehash_selection

    case = code_input_case(hard_only=hard_only)
    if branch == "source":
        case = replace(
            case,
            request=replace(
                case.request,
                input_selection_id=None,
                input_selection_hash=None,
                collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
            ),
        )
    elif branch == "text":
        from sidera_control_plane.domain.artifacts import ArtifactType

        group = case.selection.groups[0]
        source = group.sources[0]
        selection = rehash_selection(
            replace(
                case.selection,
                groups=(
                    replace(
                        group,
                        sources=(
                            replace(
                                source,
                                artifacts=(
                                    replace(
                                        source.artifacts[0],
                                        artifact_type=ArtifactType.REPORT,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )
        case = replace(
            case,
            selection=selection,
            request=replace(
                case.request, input_selection_hash=selection.selection_hash
            ),
        )
    elif branch == "legacy_diff":
        case = replace(case, plan=replace(case.plan, integration_spec=None))
    parent = replace(case.execution, status=ExecutionStatus.SUCCEEDED)
    case = replace(case, execution=parent)
    case.request.materialize(
        execution_lineage_id=parent.lineage_id,
        first_execution_id=parent.id,
        now=case.selection.created_at,
    )
    session = replace(
        ExecutionSession.start(
            session_id=UUID(int=920),
            workspace_id=parent.workspace_id,
            execution_id=parent.id,
            worker_id=UUID(int=921),
            adapter_session_id="test-consumer",
            now=case.selection.created_at,
        ),
        status=ExecutionSessionStatus.CLOSED,
    )
    child = replace(
        parent,
        id=UUID(int=922),
        status=ExecutionStatus.QUEUED,
        lineage_id=parent.lineage_id if kind == "retry" else UUID(int=922),
        attempt=2 if kind == "retry" else 1,
        source_execution_id=None if kind == "retry" else parent.id,
        source_session_id=None if kind == "retry" else session.id,
    )
    uow = MaterializationUnitOfWork(case)
    uow.execution_requests.get_effective_by_execution_lineage = (
        lambda lineage_id, workspace_id: (
            case.request
            if (lineage_id, workspace_id) == (parent.lineage_id, parent.workspace_id)
            else None
        )
    )
    uow.sessions = SimpleNamespace(get_any=lambda *_args: session)
    return case, child, session, uow


@pytest.mark.parametrize("kind", ("retry", "continuation"))
@pytest.mark.parametrize("hard_only", (False, True))
def test_normal_derived_execution_keeps_code_selection_in_fresh_v3_consumer(
    kind: str, hard_only: bool
) -> None:
    from typing import cast

    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )
    from sidera_control_plane.application.ports import ControlPlaneUnitOfWork

    case, child, session, uow = derived_code_case(kind=kind, hard_only=hard_only)
    arguments = {
        "uow": cast(ControlPlaneUnitOfWork, uow),
        "execution": child,
        "inheritance_kind": kind,
        "source_execution_id": case.execution.id,
        "source_session_id": session.id,
        "ttl_seconds": 1800,
        "created_at": case.selection.created_at,
        "additional_fact_scope_refs": ("remaining_attempts",),
        "additional_budget": {"remaining_attempts": 1},
    }
    assembly = build_derived_execution_context_assembly_request(**arguments)
    assert assembly is not None
    assert assembly.manifest_schema_version == 3
    assert assembly.consumer_id == child.id
    assert assembly.input_selection_id == case.selection.id
    assert assembly.input_selection_hash == case.selection.selection_hash
    manifest = assembly.code_input_manifest
    assert manifest is not None
    assert manifest.execution_id == child.id
    assert manifest.execution_id != case.execution.id
    assert manifest.request_id == case.request.id
    assert manifest.selection_id == case.selection.id
    assert manifest.selection_hash == case.selection.selection_hash
    assert assembly.verification_manifest is None
    assert assembly.budget["remaining_attempts"] == 1
    assert assembly.budget["execution_context_inheritance"] == {
        "kind": kind,
        "authorization_request_id": str(case.request.id),
        "source_execution_id": str(case.execution.id),
        "source_session_id": str(session.id),
        "source_lineage_id": str(case.execution.lineage_id),
    }
    assert "execution_context_inheritance" in assembly.fact_scope_refs
    assert "remaining_attempts" in assembly.fact_scope_refs
    assert ":v3:" in assembly.idempotency_key
    assert manifest.input_hash in assembly.idempotency_key
    replay = build_derived_execution_context_assembly_request(
        **arguments, idempotency_key=assembly.idempotency_key
    )
    assert replay is not None
    assert replay.request_fingerprint == assembly.request_fingerprint
    assert replay.code_input_manifest == manifest
    assert case.request.version > case.selection.target_request_version
    assert (
        uow.release.request_version_at_release == case.selection.target_request_version
    )


@pytest.mark.parametrize("kind", ("retry", "continuation"))
def test_normal_derived_execution_rejects_missing_selected_code(kind: str) -> None:
    from typing import cast

    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )
    from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
    from sidera_control_plane.domain.errors import ValidationError

    case, child, session, uow = derived_code_case(kind=kind, hard_only=True)
    uow.results.retained_code_results = lambda *_args: ()
    with pytest.raises(ValidationError):
        build_derived_execution_context_assembly_request(
            uow=cast(ControlPlaneUnitOfWork, uow),
            execution=child,
            inheritance_kind=kind,
            source_execution_id=case.execution.id,
            source_session_id=session.id,
            ttl_seconds=1800,
            created_at=case.selection.created_at,
        )
    assert uow.assemblies == []
    assert uow.added_executions == []


@pytest.mark.parametrize("kind", ("retry", "continuation"))
@pytest.mark.parametrize("branch", ("source", "text", "legacy_diff"))
def test_normal_derived_execution_retains_legacy_schema_choices(
    kind: str, branch: str
) -> None:
    from typing import cast

    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )
    from sidera_control_plane.application.ports import ControlPlaneUnitOfWork

    case, child, session, uow = derived_code_case(kind=kind, branch=branch)
    assembly = build_derived_execution_context_assembly_request(
        uow=cast(ControlPlaneUnitOfWork, uow),
        execution=child,
        inheritance_kind=kind,
        source_execution_id=case.execution.id,
        source_session_id=session.id,
        ttl_seconds=1800,
        created_at=case.selection.created_at,
    )
    assert assembly is not None
    assert assembly.manifest_schema_version == (1 if branch == "source" else 2)
    assert assembly.code_input_manifest is None
    assert assembly.budget["execution_context_inheritance"]["kind"] == kind


@pytest.mark.parametrize("branch", ("code", "source", "text", "legacy_diff"))
def test_derived_context_prefix_uses_actual_input_protocol_and_replays(branch: str):
    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )

    case, child, session, uow = derived_code_case(kind="handoff", branch=branch)
    prefix = f"context:handoff:fixed:attempt:2:execution:{child.id}"
    arguments = {
        "uow": uow,
        "execution": child,
        "inheritance_kind": "handoff",
        "source_execution_id": case.execution.id,
        "source_session_id": session.id,
        "ttl_seconds": 1800,
        "created_at": case.selection.created_at,
        "idempotency_key_prefix": prefix,
    }
    assembly = build_derived_execution_context_assembly_request(**arguments)
    assert assembly is not None
    if branch == "code":
        assert assembly.code_input_manifest is not None
        suffix = f":v3:input:{assembly.code_input_manifest.input_hash}"
    elif branch == "source":
        suffix = ":v1"
    else:
        suffix = f":v2:selection:{case.selection.selection_hash}"
    assert assembly.idempotency_key == prefix + suffix
    assert assembly.consumer_id == child.id
    assert assembly.budget["execution_context_inheritance"][
        "authorization_request_id"
    ] == str(case.request.id)
    repeated = build_derived_execution_context_assembly_request(**arguments)
    assert repeated is not None
    assert repeated.request_fingerprint == assembly.request_fingerprint
    assert repeated.idempotency_key == assembly.idempotency_key


@pytest.mark.parametrize(
    "prefix",
    ("", " prefix", "prefix ", "prefix\x00", "prefix\n", "prefix\x7f", "prefix\x85", 4),
)
def test_derived_context_prefix_rejects_invalid_identity(prefix):
    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )

    case, child, session, uow = derived_code_case(kind="handoff")
    with pytest.raises(ValueError, match="prefix"):
        build_derived_execution_context_assembly_request(
            uow=uow,
            execution=child,
            inheritance_kind="handoff",
            source_execution_id=case.execution.id,
            source_session_id=session.id,
            ttl_seconds=1800,
            created_at=case.selection.created_at,
            idempotency_key_prefix=prefix,
        )


def test_derived_context_rejects_prefix_with_full_key():
    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )

    case, child, session, uow = derived_code_case(kind="handoff")
    with pytest.raises(ValueError, match="prefix"):
        build_derived_execution_context_assembly_request(
            uow=uow,
            execution=child,
            inheritance_kind="handoff",
            source_execution_id=case.execution.id,
            source_session_id=session.id,
            ttl_seconds=1800,
            created_at=case.selection.created_at,
            idempotency_key_prefix="context:handoff:fixed",
            idempotency_key="explicit-full-key",
        )


def test_derived_code_context_still_rejects_explicit_v2_handoff_key():
    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )

    case, child, session, uow = derived_code_case(kind="handoff", hard_only=True)
    key = (
        f"context:handoff:fixed:attempt:1:execution:{child.id}:"
        f"v2:selection:{case.selection.selection_hash}"
    )
    with pytest.raises(ValueError, match="v3.*input hash"):
        build_derived_execution_context_assembly_request(
            uow=uow,
            execution=child,
            inheritance_kind="handoff",
            source_execution_id=case.execution.id,
            source_session_id=session.id,
            ttl_seconds=1800,
            created_at=case.selection.created_at,
            idempotency_key=key,
        )


def test_derived_context_prefix_does_not_reuse_old_key_for_changed_code_input():
    from dataclasses import replace

    from sidera_control_plane.application.context_requests import (
        build_derived_execution_context_assembly_request,
    )

    from .test_execution_code_inputs import changed_snapshot

    case, child, session, uow = derived_code_case(kind="handoff", hard_only=True)
    arguments = {
        "uow": uow,
        "execution": child,
        "inheritance_kind": "handoff",
        "source_execution_id": case.execution.id,
        "source_session_id": session.id,
        "ttl_seconds": 1800,
        "created_at": case.selection.created_at,
    }
    prefix = f"context:handoff:fixed:attempt:1:execution:{child.id}"
    original = build_derived_execution_context_assembly_request(
        **arguments, idempotency_key_prefix=prefix
    )
    changed_case = replace(
        case,
        snapshots=(changed_snapshot(case.snapshots[0], result_commit="e" * 40),),
    )
    uow.results = changed_case.uow().results
    changed = build_derived_execution_context_assembly_request(
        **arguments, idempotency_key_prefix=prefix
    )
    assert original is not None and changed is not None
    assert (
        original.code_input_manifest.input_hash
        != changed.code_input_manifest.input_hash
    )
    assert original.idempotency_key != changed.idempotency_key
    assert original.request_fingerprint != changed.request_fingerprint
    with pytest.raises(ValueError, match="v3.*input hash"):
        build_derived_execution_context_assembly_request(
            **arguments, idempotency_key=original.idempotency_key
        )
