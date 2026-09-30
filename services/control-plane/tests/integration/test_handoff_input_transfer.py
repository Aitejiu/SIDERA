from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from sidera_control_plane.application.code_result_capture import (
    CODE_RESULT_CAPTURE_OPERATION_KIND,
    code_capture_operation_id,
)
from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputTransferError,
    HandoffInputTransferService,
)
from sidera_control_plane.application.result_ports import ResultOperationAttemptV1
from sidera_control_plane.domain.execution import ExecutionSession, ExecutionStatus
from sidera_control_plane.domain.handoff_evidence import HandoffContextBundle
from sidera_control_plane.domain.handoffs import HandoffPhase, HandoffWriterState
from sidera_control_plane.domain.inputs import ExecutionInput, ExecutionInputStatus
from sidera_control_plane.domain.result_operations import ResultOperationStatus
from sidera_control_plane.domain.workspace import workspace_location_ref_hash
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionInputModel,
    ExecutionSessionModel,
    HandoffProposalModel,
)
from tests.integration.test_handoff_target_execution import _seed as seed_handoff

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _prepare_transfer(uow_factory, fixture, workspace_id: UUID) -> UUID:
    target_session_id = uuid4()
    source_input_id = uuid4()
    with uow_factory() as uow:
        uow.session.execute(
            update(ExecutionSessionModel)
            .where(ExecutionSessionModel.execution_id == fixture.source_execution_id)
            .values(capability_snapshot=None)
        )
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        assert transition is not None
        source_package_id = uow.session.scalar(
            select(HandoffProposalModel.source_context_package_id).where(
                HandoffProposalModel.id == transition.proposal_id,
                HandoffProposalModel.workspace_id == workspace_id,
            )
        )
        assert source_package_id is not None
        source_package = uow.context_packages.get(
            source_package_id,
            workspace_id,
        )
        source_manifest = uow.workspaces.get(
            transition.source_workspace_manifest_id,
            workspace_id,
        )
        target_execution = uow.executions.get(
            fixture.target_execution_id,
            workspace_id,
        )
        assert source_package is not None
        assert source_manifest is not None
        assert target_execution is not None

        previous_execution_version = target_execution.version
        target_execution.status = ExecutionStatus.RUNNING
        target_execution.assigned_worker_id = transition.target_worker_id
        target_execution.started_at = NOW
        target_execution.version += 1
        uow.executions.save(target_execution, previous_execution_version)

        session = ExecutionSession.start(
            session_id=target_session_id,
            workspace_id=workspace_id,
            execution_id=target_execution.id,
            worker_id=transition.target_worker_id,
            adapter_session_id="handoff-target-session",
            now=NOW,
        )
        uow.sessions.add(session)

        previous_transition_version = transition.version
        transition.phase = HandoffPhase.TRANSFER_SAFE_INPUTS
        transition.writer_state = HandoffWriterState.TARGET_ACTIVE
        transition.target_session_id = target_session_id
        transition.version += 1
        uow.handoff_transitions.save(transition, previous_transition_version)

        source_input = ExecutionInput.accept(
            id=source_input_id,
            workspace_id=workspace_id,
            execution_id=transition.source_execution_id,
            session_id=transition.source_session_id,
            client_idempotency_key="integration-source-input",
            text="continue from the exact handoff checkpoint",
            attachment_refs=("artifact://handoff",),
            now=NOW,
        )
        source_input.queue()
        uow.execution_inputs.add(source_input)

        bundle = HandoffContextBundle.create(
            id=uuid4(),
            schema_version=1,
            workspace_id=workspace_id,
            worker_item_id=transition.worker_item_id,
            handoff_transition_id=transition.id,
            logical_attempt=transition.logical_attempt,
            recovery_decision_id=None,
            source_execution_id=transition.source_execution_id,
            source_session_id=transition.source_session_id,
            source_checkpoint_sequence=3,
            source_last_accepted_sequence=3,
            source_context_package_id=source_package.package_id,
            source_context_package_hash=source_package.package_hash,
            source_worker_context_version=source_package.context_version or 1,
            target_execution_id=target_execution.id,
            target_worker_id=transition.target_worker_id,
            target_runtime_snapshot_hash="c" * 64,
            target_context_version=transition.target_context_version,
            source_workspace_manifest_id=source_manifest.id,
            source_workspace_version=source_manifest.version,
            workspace_location_ref_hash=workspace_location_ref_hash(
                source_manifest.location_ref
            ),
            repository_state=(),
            artifact_refs=(),
            evidence_refs=(),
            handoff_reason="integration handoff",
            handoff_instruction="continue from the exact checkpoint",
            delivered_input_refs=(),
            transferable_input_refs=(source_input.id,),
            excluded_input_refs=(),
            created_at=NOW,
        )
        uow.handoff_context_bundles.add_append_only(bundle)
        previous_transition_version = transition.version
        transition.handoff_context_bundle_id = bundle.id
        transition.version += 1
        uow.handoff_transitions.save(transition, previous_transition_version)
    return target_session_id


def test_safe_input_transfer_persists_lineage_and_is_restart_idempotent(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    fixture, _runtime = seed_handoff(database, workspace_id)
    target_session_id = _prepare_transfer(uow_factory, fixture, workspace_id)

    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        assert transition is not None
    service = HandoffInputTransferService(uow_factory)
    classification = service.classify_source_inputs(transition=transition, now=NOW)

    target_execution = None
    with uow_factory() as uow:
        target_execution = uow.executions.get(fixture.target_execution_id, workspace_id)
        assert target_execution is not None

    first = service.transfer_safe_inputs(
        transition=transition,
        target_execution=target_execution,
        target_session_id=target_session_id,
        classification=classification,
        now=NOW,
    )
    second = service.transfer_safe_inputs(
        transition=transition,
        target_execution=target_execution,
        target_session_id=target_session_id,
        classification=classification,
        now=NOW,
    )

    assert len(first) == 1
    assert second == first
    target = first[0]
    assert target.status is ExecutionInputStatus.QUEUED
    assert target.execution_id == fixture.target_execution_id
    assert target.session_id == target_session_id
    assert target.source_input_id == classification.transferable_input_ids[0]
    assert target.handoff_transition_id == fixture.transition_id
    with database.session() as session:
        source = session.get(ExecutionInputModel, classification.transferable_input_ids[0])
        derived_count = session.scalar(
            select(func.count())
            .select_from(ExecutionInputModel)
            .where(ExecutionInputModel.handoff_transition_id == fixture.transition_id)
        )
        persisted_session = session.get(ExecutionSessionModel, target_session_id)
    assert source is not None
    assert source.status == ExecutionInputStatus.SUPERSEDED.value
    assert source.superseded_reason == "handoff_transferred"
    assert derived_count == 1
    assert persisted_session is not None


def test_safe_input_transfer_rejects_a_finalizing_target_session(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    fixture, _runtime = seed_handoff(database, workspace_id)
    target_session_id = _prepare_transfer(uow_factory, fixture, workspace_id)
    service = HandoffInputTransferService(uow_factory)
    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        target_execution = uow.executions.get(
            fixture.target_execution_id, workspace_id
        )
        assert transition is not None
        assert target_execution is not None
        item = uow.worker_items.get(transition.worker_item_id, workspace_id)
        assert item is not None
        operation = ResultOperationAttemptV1(
            operation_id=code_capture_operation_id(target_execution.id),
            workspace_id=workspace_id,
            root_task_id=item.root_task_id,
            operation_kind=CODE_RESULT_CAPTURE_OPERATION_KIND,
            attempt=1,
            status=ResultOperationStatus.RUNNING,
            payload={"terminal_observed": True, "terminal_outcome": "succeeded"},
            lease_owner=None,
            lease_token=None,
            lease_expiry=None,
            receipt_ref=None,
            failure_code=None,
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        uow.results.save_operation(operation, expected_version=0)
    classification = service.classify_source_inputs(
        transition=transition, now=NOW
    )

    with pytest.raises(HandoffInputTransferError) as caught:
        service.transfer_safe_inputs(
            transition=transition,
            target_execution=target_execution,
            target_session_id=target_session_id,
            classification=classification,
            now=NOW,
        )

    assert caught.value.code == "handoff_input_target_finalizing"
    with database.session() as session:
        derived_count = session.scalar(
            select(func.count())
            .select_from(ExecutionInputModel)
            .where(ExecutionInputModel.handoff_transition_id == fixture.transition_id)
        )
    assert derived_count == 0
