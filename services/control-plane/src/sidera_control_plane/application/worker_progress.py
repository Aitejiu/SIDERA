"""Append authorized self-reports without advancing execution lifecycle."""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue
from pydantic import ValidationError as PayloadValidationError
from sidera_agents.contracts import RunSnapshot, RunState, StartLookup, StartStatus
from sidera_agents.errors import AgentError
from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.application.agent_worker_adapter import NativeWorkerServices
from sidera_control_plane.application.assignments import (
    TrustedAssignmentIdentity,
    require_trusted_assignment_identity,
)
from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.task_tool_scope import read_task_tool_scope
from sidera_control_plane.domain.agent_bindings import AssignmentDenied
from sidera_control_plane.domain.agent_tools import (
    ControlPlaneToolScope,
    WorkerProgressPayload,
    WorkerProgressTarget,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution


@dataclass(frozen=True, slots=True)
class CurrentProgress:
    execution: Execution
    scope: ControlPlaneToolScope
    payload: WorkerProgressPayload


class WorkerProgressCommandHandler:
    command_types = frozenset({"ReportWorkerProgress"})

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        identity: TrustedAssignmentIdentity | None = None,
        authorizer: ExecutionAuthorizer | None = None,
        native_services: NativeWorkerServices | None = None,
    ) -> None:
        self._clock, self._identity = clock, identity
        self._authorizer, self._services = authorizer, native_services

    def preflight(self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope) -> None:
        if envelope.command_type in self.command_types:
            self._current(uow, envelope)

    def _current(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CurrentProgress:
        identity, host, services = self._identity, self._authorizer, self._services
        if (
            identity is None
            or host is None
            or services is None
            or identity.actor_type != "agent"
        ):
            raise AssignmentDenied("Trusted native progress authority required")
        require_trusted_assignment_identity(envelope, identity)
        if services.principal_id != identity.principal_id:
            raise AssignmentDenied("Native progress principal changed")
        if (
            envelope.command_type not in self.command_types
            or envelope.target_type != "execution"
            or envelope.target_id is None
            or type(envelope.expected_version) is not int
            or envelope.expected_version < 1
        ):
            raise ValidationError(
                "Progress requires an exact execution target and version"
            )
        try:
            payload = WorkerProgressPayload.model_validate(envelope.payload)
        except PayloadValidationError:
            raise ValidationError("Invalid progress report") from None
        target = WorkerProgressTarget(
            envelope.workspace_id,
            identity.principal_id,
            envelope.target_id,
            payload.worker_item_id,
            payload.authority_id,
            payload.connection_id,
            envelope.expected_version,
            payload.binding_version,
            payload.lease_id,
            payload.fencing_token,
            payload.run_id,
            payload.response_id,
        )
        try:
            host.require(identity.grant_id, "progress.report", target)
            link = uow.execution_authorities.get_link(
                envelope.workspace_id, envelope.target_id
            )
            if link is None or link.authority_id != payload.authority_id:
                raise AssignmentDenied("Original progress authority unavailable")
            now = self._clock()
            uow.execution_authorities.lock_intention(
                link, now=now, allow_observation_advance=True
            )
            reader = ExecutionAuthorityReader(
                lambda: nullcontext(uow),
                principal=lambda: identity.principal_id,
                authorizer=services.authorizer,
                clock=self._clock,
            )
            scope = read_task_tool_scope(
                lambda: nullcontext(uow),
                catalog=services.catalog,
                authorizer=reader,
                grant_id=payload.authority_id,
                connection_id=payload.connection_id,
            )
            if (
                scope.role != "worker"
                or scope.workspace_id != envelope.workspace_id
                or scope.principal_id != identity.principal_id
                or scope.execution_id != envelope.target_id
                or scope.worker_item_id != payload.worker_item_id
                or scope.native_grant_id != payload.authority_id
                or scope.binding_version != payload.binding_version
                or scope.lease_id != payload.lease_id
                or scope.fencing_token != payload.fencing_token
                or scope.root_task_id is None
            ):
                raise AssignmentDenied("Original progress scope changed")
            actual_version = scope.execution_version
            if type(actual_version) is not int or actual_version < 1:
                raise AssignmentDenied("Current progress execution version unavailable")
            if actual_version != envelope.expected_version:
                raise VersionConflict(envelope.expected_version, actual_version)
            execution = uow.executions.get(envelope.target_id, envelope.workspace_id)
            if execution is None or execution.version != envelope.expected_version:
                raise AssignmentDenied("Current progress execution unavailable")
            reader.require(scope.native_grant_id, "run.read", scope.workspace_id)
            lookup = services.execution.get_start(
                scope.workspace_id,
                scope.start_operation_id,
                grant_id=scope.native_grant_id,
            )
            if (
                not isinstance(lookup, StartLookup)
                or not lookup.found
                or lookup.receipt is None
                or lookup.receipt.operation_id != scope.start_operation_id
                or lookup.receipt.status is not StartStatus.ACCEPTED
                or lookup.receipt.run_id != payload.run_id
                or (scope.run_id is not None and scope.run_id != lookup.receipt.run_id)
            ):
                raise AssignmentDenied("Original native progress admission unavailable")
            run = services.execution.get_run(
                scope.workspace_id, payload.run_id, grant_id=scope.native_grant_id
            )
            if (
                not isinstance(run, RunSnapshot)
                or run.workspace_id != scope.workspace_id
                or run.run_id != payload.run_id
                or run.profile_ref != scope.profile_ref
                or run.state is not RunState.RUNNING
                or run.active_response_id != payload.response_id
            ):
                raise AssignmentDenied(
                    "Original native progress response is not current"
                )
            reader.require(scope.native_grant_id, "profile.read", scope.profile_ref)
            host.require(identity.grant_id, "progress.report", target)
        except AgentError:
            raise AssignmentDenied("Current progress authority denied") from None
        return CurrentProgress(execution, scope, payload)

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        current = self._current(uow, envelope)
        scope, report = current.scope, current.payload
        report_id = uuid4()
        payload: dict[str, JsonValue] = {
            "report_id": str(report_id),
            "execution_id": str(current.execution.id),
            "execution_version": current.execution.version,
            "worker_item_id": str(scope.worker_item_id),
            "root_task_id": str(scope.root_task_id),
            "binding_version": scope.binding_version,
            "run_id": str(report.run_id),
            "response_id": str(report.response_id),
            "summary": report.summary,
            "steps": [dict(step) for step in report.steps],
            "reported_at": self._clock().astimezone(UTC).isoformat(),
            "verified": False,
        }
        event = EventData(
            id=report_id,
            workspace_id=envelope.workspace_id,
            aggregate_type="worker_progress_report",
            aggregate_id=report_id,
            aggregate_version=1,
            event_type="WorkerProgressReported",
            payload=payload,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=current.execution.id,
            aggregate_version=current.execution.version,
            event_ids=(report_id,),
            data={"report_id": str(report_id), "status": "reported", "verified": False},
        )
