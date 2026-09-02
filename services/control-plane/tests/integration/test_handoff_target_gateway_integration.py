from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.handoff_context import HandoffContextService
from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputClassification,
)
from sidera_control_plane.application.worker_gateway import (
    GatewayRecoveryRequired,
    WorkerGateway,
)
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationStatus,
    GatewayOperationType,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffStopEvidence,
)
from sidera_control_plane.domain.workspace import workspace_manifest_digest
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    GatewayOperationModel,
    HandoffTransitionModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_to_payload,
)
from tests.integration.test_context_package_worker import EmptyCatalog
from tests.integration.test_handoff_context_package import (
    _add_target_request,
    _reset_transition_for_bundle,
    _seed_proposal,
)
from tests.integration.test_handoff_target_execution import _scheduler
from tests.integration.test_handoff_target_execution import _seed as seed_handoff
from tests.integration.test_planning_runner_integration import seed_request

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class _UnusedWorkspaceProvider:
    def prepare(self, spec, *, operation_id):
        raise AssertionError("handoff target start must reuse the transferred workspace")


class _KnownFailedEnableAdapter(FakeWorkerAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.enable_calls = 0

    def enable_handoff_writer(
        self,
        handle,
        *,
        fencing_token: int,
        operation_id: UUID,
    ) -> DeliveryResult:
        self.enable_calls += 1
        if self.enable_calls == 1:
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.FAILED,
                accepted_sequence=None,
                failure_code="known_no_side_effect",
                execution_session_id=handle.execution_session_id,
            )
        return super().enable_handoff_writer(
            handle,
            fencing_token=fencing_token,
            operation_id=operation_id,
        )


def _prepare_gateway_target(
    database: Database,
    workspace_id: UUID,
    uow_factory,
    *,
    failures: dict[str, str] | None = None,
    adapter: FakeWorkerAdapter | None = None,
):
    fixture, runtime = seed_handoff(database, workspace_id)
    proposal = _seed_proposal(database, fixture)
    planning_request_id = seed_request(uow_factory, workspace_id)
    service = HandoffContextService(uow_factory)

    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        source_package = uow.context_packages.get(
            proposal.payload.source_context_package_id,
            workspace_id,
        )
        source_manifest = (
            uow.workspaces.get(transition.source_workspace_manifest_id, workspace_id)
            if transition is not None
            else None
        )
        assert transition is not None
        assert source_package is not None
        assert source_manifest is not None
        checkpoint = HandoffCheckpointEvidence(
            operation_id=uuid4(),
            transition_id=transition.id,
            source_session_id=transition.source_session_id,
            accepted_sequence=3,
            workspace_digest=workspace_manifest_digest(source_manifest),
            context_package_id=source_package.package_id,
            context_package_hash=source_package.package_hash,
            context_version=source_package.context_version or 0,
            progress_summary_ref="progress:handoff-target-gateway",
            artifact_refs=(),
            evidence_refs=(),
            quiescent=True,
            produced_at=NOW,
        )
        stop = HandoffStopEvidence(
            operation_id=uuid4(),
            source_session_id=transition.source_session_id,
            accepted_sequence=3,
            stopped=True,
            produced_at=NOW,
        )

    _reset_transition_for_bundle(database, fixture, checkpoint, stop, source_manifest)

    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        target_execution = uow.executions.get(fixture.target_execution_id, workspace_id)
        worker_item = uow.worker_items.get(transition.worker_item_id, workspace_id)
        assert transition is not None
        assert target_execution is not None
        assert worker_item is not None
        target_request = _add_target_request(
            uow,
            planning_request_id=planning_request_id,
            transition=transition,
            proposal=proposal,
            target_execution=target_execution,
            worker_item=worker_item,
        )
        source_facts = service.collect_source_facts(
            transition=transition,
            workspace_id=workspace_id,
            input_classification=HandoffInputClassification(
                source_session_id=transition.source_session_id,
                cutoff_sequence=transition.source_input_cutoff_sequence,
                dispositions=(),
                transferable_input_ids=(),
                excluded_input_ids=(),
                unknown_input_ids=(),
            ),
        )
        bundle, _assembly = service.create_bundle_and_request(
            transition=transition,
            proposal=proposal,
            target_request=target_request,
            target_execution=target_execution,
            source_facts=source_facts,
            now=NOW,
            uow=uow,
        )

    worker = ContextPackageWorker(
        uow_factory=uow_factory,
        catalog=EmptyCatalog(),
        owner_id="handoff-target-gateway",
        clock=lambda: NOW,
    )
    package_result = worker.run_once(limit=1)
    assert package_result.ready == 1
    package = service.wait_until_package_ready(
        transition_id=fixture.transition_id,
        workspace_id=workspace_id,
        now=NOW,
    )
    assert package is not None
    assert package.control_plane_fact_snapshot["handoff"]["bundle_id"] == str(bundle.id)

    with database.session() as session:
        transition_row = session.get(HandoffTransitionModel, fixture.transition_id)
        assert transition_row is not None
        session.execute(
            update(ExecutionWorkspaceModel)
            .where(ExecutionWorkspaceModel.id == fixture.target_manifest_id)
            .values(lifecycle="ready", version=2)
        )
        session.execute(
            update(HandoffTransitionModel)
            .where(HandoffTransitionModel.id == fixture.transition_id)
            .values(
                phase="start_target_session",
                writer_state="target_starting",
                version=transition_row.version + 1,
            )
        )
        session.execute(
            update(ModuleDefinitionModel)
            .where(ModuleDefinitionModel.id == runtime.adapter_definition_id)
            .values(capabilities=capability_profile_to_payload(runtime.capability_snapshot))
        )
        session.execute(
            update(ModuleDefinitionModel)
            .where(ModuleDefinitionModel.id == runtime.workspace_definition_id)
            .values(
                capabilities={
                    "isolation": "filesystem",
                    "writable": True,
                    "git_worktree": True,
                    "stability": "stable",
                }
            )
        )
        session.execute(
            update(ModuleInstanceModel)
            .where(ModuleInstanceModel.id == runtime.workspace_module_instance_id)
            .values(
                config={
                    "workspace.repository_ref": "repo://sidera",
                    "workspace.base_revision": "a" * 40,
                }
            )
        )
        session.commit()

    scheduler = _scheduler(database, runtime)
    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(fixture.transition_id, workspace_id)
        assert transition is not None
        transition_version = transition.version
    scheduler.assign_handoff_target(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=transition_version,
        now=NOW,
    )

    worker_adapter = adapter or FakeWorkerAdapter(failures=failures)
    gateway = WorkerGateway(
        uow_factory,
        adapter_factories={"fake": lambda _instance: worker_adapter},
        workspace_factories={
            "git-worktree": lambda _instance: _UnusedWorkspaceProvider(),
        },
        context_materializer=ContextMaterializer(EmptyCatalog()),
        clock=lambda: NOW,
    )
    return fixture, gateway, worker_adapter


def _transition_version(uow_factory, workspace_id: UUID, transition_id: UUID) -> int:
    with uow_factory() as uow:
        transition = uow.handoff_transitions.get(transition_id, workspace_id)
        assert transition is not None
        return transition.version


def _move_to_context_acknowledgement(
    database: Database,
    transition_id: UUID,
) -> int:
    with database.session() as session:
        transition = session.get(HandoffTransitionModel, transition_id)
        assert transition is not None
        next_version = transition.version + 1
        session.execute(
            update(HandoffTransitionModel)
            .where(HandoffTransitionModel.id == transition_id)
            .values(
                phase="acknowledge_context",
                writer_state="target_starting",
                version=next_version,
            )
        )
        session.commit()
    return next_version


def test_target_start_is_bound_read_only_then_enables_one_writer(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    fixture, gateway, adapter = _prepare_gateway_target(
        database,
        workspace_id,
        uow_factory,
    )
    start_version = _transition_version(uow_factory, workspace_id, fixture.transition_id)

    started = gateway.start_handoff_target(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=start_version,
    )
    repeated_start = gateway.start_handoff_target(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=start_version + 1,
    )
    assert repeated_start == started
    assert len(adapter.active_sessions()) == 1

    acknowledgement_version = _move_to_context_acknowledgement(
        database,
        fixture.transition_id,
    )
    first_enable = gateway.enable_handoff_target_writer(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=acknowledgement_version,
    )
    repeated_enable = gateway.enable_handoff_target_writer(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=acknowledgement_version,
    )

    assert first_enable.outcome is DeliveryOutcome.DELIVERED
    assert repeated_enable == first_enable
    assert adapter.handoff_resume_count == 1
    assert adapter.handoff_resume_instructions == ("continue from the source checkpoint",)
    with database.session() as session:
        execution = session.get(ExecutionModel, fixture.target_execution_id)
        target_session = session.get(ExecutionSessionModel, started.session_id)
        target_manifest = session.get(ExecutionWorkspaceModel, fixture.target_manifest_id)
        assert execution is not None
        assert target_session is not None
        assert target_manifest is not None
        assert execution.status == "running"
        assert target_session.status == "active"
        assert target_manifest.lifecycle == "active"
        operations = session.scalars(
            select(GatewayOperationModel).where(
                GatewayOperationModel.execution_id == fixture.target_execution_id
            )
        ).all()
        assert {row.operation_type for row in operations} == {
            GatewayOperationType.START_SESSION.value,
            GatewayOperationType.HANDOFF_ENABLE_WRITER.value,
        }
        assert all(row.status == GatewayOperationStatus.SUCCEEDED.value for row in operations)


def test_target_writer_timeout_is_unknown_and_never_replayed(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    fixture, gateway, adapter = _prepare_gateway_target(
        database,
        workspace_id,
        uow_factory,
        failures={"handoff_enable_writer_after": "timeout"},
    )
    start_version = _transition_version(uow_factory, workspace_id, fixture.transition_id)
    started = gateway.start_handoff_target(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=start_version,
    )
    acknowledgement_version = _move_to_context_acknowledgement(
        database,
        fixture.transition_id,
    )

    with pytest.raises(GatewayRecoveryRequired):
        gateway.enable_handoff_target_writer(
            workspace_id=workspace_id,
            transition_id=fixture.transition_id,
            expected_transition_version=acknowledgement_version,
        )
    with pytest.raises(GatewayRecoveryRequired):
        gateway.enable_handoff_target_writer(
            workspace_id=workspace_id,
            transition_id=fixture.transition_id,
            expected_transition_version=acknowledgement_version,
        )

    operation_id = handoff_step_operation_id(
        fixture.transition_id,
        "acknowledge_context",
        logical_attempt=1,
        action="enable_writer",
    )
    with database.session() as session:
        operation = session.scalar(
            select(GatewayOperationModel)
            .where(GatewayOperationModel.operation_id == operation_id)
        )
        target_manifest = session.get(ExecutionWorkspaceModel, fixture.target_manifest_id)
        assert operation is not None
        assert target_manifest is not None
        assert operation.status == GatewayOperationStatus.UNKNOWN.value
        assert target_manifest.lifecycle == "active"
    assert started.session_id
    assert adapter.handoff_resume_count == 1


def test_known_failed_writer_enable_can_retry_without_a_second_target_start(
    database: Database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    adapter = _KnownFailedEnableAdapter()
    fixture, gateway, _ = _prepare_gateway_target(
        database,
        workspace_id,
        uow_factory,
        adapter=adapter,
    )
    start_version = _transition_version(uow_factory, workspace_id, fixture.transition_id)
    started = gateway.start_handoff_target(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=start_version,
    )
    acknowledgement_version = _move_to_context_acknowledgement(
        database,
        fixture.transition_id,
    )

    first = gateway.enable_handoff_target_writer(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=acknowledgement_version,
    )
    second = gateway.enable_handoff_target_writer(
        workspace_id=workspace_id,
        transition_id=fixture.transition_id,
        expected_transition_version=acknowledgement_version,
    )

    assert first.outcome is DeliveryOutcome.FAILED
    assert second.outcome is DeliveryOutcome.DELIVERED
    assert adapter.enable_calls == 2
    assert adapter.handoff_resume_count == 1
    assert len(adapter.active_sessions()) == 1
    with database.session() as session:
        target_manifest = session.get(ExecutionWorkspaceModel, fixture.target_manifest_id)
        assert target_manifest is not None
        assert target_manifest.lifecycle == "active"
        operation = session.scalar(
            select(GatewayOperationModel).where(
                GatewayOperationModel.execution_id == fixture.target_execution_id,
                GatewayOperationModel.operation_type
                == GatewayOperationType.HANDOFF_ENABLE_WRITER.value,
            )
        )
        assert operation is not None
        assert operation.status == GatewayOperationStatus.SUCCEEDED.value
    assert started.session_id
