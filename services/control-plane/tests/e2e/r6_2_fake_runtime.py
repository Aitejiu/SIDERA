"""Deterministic runtime for the R6.2 fake handoff vertical gate."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete

from sidera_control_plane.adapters.fake_worker import FakeWorkerAdapter
from sidera_control_plane.application.activation_reconciler import ActivationReconciler
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.context_materialization import ContextMaterializer
from sidera_control_plane.application.execution_commands import (
    ExecutionCommandHandler,
    ExecutionInputCommandHandler,
)
from sidera_control_plane.application.handoff_candidates import HandoffCandidateService
from sidera_control_plane.application.handoff_commands import HandoffCommandHandler
from sidera_control_plane.application.handoff_context import HandoffContextService
from sidera_control_plane.application.handoff_coordinator import HandoffCoordinator
from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputTransferService,
)
from sidera_control_plane.application.handoff_recovery import (
    HandoffRecoveryService,
)
from sidera_control_plane.application.handoff_target import HandoffTargetService
from sidera_control_plane.application.ports import KnowledgeCatalogPort
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_gateway import (
    ConfiguredExecutionSpecFactory,
    WorkerGateway,
)
from sidera_control_plane.application.worker_policy import LowestCostWorkerPolicy
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.application.workspace_handoff import WorkspaceHandoffService
from sidera_control_plane.context_package_worker import ContextPackageWorker
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequestDependency,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffOperationInspection,
    HandoffWorkspaceInspection,
)
from sidera_control_plane.domain.handoff_recovery import (
    HandoffRecoveryStrategy,
    HandoffSafeConclusion,
)
from sidera_control_plane.domain.handoffs import HandoffTransition
from sidera_control_plane.domain.workspace import (
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    WorkspaceCheckout,
    WorkspaceLifecycle,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import ExecutionRequestDependencyModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.e2e.seed_r6_2_handoff import G4Fixture, G4Scenario, load_g4_fixture


class FaultPoint(StrEnum):
    CHECKPOINT_BEFORE = "handoff_checkpoint_before"
    CHECKPOINT_AFTER = "handoff_checkpoint_after"
    STOP_BEFORE = "handoff_stop_before"
    STOP_AFTER = "handoff_stop_after"
    FREEZE_BEFORE = "freeze_before"
    FREEZE_AFTER = "freeze_after"
    TRANSFER_BEFORE = "transfer_before"
    TRANSFER_AFTER = "transfer_after"
    TARGET_START_BEFORE = "target_start_before"
    TARGET_START_AFTER = "target_start_after"
    WRITER_BEFORE = "handoff_enable_writer_before"
    WRITER_AFTER = "handoff_enable_writer_after"


class UnknownBoundaryError(RuntimeError):
    """A fake provider boundary cannot prove whether its side effect happened."""

    unknown = True

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _EmptyCatalog(KnowledgeCatalogPort):
    def search(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        del args, kwargs
        return ()

    def get_revision_manifest(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("G4 does not use external knowledge revisions")

    def materialize(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        del args, kwargs
        return ()


class RecordingFakeAdapter(FakeWorkerAdapter):
    """Fake worker boundary with deterministic target-start and call evidence."""

    def __init__(
        self,
        *,
        source_execution_id: UUID,
        failures: Mapping[str, str] | None = None,
        unknown_target_start_once: bool = False,
        unknown_target_start_after_once: bool = False,
        handoff_context: tuple[UUID, str, int],
        handoff_workspace_digest: str,
    ) -> None:
        super().__init__(
            failures=failures,
            handoff_context=handoff_context,
            handoff_workspace_digest=handoff_workspace_digest,
        )
        self.source_execution_id = source_execution_id
        self.unknown_target_start_once = unknown_target_start_once
        self.unknown_target_start_after_once = unknown_target_start_after_once
        self.calls: list[tuple[str, UUID]] = []
        self._start_operation_execution: dict[UUID, UUID] = {}
        self._handles_by_operation: dict[UUID, Any] = {}

    def start_session(
        self,
        spec: ExecutionSpec,
        workspace: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> Any:
        self.calls.append(("start_session", spec.execution_id))
        self._start_operation_execution[operation_id] = spec.execution_id
        if (
            spec.execution_id != self.source_execution_id
            and self.unknown_target_start_once
        ):
            self.unknown_target_start_once = False
            raise UnknownBoundaryError("target_start_unknown")
        handle = super().start_session(spec, workspace, operation_id=operation_id)
        self._handles_by_operation[operation_id] = handle
        if (
            spec.execution_id != self.source_execution_id
            and self.unknown_target_start_after_once
        ):
            self.unknown_target_start_after_once = False
            raise UnknownBoundaryError("target_start_after_unknown")
        if spec.execution_id == self.source_execution_id:
            with self._lock:
                fake_session = self._sessions[handle.adapter_session_id]
                fake_session.last_accepted_sequence = 1
            tuple(self.read_events(handle))
        return handle

    def capture_handoff_checkpoint(self, handle: Any, **kwargs: Any) -> Any:
        operation_id = kwargs["operation_id"]
        self._handles_by_operation[operation_id] = handle
        self.calls.append(("capture_handoff_checkpoint", kwargs["transition_id"]))
        return super().capture_handoff_checkpoint(handle, **kwargs)

    def stop_session(self, handle: Any, *, operation_id: UUID) -> Any:
        self._handles_by_operation[operation_id] = handle
        self.calls.append(("stop_session", operation_id))
        return super().stop_session(handle, operation_id=operation_id)

    def enable_handoff_writer(self, handle: Any, **kwargs: Any) -> Any:
        operation_id = kwargs["operation_id"]
        self._handles_by_operation[operation_id] = handle
        self.calls.append(("enable_handoff_writer", operation_id))
        return super().enable_handoff_writer(handle, **kwargs)

    def inspect_operation(
        self,
        *,
        operation_id: UUID,
        operation_type: GatewayOperationType,
        now: datetime,
    ) -> HandoffOperationInspection:
        execution_id = self._start_operation_execution.get(operation_id)
        handle = self._handles_by_operation.get(operation_id)
        if execution_id is not None and operation_type is GatewayOperationType.START_SESSION:
            if handle is None:
                return HandoffOperationInspection(
                    operation_id=operation_id,
                    operation_type=operation_type,
                    outcome=HandoffInspectionOutcome.ABSENT,
                    execution_id=execution_id,
                    session_id=None,
                    accepted_sequence=None,
                    context_binding=None,
                    observed_at=now,
                    evidence_refs=(f"fake-inspection://{operation_id}/absent",),
                )
            return HandoffOperationInspection(
                operation_id=operation_id,
                operation_type=operation_type,
                outcome=HandoffInspectionOutcome.SUCCEEDED,
                execution_id=execution_id,
                session_id=handle.execution_session_id,
                accepted_sequence=handle.last_accepted_sequence,
                context_binding=handle.context_binding,
                observed_at=now,
                evidence_refs=(f"fake-inspection://{operation_id}/succeeded",),
            )
        handle = self._handles_by_operation.get(operation_id)
        if handle is not None:
            try:
                inspection = super().inspect_handoff_operation(
                    handle,
                    operation_id=operation_id,
                )
            except Exception as exc:  # noqa: BLE001 - inspection is a boundary probe
                del exc
                inspection = None
            if inspection is not None and inspection.operation_type is operation_type:
                return inspection
        return HandoffOperationInspection(
            operation_id=operation_id,
            operation_type=operation_type,
            outcome=HandoffInspectionOutcome.ABSENT,
            execution_id=execution_id,
            session_id=handle.execution_session_id if handle is not None else None,
            accepted_sequence=None,
            context_binding=handle.context_binding if handle is not None else None,
            observed_at=now,
            evidence_refs=(f"fake-inspection://{operation_id}/absent",),
        )


class RecordingFakeWorkspaceProvider:
    """In-memory WorkspaceProvider with explicit transfer identities."""

    def __init__(self, *, failures: Mapping[str, str] | None = None) -> None:
        self.failures = dict(failures or {})
        self.calls: list[tuple[str, UUID]] = []
        self._manifests: dict[str, ExecutionWorkspaceManifest] = {}
        self._transfer_manifests: dict[UUID, ExecutionWorkspaceManifest] = {}

    def prepare(self, spec: ExecutionSpec, *, operation_id: UUID) -> ExecutionWorkspaceManifest:
        self.calls.append(("prepare", operation_id))
        manifest = ExecutionWorkspaceManifest.create(
            id=uuid5(NAMESPACE_URL, f"sidera.fake.g4.workspace:{spec.execution_id}"),
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            execution_id=spec.execution_id,
            checkouts=tuple(
                WorkspaceCheckout(
                    repository_ref=repo.repository_ref,
                    base_revision=repo.base_revision,
                    branch=f"sidera/g4/{spec.execution_id}",
                    current_head=repo.base_revision,
                )
                for repo in spec.repositories
            ),
            location_ref=f"fake-workspace://r6-2/g4/{spec.execution_id}",
            retention_seconds=3600,
            fencing_token=spec.fencing_token,
            created_at=datetime.now(UTC),
        )
        self._ready_active(manifest)
        return manifest

    def freeze(
        self,
        manifest: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        self.calls.append(("freeze", operation_id))
        self._maybe_fail(FaultPoint.FREEZE_BEFORE)
        manifest.freeze(now=datetime.now(UTC))
        self._manifests[manifest.location_ref] = manifest
        self._maybe_fail(FaultPoint.FREEZE_AFTER)
        return manifest

    def retain(self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID) -> ExecutionWorkspaceManifest:
        del operation_id
        manifest.retain(until=datetime.now(UTC))
        return manifest

    def release(self, manifest: ExecutionWorkspaceManifest, *, operation_id: UUID) -> ExecutionWorkspaceManifest:
        del operation_id
        manifest.release(now=datetime.now(UTC))
        return manifest

    def inspect(self, location_ref: str) -> ExecutionWorkspaceManifest:
        return self._manifests[location_ref]

    def transfer_handoff(
        self,
        source_manifest: ExecutionWorkspaceManifest,
        target: Any,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> ExecutionWorkspaceManifest:
        del transition_id, logical_attempt
        self.calls.append(("transfer_handoff", operation_id))
        self._maybe_fail(FaultPoint.TRANSFER_BEFORE)
        manifest = ExecutionWorkspaceManifest.create(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera.fake.g4.target-manifest:{target.target_execution_id}",
            ),
            workspace_id=source_manifest.workspace_id,
            task_id=target.task_id,
            execution_id=target.target_execution_id,
            checkouts=source_manifest.checkouts,
            location_ref=source_manifest.location_ref,
            retention_seconds=source_manifest.retention_seconds,
            fencing_token=target.anticipated_fencing_token,
            created_at=source_manifest.created_at,
        )
        self._ready(manifest)
        self._transfer_manifests[operation_id] = manifest
        self._maybe_fail(FaultPoint.TRANSFER_AFTER)
        return manifest

    def inspect_handoff_transfer(
        self,
        *,
        transition_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
    ) -> HandoffWorkspaceInspection:
        manifest = self._transfer_manifests.get(operation_id)
        return HandoffWorkspaceInspection(
            transition_id=transition_id,
            logical_attempt=logical_attempt,
            operation_id=operation_id,
            outcome=(
                HandoffInspectionOutcome.SUCCEEDED
                if manifest is not None
                else HandoffInspectionOutcome.ABSENT
            ),
            source_manifest_id=None,
            target_manifest_id=manifest.id if manifest is not None else None,
            source_lifecycle=WorkspaceLifecycle.FROZEN.value,
            target_lifecycle=manifest.lifecycle.value if manifest is not None else None,
            observed_at=datetime.now(UTC),
            evidence_refs=(f"fake-workspace-inspection://{operation_id}",),
        )

    def _ready(self, manifest: ExecutionWorkspaceManifest) -> None:
        manifest.mark_preparing()
        manifest.mark_ready()

    def _ready_active(self, manifest: ExecutionWorkspaceManifest) -> None:
        self._ready(manifest)
        manifest.mark_active()
        self._manifests[manifest.location_ref] = manifest

    def _maybe_fail(self, point: FaultPoint) -> None:
        code = self.failures.get(point.value)
        if code is not None:
            raise UnknownBoundaryError(code)


class _RecoveryGateway:
    def __init__(self, adapter: RecordingFakeAdapter) -> None:
        self.adapter = adapter

    def inspect_handoff_operation(
        self,
        *,
        workspace_id: UUID,
        transition_id: UUID,
        operation_id: UUID,
        operation_type: GatewayOperationType,
        now: datetime,
    ) -> HandoffOperationInspection:
        del workspace_id, transition_id
        return self.adapter.inspect_operation(
            operation_id=operation_id,
            operation_type=operation_type,
            now=now,
        )


class _RecoveryWorkspace:
    def __init__(self, workspace: WorkspaceHandoffService) -> None:
        self.workspace = workspace

    def inspect(
        self,
        *,
        transition_id: UUID,
        workspace_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
        now: datetime,
    ) -> HandoffWorkspaceInspection:
        return self.workspace.inspect(
            transition_id=transition_id,
            workspace_id=workspace_id,
            logical_attempt=logical_attempt,
            operation_id=operation_id,
            now=now,
        )


class FakeHandoffRuntime:
    """Run the real R6.2 services against deterministic fake boundaries."""

    def __init__(
        self,
        database: Database,
        workspace_id: UUID,
        fixture: G4Fixture,
        *,
        now: datetime,
        fault: FaultPoint | str | None = None,
        distinct_adapter_definitions: bool = False,
    ) -> None:
        self.database = database
        self.workspace_id = workspace_id
        self.fixture = fixture
        self.now = now
        self.fault = FaultPoint(fault) if fault is not None else None
        self.distinct_adapter_definitions = distinct_adapter_definitions
        self._prepare_attempt = 0
        self.uow_factory = lambda: SqlAlchemyUnitOfWork(database)
        self.catalog = _EmptyCatalog()
        resolver = WorkerRuntimeResolver()
        self.adapter = RecordingFakeAdapter(
            source_execution_id=fixture.source_execution_id,
            failures=self._adapter_failures(),
            unknown_target_start_once=(
                fixture.scenario is G4Scenario.TARGET_START_UNKNOWN
                or self.fault is FaultPoint.TARGET_START_BEFORE
            ),
            unknown_target_start_after_once=(
                self.fault is FaultPoint.TARGET_START_AFTER
            ),
            handoff_context=(
                fixture.source_package_id,
                fixture.source_package_hash,
                fixture.source_context_version,
            ),
            handoff_workspace_digest=self._source_workspace_digest(),
        )
        self.workspace = RecordingFakeWorkspaceProvider(
            failures=self._workspace_failures(),
        )
        self.gateway = WorkerGateway(
            self.uow_factory,
            adapter_factories={"fake": lambda _instance: self.adapter},
            workspace_factories={"fake-workspace": lambda _instance: self.workspace},
            spec_factory=ConfiguredExecutionSpecFactory(),
            context_materializer=ContextMaterializer(self.catalog),
            clock=lambda: self.now,
        )
        self.workspace_handoff = WorkspaceHandoffService(
            self.workspace,
            uow_factory=self.uow_factory,
        )
        self.recovery = HandoffRecoveryService(
            self.uow_factory,
            gateway=_RecoveryGateway(self.adapter),
            workspace_handoff=_RecoveryWorkspace(self.workspace_handoff),
            runtime_resolver=resolver,
        )
        self.candidates = HandoffCandidateService(
            self.uow_factory,
            runtime_resolver=resolver,
        )
        self.scheduler = Scheduler(
            self.uow_factory,
            LowestCostWorkerPolicy(),
            lambda: self.now,
            runtime_resolver=resolver,
            monotonic_clock=lambda: 1.0,
        )
        self.activation = ActivationReconciler(
            uow_factory=self.uow_factory,
            owner_id="r6-2-fake-activation-reconciler",
            clock=lambda: self.now,
        )
        self.context_service = HandoffContextService(self.uow_factory)
        self.input_transfer = HandoffInputTransferService(self.uow_factory)
        self.target_service = HandoffTargetService(self.uow_factory)
        self.coordinator = HandoffCoordinator(
            uow_factory=self.uow_factory,
            gateway=self.gateway,
            workspace_handoff=self.workspace_handoff,
            target_service=self.target_service,
            context_service=self.context_service,
            input_transfer=self.input_transfer,
            scheduler=self.scheduler,
            clock=lambda: self.now,
            owner_id="r6-2-fake-handoff-coordinator",
            lease_ttl_seconds=3600,
        )
        self.context_worker = ContextPackageWorker(
            uow_factory=self.uow_factory,
            catalog=self.catalog,
            owner_id="r6-2-fake-context-worker",
            clock=lambda: self.now,
        )
        self.commands = CommandService(
            self.uow_factory,
            (
                ExecutionCommandHandler(lambda: self.now, lease_ttl_seconds=3600),
                ExecutionInputCommandHandler(lambda: self.now),
                HandoffCommandHandler(
                    lambda: self.now,
                    candidate_service=self.candidates,
                    runtime_resolver=resolver,
                    recovery_service=self.recovery,
                ),
            ),
        )
        self._prime_source_session()

    def prepare_handoff(self, target_worker_id: UUID | None = None) -> Any:
        target_worker_id = target_worker_id or self.fixture.target_worker_ids[0]
        self._prepare_attempt += 1
        with self.uow_factory() as uow:
            item = uow.worker_items.get(self.fixture.worker_item_ids["b"], self.workspace_id)
            if item is None:
                raise AssertionError("G4 source WorkerItem is missing")
            version = item.version
        envelope = CommandEnvelope(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera.fake.g4.prepare:{self.workspace_id}:{self._prepare_attempt}",
            ),
            workspace_id=self.workspace_id,
            command_type="PrepareWorkerHandoff",
            target_type="worker_item",
            actor_type="user",
            actor_id="r6-2-e2e",
            target_id=self.fixture.worker_item_ids["b"],
            expected_version=version,
            payload={
                "target_worker_id": str(target_worker_id),
                "handoff_reason": "move the WorkerItem to a compatible agent",
                "handoff_instruction": "continue from the captured source checkpoint",
                "input_plan": {"preserve": ["delivered"], "transfer": ["queued"]},
                "proposal_ttl_seconds": 900,
            },
            idempotency_key=(
                f"r6-2-g4-prepare:{self.workspace_id}:{self._prepare_attempt}"
            ),
            correlation_id=uuid5(NAMESPACE_URL, f"sidera.fake.g4.correlation:{self.workspace_id}"),
        )
        result = self.commands.handle(envelope)
        with self.uow_factory() as uow:
            proposal = uow.handoff_proposals.get(result.aggregate_id, self.workspace_id)
        if proposal is None:
            raise AssertionError("handoff proposal was not persisted")
        return proposal

    def update_pending_source_input(self) -> Any:
        with self.uow_factory() as uow:
            session = uow.sessions.get_any(
                self.fixture.source_session_id,
                self.workspace_id,
            )
            if session is None:
                raise AssertionError("G4 source Session is missing")
            session_version = session.version
        envelope = CommandEnvelope(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera.fake.g4.update-input:{self.workspace_id}",
            ),
            workspace_id=self.workspace_id,
            command_type="UpdatePendingExecutionInput",
            target_type="execution_session",
            actor_type="user",
            actor_id="r6-2-e2e",
            target_id=self.fixture.source_session_id,
            expected_version=session_version,
            payload={
                "execution_id": str(self.fixture.source_execution_id),
                "client_idempotency_key": "queued-source-input",
                "text": "Transfer this queued instruction after the user revised it.",
            },
            idempotency_key=f"r6-2-g4-update-input:{self.workspace_id}",
            correlation_id=uuid5(
                NAMESPACE_URL,
                f"sidera.fake.g4.update-input-correlation:{self.workspace_id}",
            ),
        )
        return self.commands.handle(envelope)

    def confirm_handoff(self, proposal: Any) -> HandoffTransition:
        worker_item_version = int(proposal.payload.impact_summary["worker_item_version"])
        envelope = CommandEnvelope(
            id=uuid5(NAMESPACE_URL, f"sidera.fake.g4.confirm:{proposal.id}"),
            workspace_id=self.workspace_id,
            command_type="ConfirmWorkerHandoff",
            target_type="handoff_proposal",
            actor_type="user",
            actor_id="r6-2-e2e",
            target_id=proposal.id,
            expected_version=proposal.version,
            payload={
                "proposal_id": str(proposal.id),
                "expected_proposal_version": proposal.version,
                "expected_proposal_hash": proposal.proposal_hash,
                "expected_worker_item_version": worker_item_version,
            },
            idempotency_key=f"r6-2-g4-confirm:{proposal.id}",
            correlation_id=uuid5(NAMESPACE_URL, f"sidera.fake.g4.confirm-correlation:{proposal.id}"),
        )
        result = self.commands.handle(envelope)
        with self.uow_factory() as uow:
            transition = uow.handoff_transitions.get(result.aggregate_id, self.workspace_id)
        if transition is None:
            raise AssertionError("handoff transition was not persisted")
        return transition

    def run_once(self) -> tuple[Any, Any]:
        context_result = self.context_worker.run_once(limit=10)
        handoff_result = self.coordinator.run_once(limit=10)
        return context_result, handoff_result

    def run_activation(self) -> Any:
        return self.activation.run_once(limit=10)

    def attach_c_dependency_to_target(self, transition_id: UUID) -> None:
        """Rebind the fixture's downstream edge to the new handoff lineage.

        Production plan publication owns this edge.  The G4 fixture adds it
        after target request creation so the test can prove that activation
        follows the new lineage rather than the retired source execution.
        """

        with self.uow_factory() as uow:
            target_request = uow.execution_requests.get_by_handoff_transition(
                transition_id,
                self.workspace_id,
            )
            downstream = uow.execution_requests.get(
                self.fixture.request_c_id,
                self.workspace_id,
                for_update=True,
            )
            if target_request is None or downstream is None:
                raise AssertionError("G4 target/downstream request is missing")
            uow.session.execute(
                delete(ExecutionRequestDependencyModel).where(
                    ExecutionRequestDependencyModel.workspace_id == self.workspace_id,
                    ExecutionRequestDependencyModel.downstream_request_id
                    == downstream.id,
                )
            )
            uow.execution_requests.add_dependency(
                ExecutionRequestDependency(
                    downstream_request_id=downstream.id,
                    upstream_request_id=target_request.id,
                    enforcement=DependencyEnforcement.HARD,
                )
            )
            uow.session.flush()

    def complete_target_execution(self, transition_id: UUID) -> Any:
        with self.uow_factory() as uow:
            transition = uow.handoff_transitions.get(
                transition_id,
                self.workspace_id,
            )
            if transition is None or transition.target_execution_id is None:
                raise AssertionError("G4 target execution is missing")
            execution = uow.executions.get(
                transition.target_execution_id,
                self.workspace_id,
            )
            lease = uow.leases.get_active_for_execution(
                transition.target_execution_id,
                self.workspace_id,
            )
            if execution is None or lease is None:
                raise AssertionError("G4 target execution lease is missing")
            worker_id = lease.worker_id
        envelope = CommandEnvelope(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera.fake.g4.complete-target:{transition_id}",
            ),
            workspace_id=self.workspace_id,
            command_type="ReportExecutionSucceeded",
            target_type="execution",
            actor_type="worker",
            actor_id=str(worker_id),
            target_id=execution.id,
            expected_version=execution.version,
            payload={
                "lease_id": str(lease.id),
                "worker_id": str(worker_id),
                "fencing_token": lease.fencing_token,
            },
            idempotency_key=f"r6-2-g4-complete-target:{transition_id}",
            correlation_id=uuid5(
                NAMESPACE_URL,
                f"sidera.fake.g4.complete-target-correlation:{transition_id}",
            ),
        )
        return self.commands.handle(envelope)

    def run_until_converged(self, transition_id: UUID, *, limit: int = 80) -> HandoffTransition:
        for _ in range(limit):
            self.run_once()
            transition = self._transition(transition_id)
            if transition.status.value == "converged":
                return transition
            if transition.status.value == "blocked_unknown":
                raise AssertionError(
                    f"handoff became unknown at {transition.phase.value}: {transition.failure_code}"
                )
            if transition.status.value in {"failed", "blocked"}:
                raise AssertionError(
                    f"handoff did not progress at {transition.phase.value}: {transition.failure_code}"
                )
        raise AssertionError("handoff did not converge within the deterministic run limit")

    def run_until_unknown(self, transition_id: UUID, *, limit: int = 80) -> HandoffTransition:
        for _ in range(limit):
            self.run_once()
            transition = self._transition(transition_id)
            if transition.status.value == "blocked_unknown":
                return transition
            if transition.status.value in {"failed", "blocked", "converged"}:
                raise AssertionError(
                    f"handoff did not reach unknown state: {transition.status.value}"
                )
        raise AssertionError("handoff did not reach unknown state within the run limit")

    def recover_target_start_unknown(self, transition_id: UUID) -> HandoffTransition:
        transition = self._transition(transition_id)
        inspection = self.recovery.inspect_unknown(transition=transition, now=self.now)
        if HandoffSafeConclusion.TARGET_ABSENT not in inspection.safe_conclusions:
            raise AssertionError(
                f"target start is not proven absent: {inspection.safe_conclusions}"
            )
        envelope = CommandEnvelope(
            id=uuid5(NAMESPACE_URL, f"sidera.fake.g4.recover:{transition.id}:{transition.version}"),
            workspace_id=self.workspace_id,
            command_type="RecoverWorkerHandoff",
            target_type="handoff_transition",
            actor_type="user",
            actor_id="r6-2-e2e",
            target_id=transition.id,
            expected_version=transition.version,
            payload={
                "transition_id": str(transition.id),
                "expected_transition_version": transition.version,
                "strategy": HandoffRecoveryStrategy.CONFIRM_TARGET_ABSENT.value,
                "reason": "fake target start was proven absent by adapter inspection",
                "evidence_refs": list(inspection.evidence_refs),
            },
            idempotency_key=f"r6-2-g4-recover:{transition.id}:{transition.version}",
            correlation_id=uuid5(NAMESPACE_URL, f"sidera.fake.g4.recover-correlation:{transition.id}:{transition.version}"),
        )
        self.commands.handle(envelope)
        return self._transition(transition_id)

    def _transition(self, transition_id: UUID) -> HandoffTransition:
        with self.uow_factory() as uow:
            transition = uow.handoff_transitions.get(transition_id, self.workspace_id)
        if transition is None:
            raise AssertionError("handoff transition disappeared")
        return transition

    def _prime_source_session(self) -> None:
        with self.uow_factory() as uow:
            execution = uow.executions.get(self.fixture.source_execution_id, self.workspace_id)
            binding = uow.bindings.get_for_execution(self.fixture.source_execution_id, self.workspace_id)
            task = uow.tasks.get(self.fixture.subtask_ids["b"], self.workspace_id)
            adapter_instance = uow.modules.get_instance(
                self.fixture.adapter_instance_id, self.workspace_id
            )
            workspace_instance = uow.modules.get_instance(
                self.fixture.workspace_instance_id, self.workspace_id
            )
            manifest = uow.workspaces.get(self.fixture.source_manifest_id, self.workspace_id)
        if any(
            item is None
            for item in (execution, binding, task, adapter_instance, workspace_instance, manifest)
        ):
            raise AssertionError("G4 source runtime facts are incomplete")
        assert execution is not None
        assert binding is not None
        assert task is not None
        assert adapter_instance is not None
        assert workspace_instance is not None
        assert manifest is not None
        spec = ConfiguredExecutionSpecFactory().create(
            cast(Any, execution),
            task,
            binding,
            adapter_instance,
            workspace_instance,
        )
        self.adapter.start_session(
            spec,
            manifest,
            operation_id=gateway_operation_id(
                self.fixture.source_execution_id,
                GatewayOperationType.START_SESSION,
            ),
        )

    def _source_workspace_digest(self) -> str:
        with self.uow_factory() as uow:
            manifest = uow.workspaces.get(self.fixture.source_manifest_id, self.workspace_id)
        if manifest is None:
            raise AssertionError("G4 source workspace manifest is missing")
        from sidera_control_plane.domain.workspace import workspace_manifest_digest

        return workspace_manifest_digest(manifest)

    def _adapter_failures(self) -> dict[str, str]:
        fault = self.fault
        if fault is not None and fault in {
            FaultPoint.CHECKPOINT_BEFORE,
            FaultPoint.CHECKPOINT_AFTER,
            FaultPoint.STOP_BEFORE,
            FaultPoint.STOP_AFTER,
            FaultPoint.WRITER_BEFORE,
            FaultPoint.WRITER_AFTER,
        }:
            return {fault.value: f"{fault.value}:unknown"}
        return {}

    def _workspace_failures(self) -> dict[str, str]:
        fault = self.fault
        if fault is not None and fault in {
            FaultPoint.FREEZE_BEFORE,
            FaultPoint.FREEZE_AFTER,
            FaultPoint.TRANSFER_BEFORE,
            FaultPoint.TRANSFER_AFTER,
        }:
            return {fault.value: f"{fault.value}:unknown"}
        return {}


def main(argv: Sequence[str] | None = None) -> int:
    """Drive one browser-confirmed G4 transition through the fake boundaries."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--scenario",
        choices=tuple(item.value for item in G4Scenario),
        default=G4Scenario.NORMAL.value,
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--until-unknown", action="store_true")
    parser.add_argument("--until-converged", action="store_true")
    args = parser.parse_args(argv)
    selected_modes = sum(
        bool(value) for value in (args.once, args.until_unknown, args.until_converged)
    )
    if selected_modes > 1:
        parser.error("--once, --until-unknown and --until-converged are mutually exclusive")
    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the R6.2 fake runtime")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the R6.2 fake runtime")
    if urlsplit(database_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("R6.2 fake runtime requires a loopback database host")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc

    database = Database(database_url)
    runtime: FakeHandoffRuntime | None = None
    try:
        fixture = load_g4_fixture(
            database,
            workspace_id,
            scenario=G4Scenario(args.scenario),
        )
        runtime = FakeHandoffRuntime(
            database,
            workspace_id,
            fixture,
            now=datetime.now(UTC),
        )
        with runtime.uow_factory() as uow:
            transition = uow.handoff_transitions.get_active_for_worker_item(
                fixture.worker_item_ids["b"],
                workspace_id,
            )
        if transition is None:
            raise RuntimeError("G4 workspace has no active handoff transition")
        if args.until_unknown:
            transition = runtime.run_until_unknown(transition.id)
            inspection = runtime.recovery.inspect_unknown(
                transition=transition,
                now=datetime.now(UTC),
            )
            runtime.recovery.persist_operation_inspection(
                transition=transition,
                inspection=inspection,
            )
        elif args.until_converged:
            transition = runtime.run_until_converged(transition.id)
        else:
            runtime.run_once()
            transition = runtime._transition(transition.id)
        print(
            json.dumps(
                {
                    "component": "r6-2-fake-runtime",
                    "status": "ok",
                    "transition_status": transition.status.value,
                    "phase": transition.phase.value,
                    "logical_attempt": transition.logical_attempt,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
