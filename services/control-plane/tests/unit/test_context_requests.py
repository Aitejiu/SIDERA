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
