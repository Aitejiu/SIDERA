"""Public CP composition using externally owned agents interfaces."""

import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, Request
from sidera_agents.api import (
    AgentCatalog,
    AgentExecution,
    AgentObservations,
    AgentRuntimes,
)
from sidera_agents.contracts import SourceContent, SourceRef
from sidera_agents.errors import AgentError
from sidera_agents.ports import ContextSourceReader, ExecutionAuthorizer

from sidera_control_plane.activation_worker import build_activation_worker
from sidera_control_plane.adapters.planning_workspace import (
    PrivatePlanningWorkspaceProvider,
)
from sidera_control_plane.api.app import _build_app
from sidera_control_plane.api.assignment_queries import get_assignment_reads
from sidera_control_plane.api.dependencies import (
    get_assignment_agent_catalog,
    get_assignment_agent_runtimes,
    get_assignment_authorizer,
    get_native_worker_services,
    get_trusted_assignment_identity,
)
from sidera_control_plane.api.native_worker_queries import get_native_worker_reads
from sidera_control_plane.application.agent_worker_adapter import NativeWorkerServices
from sidera_control_plane.application.assignment_queries import AssignmentQueryService
from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
    NativeAuthorityReader,
    PlanningAuthorityReader,
)
from sidera_control_plane.application.execution_commands import ExecutionCommandHandler
from sidera_control_plane.application.native_worker_queries import (
    NativeWorkerQueryService,
)
from sidera_control_plane.application.request_authority import (
    ControlPlaneIdentity,
    ControlPlaneRequestAuthorizer,
    ControlPlaneRequestTarget,
)
from sidera_control_plane.application.task_tool_scope import read_task_tool_scope
from sidera_control_plane.application.worker_commands import WorkerCommandHandler
from sidera_control_plane.collaboration_worker import build_collaboration_worker
from sidera_control_plane.composition.native_setup import (
    configure_native_workspace,
    read_native_workspace_setup,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.context_package_worker import build_context_package_worker
from sidera_control_plane.domain.agent_tools import (
    ControlPlaneToolScope,
    WorkerProgressTarget,
)
from sidera_control_plane.domain.assignment_views import (
    AgentBindingView,
    AgentExecutionView,
    AssignmentKind,
    NativeRunSummary,
    ProfileWorkKind,
    ProfileWorkPage,
    ProfileWorkRunTarget,
    ProfileWorkTarget,
)
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.execution import ExecutionSessionStatus
from sidera_control_plane.domain.native_setup import (
    PlanningPolicyRef,
    WorkspaceSetupReceipt,
    WorkspaceSetupRequest,
    WorkspaceSetupView,
)
from sidera_control_plane.domain.native_worker_views import (
    NativeWorkerContextView,
    NativeWorkerTraceView,
)
from sidera_control_plane.gateway_owner import GatewayOwner, build_gateway_owner
from sidera_control_plane.notification_worker import build_notification_worker
from sidera_control_plane.persistence.assignment_read_store import (
    PostgresAssignmentReadStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.migration_resources import (
    migrate_database,
    schema_ready,
)
from sidera_control_plane.persistence.native_repository_resolver import (
    SavedNativeRepositories,
)
from sidera_control_plane.persistence.native_verification_sessions import (
    SavedNativeVerificationSessions,
)
from sidera_control_plane.persistence.native_worker_slots import SavedNativeWorkerSlots
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from sidera_control_plane.persistence.worker_workspace_query_store import (
    SqlAlchemyWorkerWorkspaceQueryStore,
)
from sidera_control_plane.plan_transition_worker import build_plan_transition_worker
from sidera_control_plane.planning_worker import (
    PlanningWorkerRuntime,
    build_planning_worker,
)
from sidera_control_plane.reconciler_worker import build_reconciler
from sidera_control_plane.result_worker import build_result_worker
from sidera_control_plane.scheduler_worker import build_scheduler

__all__ = [
    "AgentBindingView",
    "AgentExecutionView",
    "AssignmentKind",
    "ControlPlaneIdentity",
    "ControlPlaneRequestAuthorizer",
    "ControlPlaneRequestTarget",
    "ControlPlaneRuntime",
    "ControlPlaneToolScope",
    "NativeRunSummary",
    "PlanningPolicyRef",
    "ProfileWorkKind",
    "ProfileWorkPage",
    "ProfileWorkRunTarget",
    "ProfileWorkTarget",
    "WorkerProgressTarget",
    "WorkspaceSetupReceipt",
    "WorkspaceSetupRequest",
    "WorkspaceSetupView",
    "create_control_plane_runtime",
]

logger = logging.getLogger(__name__)


class _NativeContextSourceReader:
    def __init__(self, uow_factory, *, worker, planner, closed):
        self._uow_factory = uow_factory
        self._worker, self._planner, self._closed = worker, planner, closed

    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent:
        if self._closed():
            raise AgentError("control_plane_closed", "Control plane is closed")
        if (
            not isinstance(grant_id, UUID)
            or not isinstance(ref, SourceRef)
            or ref.provider != "control-plane"
        ):
            raise AgentError("permission_denied", "Control-plane source access denied")
        with self._uow_factory() as uow:
            worker = uow.execution_authorities.get(grant_id)
            planner = uow.planning_authorities.get(grant_id)
        if (worker is None) == (planner is None):
            raise AgentError("permission_denied", "Control-plane source access denied")
        reader = self._worker() if worker is not None else self._planner()
        if reader is None:
            raise AgentError(
                "engine_unavailable", "Control-plane sources are unavailable"
            )
        return reader.read(grant_id, ref)


class ControlPlaneRuntime:
    def __init__(
        self,
        settings: Settings,
        agent_execution: AgentExecution,
        agent_catalog: AgentCatalog,
        agent_observations: AgentObservations,
        source_reader: ContextSourceReader,
        request_authorizer: ControlPlaneRequestAuthorizer | None,
        agent_runtimes: AgentRuntimes | None,
    ) -> None:
        if settings.interview_demo_manifest is not None:
            raise ValueError("Native runtime does not use acceptance manifests")
        self._settings = settings
        self._execution = agent_execution
        self._catalog = agent_catalog
        self._observations = agent_observations
        self._source_reader = source_reader
        self._request_authorizer = request_authorizer
        self._runtimes = agent_runtimes
        self._database = Database(settings.database_url)
        uow_factory = self._uow
        principal = lambda: settings.local_actor_id
        self._execution_authorizer = NativeAuthorityReader(
            uow_factory,
            worker=ExecutionAuthorityReader(
                uow_factory,
                principal=principal,
                authorizer=request_authorizer,
            ),
            planner=PlanningAuthorityReader(
                uow_factory,
                principal=principal,
                authorizer=request_authorizer,
            ),
        )
        self._closed = False
        self._closing = False
        self._disposing = False
        self._disposal_complete = False
        self._disposal_cleanup: list[tuple[str, Callable[[], None]]] | None = None
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._admitted = 0
        self._active_work = 0
        self._regular_work = 0
        self._starting = False
        self._startup_failed = False
        self._drain_started = False
        self._background_identity: ControlPlaneIdentity | None = None
        self._owner: GatewayOwner | None = None
        self._planning: PlanningWorkerRuntime | None = None
        self._services: NativeWorkerServices | None = None
        self._business_cleanup: list[Callable[[], None]] = []
        self._repositories = SavedNativeRepositories(uow_factory)
        self._slots = SavedNativeWorkerSlots(uow_factory)
        self._heartbeat_after: UUID | None = None
        self._verifications = SavedNativeVerificationSessions(uow_factory)
        self._verification_after: UUID | None = None
        self._drained = threading.Event()
        self._context_source_reader = _NativeContextSourceReader(
            uow_factory,
            worker=lambda: (
                None
                if self._owner is None
                else self._owner.gateway_runtime.native_source_reader
            ),
            planner=lambda: (
                None if self._planning is None else self._planning.native_source_reader
            ),
            closed=lambda: self._closed,
        )
        self.app = _build_app(
            settings,
            database=self._database,
            native=True,
            request_authorizer=request_authorizer,
            admission_check=self._check_request,
            request_admission=self._request_admission,
        )

        def identity(request: Request) -> ControlPlaneIdentity | None:
            return getattr(request.state, "control_plane_identity", None)

        self.app.dependency_overrides[get_trusted_assignment_identity] = identity
        self.app.dependency_overrides[get_assignment_authorizer] = lambda: (
            request_authorizer
        )
        self.app.dependency_overrides[get_assignment_agent_catalog] = lambda: (
            agent_catalog
        )
        self.app.dependency_overrides[get_assignment_agent_runtimes] = lambda: (
            agent_runtimes
        )
        self.app.dependency_overrides[get_native_worker_services] = lambda: (
            self._services
        )
        self.app.dependency_overrides[get_assignment_reads] = lambda: self
        self.app.dependency_overrides[get_native_worker_reads] = lambda: self

    @property
    def execution_authorizer(self) -> ExecutionAuthorizer:
        return self._execution_authorizer

    @property
    def context_source_reader(self) -> ContextSourceReader:
        return self._context_source_reader

    def _uow(self) -> SqlAlchemyUnitOfWork:
        if self._closed:
            raise AgentError("control_plane_closed", "Control plane is closed")
        return SqlAlchemyUnitOfWork(
            self._database,
            before_commit=None if self._owner is None else self._owner.require_owner,
            projection_change_retention_seconds=self._settings.projection_change_retention_seconds,
        )

    def _check_open(self) -> None:
        if self._closed:
            raise HTTPException(503, detail={"code": "control_plane_closed"})

    def _check_request(self, request: Request) -> None:
        with self._condition:
            if self._closed or (
                self._closing and request.method not in {"GET", "HEAD", "OPTIONS"}
            ):
                raise HTTPException(503, detail={"code": "control_plane_closed"})

    @contextmanager
    def _request_admission(self, request: Request) -> Iterator[None]:
        with self._condition:
            self._check_request(request)
            self._active_work += 1
            self._regular_work += 1
            self._drained.clear()
        try:
            yield
        finally:
            with self._condition:
                self._active_work -= 1
                self._regular_work -= 1
                self._drained.clear()
                self._condition.notify_all()

    def _require_background_identity(
        self,
        identity: ControlPlaneIdentity | None,
    ) -> ControlPlaneIdentity:
        if identity is None:
            raise AgentError(
                "background_authority_required", "Background authority is required"
            )
        if (
            not isinstance(identity, ControlPlaneIdentity)
            or not isinstance(identity.principal_id, str)
            or not identity.principal_id.strip()
            or identity.principal_id != self._settings.local_actor_id
            or not isinstance(identity.grant_id, UUID)
            or identity.actor_type != "system"
            or self._request_authorizer is None
        ):
            raise AgentError("permission_denied", "Background authority is unavailable")
        try:
            current = self._request_authorizer.identity_for_grant(identity.grant_id)
        except Exception:  # noqa: BLE001 - Host errors are never exposed.
            current = None
        if current != identity:
            raise AgentError("permission_denied", "Background authority is unavailable")
        return identity

    @contextmanager
    def _start_admission(self) -> Iterator[None]:
        self._require_background_identity(self._background_identity)
        with self._condition:
            if self._closing or self._closed:
                raise AgentError("control_plane_closed", "Control plane is closed")
            self._admitted += 1
            self._drained.clear()
        try:
            yield
        finally:
            with self._condition:
                self._admitted -= 1
                self._drained.clear()
                self._condition.notify_all()

    def start_background(
        self,
        *,
        background_identity: ControlPlaneIdentity | None = None,
    ) -> None:
        identity = self._require_background_identity(
            self._background_identity
            if background_identity is None
            else background_identity
        )
        with self._condition:
            if self._closing or self._closed or self._startup_failed:
                raise AgentError("control_plane_closed", "Control plane is closed")
            if self._background_identity is not None:
                if identity != self._background_identity:
                    raise AgentError(
                        "operation_conflict", "Background identity cannot change"
                    )
                if self._owner is not None:
                    return
            if self._starting:
                raise AgentError(
                    "operation_conflict", "Background startup is already in progress"
                )
            if self._runtimes is None or self._execution is None:
                raise AgentError(
                    "engine_unavailable", "Native execution is unavailable"
                )
            self._starting = True
        owner = None
        planning = None
        cleanup: list[Callable[[], None]] = []
        try:
            authorizer = self._request_authorizer
            if authorizer is None:
                raise AgentError(
                    "permission_denied", "Background authority is unavailable"
                )
            services = NativeWorkerServices(
                self._catalog,
                self._runtimes,
                self._execution,
                authorizer,
                identity.principal_id,
                identity.grant_id,
                self._observations,
                self._start_admission,
            )
            owner = build_gateway_owner(
                self._settings,
                native_services=services,
                use_acceptance_guard=False,
                assignment_claim_check=lambda: (
                    not (self._starting or self._closing or self._closed)
                ),
                repository_resolver=self._repositories,
                repository_refs=self._repositories.repository_refs,
            )
            planning = build_planning_worker(
                self._settings,
                native_services=services,
                planning_authorizer=authorizer,
                planning_workspace_provider=PrivatePlanningWorkspaceProvider(
                    self._settings.planning_temp_root.expanduser().resolve(),
                ),
                owner_check=owner.gateway_runtime.owner_check,
                use_acceptance_guard=False,
                native_shutdown_check=lambda: self._closing,
            )
            settings = self._settings
            owner_check = owner.gateway_runtime.owner_check

            def retain(runtime):
                cleanup.append(runtime.database.dispose)
                catalog = getattr(runtime, "catalog", None)
                if catalog is not None:
                    cleanup.append(catalog.close)
                return runtime

            scheduler = retain(
                build_scheduler(
                    settings, owner_check=owner_check, native_services=services
                )
            )
            activation = retain(
                build_activation_worker(settings, owner_check=owner_check)
            )
            context = retain(
                build_context_package_worker(settings, owner_check=owner_check)
            )
            collaboration = retain(
                build_collaboration_worker(
                    settings, owner_check=owner_check, scan_all_workspaces=True
                )
            )
            transition = retain(
                build_plan_transition_worker(settings, owner_check=owner_check)
            )
            result = retain(
                build_result_worker(
                    settings,
                    owner_check=owner_check,
                    repository_resolver=self._repositories,
                    repository_refs=self._repositories.repository_refs,
                )
            )
            lease_reconciler = retain(
                build_reconciler(settings, owner_check=owner_check)
            )
            notification = (
                None
                if not settings.notification_enabled
                else retain(
                    build_notification_worker(settings, owner_check=owner_check)
                )
            )
            with self._condition:
                if self._closing or self._closed:
                    raise AgentError("control_plane_closed", "Control plane is closed")
                self._background_identity = identity
                self._services = services
                self._owner = owner
                self._planning = planning
                self._business_cleanup = cleanup
            self._launch(
                "gateway",
                lambda: owner.gateway.run_once(settings.gateway_batch_size),
                settings.gateway_poll_interval_seconds,
            )
            self._launch(
                "planning",
                lambda: planning.worker.run_once(settings.planning_batch_size),
                settings.planning_poll_interval_seconds,
            )
            self._launch(
                "input",
                lambda: owner.input_runtime.dispatcher.run_once(
                    settings.gateway_batch_size
                ),
                settings.gateway_poll_interval_seconds,
            )
            self._launch(
                "handoff",
                lambda: owner.handoff_runtime.coordinator.run_once(
                    settings.handoff_batch_size
                ),
                settings.handoff_poll_interval_seconds,
            )
            self._launch(
                "scheduler",
                lambda: scheduler.scheduler.run_once(settings.scheduler_batch_size),
                settings.scheduler_poll_interval_seconds,
            )
            self._launch(
                "activation",
                lambda: activation.worker.run_once(settings.activation_batch_size),
                settings.activation_poll_interval_seconds,
            )
            self._launch(
                "context",
                lambda: context.worker.run_once(settings.context_assembly_batch_size),
                1.0,
            )
            self._launch(
                "collaboration",
                lambda: collaboration.worker.run_once(
                    settings.collaboration_batch_size
                ),
                settings.collaboration_poll_seconds,
            )
            recovery_sweep = collaboration.recovery_sweep
            if recovery_sweep is not None:
                self._launch(
                    "collaboration-recovery",
                    lambda: recovery_sweep(datetime.now(UTC)),
                    settings.collaboration_recovery_sweep_seconds,
                )
            self._launch(
                "plan-transition",
                lambda: transition.coordinator.run_once(
                    settings.plan_transition_batch_size
                ),
                settings.plan_transition_poll_interval_seconds,
            )
            self._launch("result", lambda: result.worker.run_once(), 1.0)
            self._launch(
                "reconciler",
                lambda: lease_reconciler.reconciler.run_once(
                    settings.reconciler_batch_size
                ),
                settings.reconciler_poll_interval_seconds,
            )
            if notification is not None:
                self._launch(
                    "notification",
                    lambda: notification.worker.run_once(
                        settings.notification_reconcile_batch_size
                    ),
                    settings.notification_poll_seconds,
                )
            self._launch(
                "slot-heartbeat",
                self._heartbeat_owned_slots,
                min(
                    settings.assignment_lease_renew_interval_seconds,
                    settings.worker_heartbeat_timeout_seconds / 3,
                ),
            )
            self._launch(
                "verification-original",
                self._observe_original_verifications,
                settings.gateway_poll_interval_seconds,
                maintenance=True,
            )
            for family, reconciler in (
                ("worker", owner.gateway_runtime.native_reconciler),
                ("planner", planning.native_reconciler),
            ):
                if reconciler is not None:
                    self._launch(
                        family + "-original",
                        lambda reconciler=reconciler: reconciler.run_once(
                            None,
                            min(settings.gateway_batch_size, 200),
                        ),
                        settings.gateway_poll_interval_seconds,
                        maintenance=True,
                    )
            with self._condition:
                if self._closing or self._closed:
                    raise AgentError("control_plane_closed", "Control plane is closed")
                self._starting = False
                self._condition.notify_all()
        except BaseException:
            self._startup_failed = True
            self._stop.set()
            rollback_deadline = time.monotonic() + 2.0
            for thread in self._threads:
                thread.join(timeout=max(0.0, rollback_deadline - time.monotonic()))
            if any(thread.is_alive() for thread in self._threads):
                with self._condition:
                    self._closing = True
                    self._owner, self._planning = owner, planning
                    self._business_cleanup = cleanup
            else:
                with self._condition:
                    self._owner = self._planning = self._services = None
                    self._background_identity = None
                self._dispose_business(cleanup)
                if planning is not None:
                    planning.catalog.close()
                    planning.database.dispose()
                if owner is not None:
                    owner.close()
            raise
        finally:
            with self._condition:
                self._starting = False
                self._condition.notify_all()

    def _launch(
        self,
        name: str,
        callback: Callable[[], object],
        interval: float,
        *,
        maintenance: bool = False,
    ) -> None:
        def consume() -> None:
            last_failure: float | None = None
            while not self._stop.is_set():
                with self._condition:
                    if self._stop.is_set():
                        return
                    eligible = not self._starting and (maintenance or not self._closing)
                    if eligible:
                        self._active_work += 1
                        if not maintenance:
                            self._regular_work += 1
                            self._drained.clear()
                if eligible:
                    try:
                        if not maintenance and self._owner is not None:
                            self._owner.require_owner()
                            self._require_background_identity(self._background_identity)
                        callback()
                        last_failure = None
                    except Exception as exc:  # noqa: BLE001 - Durable actions stay available for the next tick.
                        now = time.monotonic()
                        if last_failure is None or now - last_failure >= 30.0:
                            logger.warning(
                                "control_plane_consumer_failed callback=%s exception=%s",
                                name,
                                type(exc).__name__,
                            )
                            last_failure = now
                    finally:
                        with self._condition:
                            self._active_work -= 1
                            if not maintenance:
                                self._regular_work -= 1
                                self._drained.clear()
                            self._condition.notify_all()
                self._stop.wait(interval)

        thread = threading.Thread(target=consume, name="sidera-cp-" + name, daemon=True)
        try:
            thread.start()
        finally:
            if thread.ident is not None:
                with self._condition:
                    self._threads.append(thread)

    def _check_drain(self) -> None:
        with self._condition:
            if self._starting or self._admitted or self._regular_work:
                self._drained.clear()
                return
        with self._uow() as uow:
            workers = uow.execution_authorities.list_reconciliation_links(
                None, limit=200
            )
            planners = uow.planning_authorities.list_reconciliation_links(
                None, limit=200
            )
        for link in workers:
            if link.run_id is None and link.status in {
                "pending",
                "waiting_capacity",
                "failed",
            }:
                self._cancel_unadmitted_worker(link)
            else:
                self._request_original_worker_stop(link)
        planner_results = [
            self._planning is not None
            and self._planning.runner is not None
            and self._planning.runner.drain_original_run(link)
            for link in planners
        ]
        if (
            not workers
            and all(planner_results)
            and not self._verifications.has_outstanding()
        ):
            with self._condition:
                if not self._starting and not self._admitted and not self._regular_work:
                    self._drained.set()
                    self._condition.notify_all()

    def _observe_original_verifications(self) -> None:
        if self._owner is None:
            return
        self._owner.require_owner()
        originals = self._verifications.page(
            after=self._verification_after,
            limit=min(self._settings.gateway_batch_size, 200),
        )
        if not originals:
            self._verification_after = None
            return
        self._verification_after = originals[-1].execution_id
        with self._condition:
            closing = (
                self._closing
                and not self._starting
                and not self._admitted
                and not self._regular_work
            )
        if closing:
            for item in originals:
                self._request_original_verification_stop(item)
        else:
            targets: list[tuple[UUID, UUID, UUID, UUID, UUID, int]] = []
            for item in originals:
                if (
                    not item.observable
                    or item.stop_requested
                    or item.session_id is None
                    or item.lease_id is None
                    or item.worker_id is None
                    or item.fencing_token is None
                ):
                    continue
                targets.append(
                    (
                        item.workspace_id,
                        item.execution_id,
                        item.session_id,
                        item.lease_id,
                        item.worker_id,
                        item.fencing_token,
                    )
                )
            self._owner.gateway_runtime.worker_gateway.sync_selected_sessions(
                tuple(targets)
            )

    def _request_original_verification_stop(self, expected) -> None:
        if not expected.observable:
            return
        with self._uow() as uow:
            execution = uow.executions.get(expected.execution_id, expected.workspace_id)
        if execution is None or execution.stop_requested:
            return
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera.native.close-stop:{expected.execution_id}:{expected.session_id}",
        )
        command = CommandEnvelope(
            id=command_id,
            workspace_id=expected.workspace_id,
            command_type="StopExecution",
            target_type="execution",
            target_id=expected.execution_id,
            actor_type="system",
            actor_id=self._settings.local_actor_id,
            payload={},
            expected_version=execution.version,
            idempotency_key=f"native-close-stop:{expected.execution_id}:{expected.session_id}",
            correlation_id=command_id,
        )

        def preflight(uow, envelope):
            current = self._verifications._original(
                uow, expected.execution_id, expected.workspace_id
            )
            if current != expected or not current.observable:
                raise AgentError(
                    "original_verification_scope_changed",
                    "Original verification scope changed",
                )

        CommandService(
            self._uow,
            (ExecutionCommandHandler(lambda: datetime.now(UTC)),),
            preflight=preflight,
        ).handle(command)

    def _heartbeat_fence(self) -> None:
        if self._owner is None:
            raise AgentError("gateway_owner_lost", "Control-plane owner is unavailable")
        self._owner.require_owner()
        self._require_background_identity(self._background_identity)

    def _heartbeat_owned_slots(self) -> None:
        with self._condition:
            if self._starting or self._closing or self._closed:
                return
        self._heartbeat_fence()
        identity = self._require_background_identity(self._background_identity)
        slots = self._slots.owned_slots()
        eligible = tuple(
            worker
            for worker in slots
            if self._heartbeat_after is None or worker.id > self._heartbeat_after
        )
        if not eligible:
            eligible = slots
        now = datetime.now(UTC)
        interval = min(
            self._settings.assignment_lease_renew_interval_seconds,
            self._settings.worker_heartbeat_timeout_seconds / 3,
        )
        bucket = int(now.timestamp() / interval)

        def transaction():
            self._heartbeat_fence()
            return SqlAlchemyUnitOfWork(
                self._database,
                before_commit=self._heartbeat_fence,
                projection_change_retention_seconds=self._settings.projection_change_retention_seconds,
            )

        def require_slot(uow, envelope):
            if not any(
                worker.id == envelope.target_id
                and worker.workspace_id == envelope.workspace_id
                for worker in self._slots._in_uow(uow)
            ):
                raise AgentError(
                    "configuration_mismatch", "Local dispatcher slot is unavailable"
                )

        service = CommandService(
            transaction, (WorkerCommandHandler(lambda: now),), preflight=require_slot
        )
        for worker in eligible[:200]:
            with self._condition:
                if self._closing or self._closed:
                    return
            command_id = uuid5(
                worker.id, f"sidera.native.heartbeat:{worker.version}:{bucket}"
            )
            envelope = CommandEnvelope(
                id=command_id,
                workspace_id=worker.workspace_id,
                command_type="HeartbeatWorker",
                target_type="worker",
                actor_type="system",
                actor_id=identity.principal_id,
                target_id=worker.id,
                payload={"healthy": True},
                expected_version=worker.version,
                idempotency_key=f"native-heartbeat:{worker.id}:{worker.version}:{bucket}",
                correlation_id=command_id,
            )
            try:
                service.handle(envelope)
            except VersionConflict:
                pass
            self._heartbeat_after = worker.id

    def _cancel_unadmitted_worker(self, expected) -> None:
        principal = self._settings.local_actor_id
        with self._uow() as uow:
            execution, _, _, _ = uow.execution_authorities.lock_unadmitted_original(
                expected,
                principal_id=principal,
            )
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera.native.close-cancel:{execution.id}:{expected.start_operation_id}",
        )
        command = CommandEnvelope(
            id=command_id,
            workspace_id=expected.workspace_id,
            command_type="CancelExecution",
            target_type="execution",
            actor_type="system",
            actor_id=principal,
            target_id=execution.id,
            payload={},
            expected_version=execution.version,
            idempotency_key=f"native-close-cancel:{execution.id}:{expected.start_operation_id}",
            correlation_id=command_id,
        )
        handler = ExecutionCommandHandler(lambda: datetime.now(UTC))

        class CancelOriginal:
            command_types = frozenset({"CancelExecution"})

            def handle(self, uow, envelope):
                uow.execution_authorities.lock_unadmitted_original(
                    expected, principal_id=principal
                )
                result = handler.handle(uow, envelope)
                uow.execution_authorities.cancel_unadmitted_original(
                    expected, principal_id=principal
                )
                return result

        def require_original(uow, envelope):
            current = uow.execution_authorities.get_link(
                expected.workspace_id, expected.execution_id
            )
            authority = uow.execution_authorities.get(
                expected.authority_id, workspace_id=expected.workspace_id
            )
            if (
                current is None
                or authority is None
                or authority.principal_id != principal
                or (
                    current.start_operation_id,
                    current.authority_id,
                    current.profile_ref,
                    current.binding_version,
                    current.run_id,
                    current.event_cursor,
                    current.request_sha256,
                    current.start_receipt,
                    current.preparation,
                )
                != (
                    expected.start_operation_id,
                    expected.authority_id,
                    expected.profile_ref,
                    expected.binding_version,
                    None,
                    0,
                    expected.request_sha256,
                    expected.start_receipt,
                    expected.preparation,
                )
            ):
                raise AgentError(
                    "native_recovery_required", "Original Worker intention changed"
                )

        CommandService(
            self._uow, (CancelOriginal(),), preflight=require_original
        ).handle(command)

    def _request_original_worker_stop(self, expected) -> None:
        with self._uow() as uow:
            link = uow.execution_authorities.get_link(
                expected.workspace_id, expected.execution_id
            )
            authority = uow.execution_authorities.get(
                expected.authority_id, workspace_id=expected.workspace_id
            )
            execution = uow.executions.get(expected.execution_id, expected.workspace_id)
            if (
                link is None
                or link != expected
                or authority is None
                or execution is None
                or authority.principal_id != self._settings.local_actor_id
                or execution.stop_requested
                or link.run_id is None
                or execution.active_lease_id != authority.lease_id
                or execution.last_fencing_token != authority.fencing_token
            ):
                return
            session = uow.sessions.get_any(
                uuid5(NAMESPACE_URL, f"sidera.native.session:{link.run_id}"),
                link.workspace_id,
            )
            if (
                session is None
                or session.execution_id != execution.id
                or session.status is not ExecutionSessionStatus.ACTIVE
            ):
                return
        self._execution_authorizer.require(
            link.authority_id, "run.stop", link.workspace_id
        )
        command_id = uuid5(
            NAMESPACE_URL, f"sidera.native.close-stop:{execution.id}:{session.id}"
        )
        command = CommandEnvelope(
            id=command_id,
            workspace_id=link.workspace_id,
            command_type="StopExecution",
            target_type="execution",
            actor_type="system",
            actor_id=self._settings.local_actor_id,
            target_id=execution.id,
            payload={},
            expected_version=execution.version,
            idempotency_key=f"native-close-stop:{execution.id}:{session.id}",
            correlation_id=command_id,
        )

        def require_original(uow, envelope):
            current = uow.execution_authorities.get_link(
                link.workspace_id, link.execution_id
            )
            if current is None or (
                current.start_operation_id,
                current.authority_id,
                current.profile_ref,
                current.binding_version,
                current.run_id,
            ) != (
                link.start_operation_id,
                link.authority_id,
                link.profile_ref,
                link.binding_version,
                link.run_id,
            ):
                raise AgentError(
                    "native_recovery_required", "Original Worker intention changed"
                )

        CommandService(
            self._uow,
            (ExecutionCommandHandler(lambda: datetime.now(UTC)),),
            preflight=require_original,
        ).handle(command)

    def _drain_worker_stops(self) -> None:
        if self._owner is None:
            return
        with self._uow() as uow:
            links = uow.execution_authorities.list_reconciliation_links(None, limit=200)
        with ExitStack() as keepers:
            for link in links:
                if link.run_id is not None:
                    keepers.enter_context(
                        self._owner.gateway_runtime.worker_gateway.original_observation_keeper(
                            link
                        )
                    )
            self._owner.gateway.run_stops_once(self._settings.gateway_batch_size)

    def _ensure_drain(self) -> None:
        with self._condition:
            if self._owner is None or self._drain_started:
                return
            self._drain_started = True
        try:
            self._launch("drain", self._check_drain, 0.01, maintenance=True)
            self._launch(
                "drain-stops", self._drain_worker_stops, 0.01, maintenance=True
            )
        except BaseException:
            with self._condition:
                self._drain_started = False
            raise

    def close(self, *, grace_seconds: float = 30.0) -> bool:
        if (
            isinstance(grace_seconds, bool)
            or not isinstance(grace_seconds, (int, float))
            or not math.isfinite(grace_seconds)
            or grace_seconds < 0
        ):
            raise ValueError("grace_seconds must be finite and nonnegative")
        deadline = time.monotonic() + grace_seconds
        with self._condition:
            while self._disposing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            if self._disposal_complete:
                return True
            self._closing = True
            owner_check = (
                None if self._owner is None else self._owner.gateway_runtime.owner_check
            )
            if self._owner is not None and owner_check is None:
                return False
        self._ensure_drain()
        with self._condition:
            while (
                self._starting
                or self._admitted
                or self._active_work
                or (
                    self._owner is not None
                    and not self._stop.is_set()
                    and not self._drained.is_set()
                )
            ):
                if self._owner is not None and (
                    owner_check is None or not owner_check()
                ):
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._stop.set()
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            return False
        with self._condition:
            # Closing reads can enter while consumer threads are being joined.
            while self._active_work or self._disposing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            if self._disposal_complete:
                return True
            self._closed = True
            self._disposing = True
            if self._disposal_cleanup is None:
                self._disposal_cleanup = [("database", self._database.dispose)]
                if self._owner is not None:
                    self._disposal_cleanup.append(("owner", self._owner.close))
                if self._planning is not None:
                    self._disposal_cleanup.extend(
                        (
                            ("planning_database", self._planning.database.dispose),
                            ("planning_catalog", self._planning.catalog.close),
                        )
                    )
                self._disposal_cleanup.append(
                    (
                        "business",
                        lambda: self._dispose_business(self._business_cleanup),
                    )
                )
        callback = "business"
        try:
            while self._disposal_cleanup:
                callback, dispose = self._disposal_cleanup[-1]
                dispose()
                self._disposal_cleanup.pop()
            with self._condition:
                self._disposal_complete = True
            return True
        except Exception as exc:  # noqa: BLE001 - Retain unfinished cleanup for retry.
            logger.warning(
                "control_plane_disposal_failed callback=%s exception=%s",
                callback,
                type(exc).__name__,
            )
            return False
        finally:
            with self._condition:
                self._disposing = False
                self._condition.notify_all()

    @staticmethod
    def _dispose_business(cleanup: list[Callable[[], None]]) -> None:
        while cleanup:
            cleanup[-1]()
            cleanup.pop()

    def migrate(self) -> None:
        self._check_open()
        migrate_database(self._database)

    def configure_workspace(
        self, request: WorkspaceSetupRequest, *, grant_id: UUID
    ) -> WorkspaceSetupReceipt:
        with self._condition:
            if self._closed or self._closing:
                raise AgentError("control_plane_closed", "Control plane is closed")
            self._active_work += 1
            self._regular_work += 1
        try:
            authorizer = self._request_authorizer
            current = None
            if authorizer is not None and isinstance(grant_id, UUID):
                try:
                    current = authorizer.identity_for_grant(grant_id)
                except Exception:  # noqa: BLE001 - Host identity errors are never exposed.
                    current = None
            if (
                authorizer is None
                or not isinstance(current, ControlPlaneIdentity)
                or current.grant_id != grant_id
                or current.actor_type != "user"
                or self._settings.local_actor_type != "user"
                or current.principal_id != self._settings.local_actor_id
            ):
                raise AgentError(
                    "permission_denied", "Workspace setup authority is unavailable"
                )
            try:
                authorizer.require(grant_id, "workspace.configure", request)
            except Exception:  # noqa: BLE001 - Host scope errors are never exposed.
                raise AgentError(
                    "permission_denied", "Workspace setup authority is unavailable"
                ) from None
            return configure_native_workspace(
                self._uow,
                request,
                identity=current,
                settings=self._settings,
                catalog=self._catalog,
                runtimes=self._runtimes,
            )
        finally:
            with self._condition:
                self._active_work -= 1
                self._regular_work -= 1
                self._drained.clear()
                self._condition.notify_all()

    def get_workspace_setup(
        self, workspace_id: UUID, *, grant_id: UUID
    ) -> WorkspaceSetupView | None:
        with self._condition:
            if self._closed or self._closing:
                raise AgentError("control_plane_closed", "Control plane is closed")
            self._active_work += 1
            self._regular_work += 1
        try:
            authorizer = self._request_authorizer
            current = None
            if authorizer is not None and type(grant_id) is UUID:
                try:
                    current = authorizer.identity_for_grant(grant_id)
                except Exception:  # noqa: BLE001 - Host failures never disclose private state.
                    current = None
            if (
                authorizer is None
                or not isinstance(current, ControlPlaneIdentity)
                or current.grant_id != grant_id
                or current.principal_id != self._settings.local_actor_id
                or type(workspace_id) is not UUID
            ):
                raise AgentError("permission_denied", "Workspace setup read denied")
            try:
                authorizer.require(grant_id, "workspace.setup.read", workspace_id)
            except Exception:  # noqa: BLE001 - Host failures never disclose private state.
                raise AgentError(
                    "permission_denied", "Workspace setup read denied"
                ) from None
            return read_native_workspace_setup(self._uow, workspace_id)
        finally:
            with self._condition:
                self._active_work -= 1
                self._regular_work -= 1
                self._drained.clear()
                self._condition.notify_all()

    def readiness(self) -> bool:
        if self._closed or self._closing or self._startup_failed:
            return False
        if self._owner is not None:
            owner_check = self._owner.gateway_runtime.owner_check
            if owner_check is None or not owner_check():
                return False
        return schema_ready(self._database)

    @contextmanager
    def _assignment_queries(
        self, workspace_id: UUID, grant_id: UUID
    ) -> Iterator[AssignmentQueryService]:
        with self._condition:
            if self._closed or self._closing:
                raise AgentError("control_plane_closed", "Control plane is closed")
            self._active_work += 1
            self._regular_work += 1
        try:
            authorizer = self._request_authorizer
            current = (
                None
                if authorizer is None or type(grant_id) is not UUID
                else authorizer.identity_for_grant(grant_id)
            )
            if (
                authorizer is None
                or not isinstance(current, ControlPlaneIdentity)
                or current.grant_id != grant_id
                or current.actor_type != "user"
                or self._settings.local_actor_type != "user"
                or current.principal_id != self._settings.local_actor_id
            ):
                raise AgentError("permission_denied", "Work read denied.")
            yield AssignmentQueryService(
                store=PostgresAssignmentReadStore(self._database.session),
                execution=self._execution,
                authorizer=authorizer,
                identity=current,
                principal_id=self._settings.local_actor_id,
                workspace_id=workspace_id,
            )
        finally:
            with self._condition:
                self._active_work -= 1
                self._regular_work -= 1
                self._drained.clear()
                self._condition.notify_all()

    def read_assignment(
        self,
        workspace_id: UUID,
        target_type: AssignmentKind,
        target_id: UUID,
        *,
        grant_id: UUID,
    ) -> AgentBindingView | None:
        with self._assignment_queries(workspace_id, grant_id) as queries:
            return queries.read_assignment(workspace_id, target_type, target_id)

    @contextmanager
    def _native_worker_queries(
        self, workspace_id: UUID, grant_id: UUID
    ) -> Iterator[NativeWorkerQueryService]:
        with self._condition:
            if self._closed or self._closing:
                raise AgentError("control_plane_closed", "Control plane is closed")
            self._active_work += 1
            self._regular_work += 1
            self._drained.clear()
        try:
            authorizer = self._request_authorizer
            current = (
                None
                if authorizer is None or type(grant_id) is not UUID
                else authorizer.identity_for_grant(grant_id)
            )
            if (
                authorizer is None
                or not isinstance(current, ControlPlaneIdentity)
                or current.grant_id != grant_id
                or current.actor_type != "user"
                or self._settings.local_actor_type != "user"
                or current.principal_id != self._settings.local_actor_id
            ):
                raise AgentError("permission_denied", "Native worker read denied.")
            yield NativeWorkerQueryService(
                workspace_store=SqlAlchemyWorkerWorkspaceQueryStore(self._database),
                assignment_store=PostgresAssignmentReadStore(self._database.session),
                execution=self._execution,
                observations=self._observations,
                input_authorizer=self._execution_authorizer,
                authorizer=authorizer,
                identity=current,
                principal_id=self._settings.local_actor_id,
                workspace_id=workspace_id,
            )
        finally:
            with self._condition:
                self._active_work -= 1
                self._regular_work -= 1
                self._drained.clear()
                self._condition.notify_all()

    def read_native_worker_context(
        self, workspace_id: UUID, worker_item_id: UUID, *, grant_id: UUID
    ) -> NativeWorkerContextView:
        with self._native_worker_queries(workspace_id, grant_id) as queries:
            return queries.read_context(workspace_id, worker_item_id)

    def read_native_worker_trace(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
        execution_id: UUID,
        *,
        grant_id: UUID,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> NativeWorkerTraceView:
        with self._native_worker_queries(workspace_id, grant_id) as queries:
            return queries.read_trace(
                workspace_id,
                worker_item_id,
                execution_id,
                after_sequence=after_sequence,
                limit=limit,
            )

    def read_agent_execution(
        self, workspace_id: UUID, execution_id: UUID, *, grant_id: UUID
    ) -> AgentExecutionView:
        with self._assignment_queries(workspace_id, grant_id) as queries:
            return queries.read_agent_execution(workspace_id, execution_id)

    def list_profile_work(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        *,
        grant_id: UUID,
        after: str | None = None,
        limit: int = 50,
    ) -> ProfileWorkPage:
        with self._assignment_queries(workspace_id, grant_id) as queries:
            return queries.list_profile_work(
                workspace_id, profile_id, after=after, limit=limit
            )

    def read_profile_work_run(
        self,
        workspace_id: UUID,
        kind: ProfileWorkKind,
        work_id: UUID,
        *,
        grant_id: UUID,
        target_type: AssignmentKind | None = None,
    ) -> NativeRunSummary:
        with self._assignment_queries(workspace_id, grant_id) as queries:
            return queries.read_profile_work_run(
                workspace_id, kind, work_id, target_type=target_type
            )

    def read_task_tool_scope(
        self, *, grant_id: UUID, connection_id: UUID
    ) -> ControlPlaneToolScope:
        with self._condition:
            if self._closed or self._closing:
                raise AgentError("control_plane_closed", "Control plane is closed")
            self._active_work += 1
            self._regular_work += 1
        try:
            return read_task_tool_scope(
                self._uow,
                catalog=self._catalog,
                authorizer=self._execution_authorizer,
                grant_id=grant_id,
                connection_id=connection_id,
            )
        finally:
            with self._condition:
                self._active_work -= 1
                self._regular_work -= 1
                self._drained.clear()
                self._condition.notify_all()


def create_control_plane_runtime(
    settings: Settings,
    agent_execution: AgentExecution,
    agent_catalog: AgentCatalog,
    agent_observations: AgentObservations,
    source_reader: ContextSourceReader,
    request_authorizer: ControlPlaneRequestAuthorizer | None = None,
    *,
    agent_runtimes: AgentRuntimes | None = None,
) -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        settings,
        agent_execution,
        agent_catalog,
        agent_observations,
        source_reader,
        request_authorizer,
        agent_runtimes,
    )
