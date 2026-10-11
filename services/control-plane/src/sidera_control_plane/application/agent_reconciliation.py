"""Bounded observation of the original public native operations owned by CP."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_agents.errors import AgentError

from sidera_control_plane.application.agent_worker_adapter import (
    NativeWorkerServices,
    project_run_event,
    read_native_preparation_receipt,
    request_fingerprint,
    require_gateway_owner,
)
from sidera_control_plane.application.event_ingestor import SessionEventIngestor
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
    PlanningAuthorityReader,
)
from sidera_control_plane.application.planning_runner import PlanningRunner
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_gateway import WorkerGateway
from sidera_control_plane.domain.agent_bindings import (
    AgentExecutionLink,
    AssignmentDenied,
)
from sidera_control_plane.domain.errors import LeaseExpired, StaleFencingToken
from sidera_control_plane.domain.execution import ExecutionStatus
from sidera_control_plane.domain.planning_authority import PlanningAgentLink


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    execution_id: UUID
    start_operation_id: UUID
    run_id: UUID | None
    code: str
    observed: int = 0
    recovery_required: bool = False


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    examined: int
    observed: int
    recovery_required: int
    outcomes: tuple[ReconcileOutcome, ...]


class AgentRunReconciler:
    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        gateway: WorkerGateway | None,
        services: NativeWorkerServices,
        planning_runner: PlanningRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        owner_check: Callable[[], bool] | None = None,
    ):
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._services = services
        self._planning_runner = planning_runner
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_check = owner_check
        self._authorizer = ExecutionAuthorityReader(
            uow_factory,
            principal=lambda: services.principal_id,
            authorizer=services.authorizer,
            clock=self._clock,
        )
        self._ingestor = SessionEventIngestor(uow_factory, clock=self._clock)
        self._planning_authorizer = PlanningAuthorityReader(
            uow_factory,
            principal=lambda: services.principal_id,
            authorizer=services.authorizer,
            clock=self._clock,
        )
        self._scan_after: dict[tuple[UUID | None, str], UUID] = {}
        self._planner_first: dict[UUID | None, bool] = {}

    def run_once(self, workspace_id: UUID | None, limit: int = 100) -> ReconcileSummary:
        if (
            (workspace_id is not None and type(workspace_id) is not UUID)
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            raise ValueError(
                "native reconciliation requires a workspace and bounded positive limit"
            )
        with self._uow_factory() as uow:
            require_gateway_owner(self._owner_check)
            links: tuple[AgentExecutionLink, ...] = ()
            planning_links: tuple[PlanningAgentLink, ...] = ()
            planner_first = self._planner_first.get(workspace_id, False)
            self._planner_first[workspace_id] = not planner_first
            remaining = limit
            for family in (
                ("planner", "worker") if planner_first else ("worker", "planner")
            ):
                if (
                    remaining == 0
                    or (family == "worker" and self._gateway is None)
                    or (family == "planner" and self._planning_runner is None)
                ):
                    continue
                key = (workspace_id, family)
                after = self._scan_after.get(key)
                if family == "planner":
                    planning_links = uow.planning_authorities.list_reconciliation_links(
                        workspace_id, limit=remaining, after_planning_run_id=after
                    )
                    if not planning_links and after is not None:
                        self._scan_after.pop(key, None)
                        planning_links = (
                            uow.planning_authorities.list_reconciliation_links(
                                workspace_id, limit=remaining
                            )
                        )
                    if planning_links:
                        self._scan_after[key] = planning_links[-1].planning_run_id
                    remaining -= len(planning_links)
                else:
                    links = uow.execution_authorities.list_reconciliation_links(
                        workspace_id, limit=remaining, after_execution_id=after
                    )
                    if not links and after is not None:
                        self._scan_after.pop(key, None)
                        links = uow.execution_authorities.list_reconciliation_links(
                            workspace_id, limit=remaining
                        )
                    if links:
                        self._scan_after[key] = links[-1].execution_id
                    remaining -= len(links)
        outcomes = []
        for link in links:
            try:
                require_gateway_owner(self._owner_check)
                outcome = self._reconcile_worker(link)
            except Exception as exc:  # noqa: BLE001 - External/write uncertainty retains the original action.
                code = getattr(exc, "code", None)
                outcome = self._outcome(
                    link,
                    code
                    if isinstance(code, str)
                    and code
                    in {
                        "permission_denied",
                        "configuration_mismatch",
                        "grant_revoked",
                        "recovery_unsupported",
                        "gateway_owner_lost",
                        "native_recovery_required",
                    }
                    else "native_query_unconfirmed",
                    recovery_required=True,
                )
            outcomes.append(outcome)
            if outcome.code == "gateway_owner_lost":
                break
        for planning_link in planning_links:
            if outcomes and outcomes[-1].code == "gateway_owner_lost":
                break
            try:
                require_gateway_owner(self._owner_check)
                if self._planning_runner is None:
                    raise AssignmentDenied("planning reconciliation owner unavailable")
                self._require_planning_original(planning_link)
                if planning_link.status == "cleanup_pending":
                    complete = self._planning_runner.drain_original_run(planning_link)
                    if not complete:
                        self._observe_planning_original(planning_link)
                    outcomes.append(
                        ReconcileOutcome(
                            planning_link.planning_run_id,
                            planning_link.start_operation_id,
                            planning_link.run_id,
                            "native_planning_cleanup_complete"
                            if complete
                            else "native_planning_recovery_required",
                            observed=int(complete),
                            recovery_required=not complete,
                        )
                    )
                    continue
                result = self._planning_runner.reconcile_original_run(planning_link)
                applied = result is not None and result.status != "stale"
                if not applied:
                    self._observe_planning_original(planning_link)
                outcome = ReconcileOutcome(
                    planning_link.planning_run_id,
                    planning_link.start_operation_id,
                    planning_link.run_id,
                    "native_planning_result_recovered"
                    if applied
                    else "native_planning_recovery_required",
                    observed=int(applied),
                    recovery_required=not applied,
                )
            except Exception as exc:  # noqa: BLE001 - The saved original result remains queryable.
                code = getattr(exc, "code", None)
                outcome = ReconcileOutcome(
                    planning_link.planning_run_id,
                    planning_link.start_operation_id,
                    planning_link.run_id,
                    code
                    if isinstance(code, str)
                    and code
                    in {
                        "gateway_owner_lost",
                        "permission_denied",
                        "configuration_mismatch",
                    }
                    else "native_planning_recovery_required",
                    recovery_required=True,
                )
            outcomes.append(outcome)
        return ReconcileSummary(
            len(outcomes),
            sum(item.observed for item in outcomes),
            sum(item.recovery_required for item in outcomes),
            tuple(outcomes),
        )

    def _require_original(self, expected: AgentExecutionLink):
        require_gateway_owner(self._owner_check)
        with self._uow_factory() as uow:
            saved = uow.execution_authorities.get_link(
                expected.workspace_id, expected.execution_id
            )
            if saved != expected:
                raise AssignmentDenied("original reconciliation link changed")
            authority = uow.execution_authorities.get(
                expected.authority_id, workspace_id=expected.workspace_id
            )
            if (
                authority is None
                or authority.principal_id != self._services.principal_id
            ):
                raise AssignmentDenied("original reconciliation authority unavailable")
            read_native_preparation_receipt(uow, expected)
        self._authorizer.require(
            expected.authority_id, "run.read", expected.workspace_id
        )
        return authority

    def _require_planning_original(self, expected: PlanningAgentLink):
        require_gateway_owner(self._owner_check)
        with self._uow_factory() as uow:
            saved = uow.planning_authorities.get_link(
                expected.workspace_id, expected.planning_run_id
            )
            authority = uow.planning_authorities.get(
                expected.authority_id, workspace_id=expected.workspace_id
            )
            start = expected.start_request
            if (
                saved != expected
                or authority is None
                or authority.principal_id != self._services.principal_id
                or (
                    authority.workspace_id,
                    authority.planning_run_id,
                    authority.operation_id,
                    authority.profile_ref,
                    authority.execution_workspace,
                )
                != (
                    expected.workspace_id,
                    expected.planning_run_id,
                    expected.start_operation_id,
                    expected.profile_ref,
                    start.workspace,
                )
                or (
                    start.operation_id,
                    start.grant_id,
                    start.profile_ref,
                    start.policy.value,
                )
                != (
                    expected.start_operation_id,
                    expected.authority_id,
                    expected.profile_ref,
                    "bounded",
                )
                or request_fingerprint(start) != expected.request_sha256
            ):
                raise AgentError(
                    "permission_denied", "Original planning intention changed."
                )
        self._planning_authorizer.require(
            expected.authority_id, "run.read", expected.workspace_id
        )
        return authority

    def _observe_planning_original(self, link: PlanningAgentLink) -> None:
        authority = self._require_planning_original(link)
        run_id = link.run_id
        if run_id is None:
            lookup = self._services.execution.get_start(
                link.workspace_id, link.start_operation_id, grant_id=link.authority_id
            )
            require_gateway_owner(self._owner_check)
            self._require_planning_original(link)
            if not lookup.found:
                return
            receipt = lookup.receipt
            if receipt is None or receipt.operation_id != link.start_operation_id:
                raise AgentError(
                    "configuration_mismatch", "Original planning admission changed."
                )
            if receipt.status.value != "accepted":
                return
            run_id = receipt.run_id
        if run_id is None:
            return
        require_gateway_owner(self._owner_check)
        snapshot = self._services.execution.get_run(
            link.workspace_id, run_id, grant_id=link.authority_id
        )
        require_gateway_owner(self._owner_check)
        if (
            snapshot.workspace_id,
            snapshot.run_id,
            snapshot.profile_ref,
            snapshot.policy.value,
            snapshot.effective_config.profile_ref,
            snapshot.effective_config.runtime_ref,
        ) != (
            link.workspace_id,
            run_id,
            link.profile_ref,
            "bounded",
            link.profile_ref,
            authority.runtime_ref,
        ):
            raise AgentError(
                "configuration_mismatch", "Original planning Run identity changed."
            )
        self._require_planning_original(link)

    def _reconcile_worker(self, link: AgentExecutionLink) -> ReconcileOutcome:
        if self._gateway is None:
            return self._outcome(
                link, "native_recovery_required", recovery_required=True
            )
        with self._gateway.original_observation_keeper(link):
            return self._observe_worker(link)

    def _observe_worker(self, link: AgentExecutionLink) -> ReconcileOutcome:
        if self._gateway is None:
            raise AssignmentDenied("Worker observation owner unavailable")
        authority = self._require_original(link)
        if link.known_unadmitted:
            return self._outcome(
                link,
                "native_admission_pending"
                if link.status == "pending"
                else "native_capacity_wait"
                if link.not_admitted
                else "native_admission_rejected",
                recovery_required=link.status == "failed",
            )
        if link.run_id is None:
            lookup = self._services.execution.get_start(
                link.workspace_id, link.start_operation_id, grant_id=link.authority_id
            )
            require_gateway_owner(self._owner_check)
            if not lookup.found:
                return self._outcome(
                    link, "native_admission_not_found", recovery_required=True
                )
            receipt = lookup.receipt
            if receipt is None or receipt.operation_id != link.start_operation_id:
                return self._outcome(
                    link, "native_admission_identity_changed", recovery_required=True
                )
            if receipt.status.value == "unknown":
                return self._outcome(
                    link, "native_admission_unknown", recovery_required=True
                )
            self._require_original(link)
            try:
                with self._uow_factory() as uow:
                    uow.execution_authorities.lock_intention(link, now=self._clock())
                    link = uow.execution_authorities.record_start(
                        link, receipt, now=self._clock()
                    )
            except (AssignmentDenied, LeaseExpired, StaleFencingToken):
                return self._outcome(
                    link, "native_recovery_required", recovery_required=True
                )
            if link.run_id is None:
                return self._outcome(
                    link,
                    "native_capacity_wait"
                    if link.not_admitted
                    else "native_admission_rejected",
                    recovery_required=not link.not_admitted,
                )
            self._gateway.reconcile_native_intention(link)
            with self._uow_factory() as uow:
                current = uow.execution_authorities.get_link(
                    link.workspace_id, link.execution_id
                )
            if current is None:
                raise AssignmentDenied("original reconciliation link unavailable")
            return self._outcome(
                current,
                "native_original_observed",
                observed=current.event_cursor - link.event_cursor,
            )

        with self._uow_factory() as uow:
            session = uow.sessions.get_any(
                uuid5(NAMESPACE_URL, f"sidera.native.session:{link.run_id}"),
                link.workspace_id,
            )
        if session is None:
            self._gateway.reconcile_native_intention(link)
            with self._uow_factory() as uow:
                current = uow.execution_authorities.get_link(
                    link.workspace_id, link.execution_id
                )
            if current is None:
                raise AssignmentDenied("original reconciliation link unavailable")
            return self._outcome(
                current,
                "native_original_observed",
                observed=current.event_cursor - link.event_cursor,
            )

        snapshot = self._services.execution.get_run(
            link.workspace_id, link.run_id, grant_id=link.authority_id
        )
        require_gateway_owner(self._owner_check)
        if (
            snapshot.workspace_id,
            snapshot.run_id,
            snapshot.profile_ref,
            snapshot.policy.value,
            snapshot.effective_config.profile_ref,
            snapshot.effective_config.runtime_ref,
        ) != (
            link.workspace_id,
            link.run_id,
            link.profile_ref,
            "bounded",
            link.profile_ref,
            authority.runtime_ref,
        ):
            raise AgentError(
                "configuration_mismatch", "Original native Run identity changed."
            )
        self._require_original(link)
        page = self._services.execution.list_events(
            link.workspace_id,
            link.run_id,
            link.event_cursor,
            limit=100,
            grant_id=link.authority_id,
        )
        require_gateway_owner(self._owner_check)
        if any(
            event.run_id != link.run_id for event in page.events
        ) or page.next_sequence != (
            page.events[-1].sequence if page.events else link.event_cursor
        ):
            raise AgentError(
                "configuration_mismatch", "Original native event page identity changed."
            )
        report = self._ingestor.ingest_original_page(
            expected=link,
            principal_id=self._services.principal_id,
            events=tuple(project_run_event(event) for event in page.events),
        )
        with self._uow_factory() as uow:
            current = uow.execution_authorities.get_link(
                link.workspace_id, link.execution_id
            )
            if current is None:
                raise AssignmentDenied("original reconciliation link unavailable")
            authority = uow.execution_authorities.get(
                link.authority_id, workspace_id=link.workspace_id
            )
            if authority is None:
                raise AssignmentDenied("original reconciliation authority unavailable")
            execution = uow.executions.get(link.execution_id, link.workspace_id)
            current_session = uow.sessions.get_any(session.id, link.workspace_id)
            lease = uow.leases.get(authority.lease_id, link.workspace_id)
            binding = uow.agent_bindings.get(
                link.workspace_id, authority.target_type, authority.target_id
            )
            current_writer = (
                execution is not None
                and current_session is not None
                and lease is not None
                and binding is not None
                and not authority.revoked
                and execution.status
                in {ExecutionStatus.LEASED, ExecutionStatus.RUNNING}
                and current_session.status.value == "active"
                and binding.version == link.binding_version
                and binding.profile_ref == link.profile_ref
                and execution.active_lease_id == authority.lease_id
                and execution.last_fencing_token == authority.fencing_token
                and lease.status.value == "active"
                and lease.expires_at > self._clock()
                and lease.fencing_token == authority.fencing_token
            )
        if current_writer:
            self._gateway.finish_original_observation(current, report)
        elif current_session is not None and current_session.status.value == "active":
            return self._outcome(
                current,
                "native_recovery_required",
                observed=len(report.events),
                recovery_required=True,
            )
        if snapshot.state.value in {"unknown", "closing"} and current.status not in {
            "succeeded",
            "failed",
            "stopped",
        }:
            return self._outcome(
                current,
                "recovery_unsupported"
                if snapshot.reason_code == "recovery_unsupported"
                else "native_run_unconfirmed",
                observed=len(report.events),
                recovery_required=True,
            )
        return self._outcome(
            current, "native_original_observed", observed=len(report.events)
        )

    @staticmethod
    def _outcome(
        link: AgentExecutionLink,
        code: str,
        *,
        observed: int = 0,
        recovery_required: bool = False,
    ) -> ReconcileOutcome:
        return ReconcileOutcome(
            link.execution_id,
            link.start_operation_id,
            link.run_id,
            code,
            observed,
            recovery_required,
        )
