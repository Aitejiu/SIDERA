from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RuntimeRef,
    SecretBinding,
    SourceRef,
    StartStatus,
    ToolBinding,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    HandoffOperationGate,
)
from sidera_control_plane.application.plan_transition_gate import (
    PlanTransitionGate,
    PlanTransitionGateOperation,
)
from sidera_control_plane.domain.errors import DomainError


def _deny() -> NoReturn:
    raise AgentError("permission_denied", "Execution authority denied.")


class ExecutionAuthorityReader:
    def __init__(self, uow_factory, *, principal, authorizer, clock=None):
        self._uow_factory = uow_factory
        self._principal = principal
        self._authorizer = authorizer
        self._clock = clock or (lambda: datetime.now(UTC))

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        self._require(grant_id, action, target, original_input_command=False)

    def require_input_command_scope(self, grant_id: UUID, workspace_id: UUID) -> None:
        """Authorize original input receipts without permitting fresh admission."""
        self._require(grant_id, "run.input", workspace_id, original_input_command=True)

    def _require(self, grant_id, action, target, *, original_input_command):
        principal = self._principal()
        if not principal or not isinstance(grant_id, UUID):
            _deny()
        with self._uow_factory() as uow:
            authority = uow.execution_authorities.get(grant_id)
            if authority is None or principal != authority.principal_id:
                _deny()
            execution = uow.executions.get(
                authority.execution_id, authority.workspace_id
            )
            link = uow.execution_authorities.get_link(
                authority.workspace_id, authority.execution_id
            )
            if (
                execution is None
                or link is None
                or link.authority_id != grant_id
                or link.profile_ref != authority.profile_ref
                or link.binding_version != authority.binding_version
                or (
                    execution.worker_item_id
                    if authority.target_type == "worker_item"
                    else execution.task_id
                )
                != authority.target_id
            ):
                _deny()
            historical = next(
                (
                    binding
                    for binding in uow.agent_bindings.history(
                        authority.workspace_id,
                        authority.target_type,
                        authority.target_id,
                    )
                    if binding.version == authority.binding_version
                ),
                None,
            )
            if historical is None or historical.profile_ref != authority.profile_ref:
                _deny()
            # Public reads are workspace-coarse: CP callers must first verify their
            # exact saved start operation / Run link, including during reconciliation.
            if action in {"run.stop", "run.read"}:
                if type(target) is not UUID or target != authority.workspace_id:
                    _deny()
                if action == "run.read" and action not in authority.actions:
                    _deny()
                return
            if original_input_command and (
                type(target) is not UUID or target != authority.workspace_id
            ):
                _deny()
            if (
                not original_input_command
                and action == "run.input"
                and execution.worker_item_id is not None
            ):
                handoff = HandoffOperationGate(uow).evaluate(
                    worker_item_id=execution.worker_item_id,
                    workspace_id=authority.workspace_id,
                    operation=HandoffGateOperation.USER_INPUT,
                )
                plan = PlanTransitionGate(uow).evaluate(
                    worker_item_id=execution.worker_item_id,
                    workspace_id=authority.workspace_id,
                    plan_generation=None,
                    operation=PlanTransitionGateOperation.INPUT,
                )
                if not handoff.allowed or not plan.allowed:
                    _deny()
            if (
                authority.revoked
                or action not in authority.actions
                or self._authorizer is None
                or not isinstance(authority.upstream_grant_id, UUID)
                or authority.upstream_grant_id == authority.id
            ):
                _deny()
            try:
                self._authorizer.require(
                    authority.upstream_grant_id,
                    action,
                    AssignmentTarget(
                        authority.workspace_id,
                        authority.target_type,
                        authority.target_id,
                        principal,
                    ),
                )
            except (DomainError, AgentError):
                _deny()
            if original_input_command:
                return
            binding = uow.agent_bindings.get(
                authority.workspace_id, authority.target_type, authority.target_id
            )
            if (
                binding is None
                or binding.version != authority.binding_version
                or binding.profile_ref != authority.profile_ref
                or (
                    not binding.runnable
                    and action not in {"runtime.read", "profile.read"}
                )
                or execution.stop_requested
                or (
                    execution.handoff_transition_id is not None
                    and not self._native_handoff_scope(
                        uow, execution, binding, link, action=action
                    )
                )
                or execution.status.value not in {"leased", "running"}
            ):
                _deny()
            lease = uow.leases.get(authority.lease_id, authority.workspace_id)
            if (
                lease is None
                or lease.execution_id != authority.execution_id
                or lease.status.value != "active"
                or lease.expires_at <= self._clock()
                or lease.fencing_token != authority.fencing_token
                or execution.active_lease_id != lease.id
                or execution.last_fencing_token != authority.fencing_token
            ):
                _deny()
            manifest = uow.workspaces.get(
                authority.workspace_ref, authority.workspace_id
            )
            if (
                manifest is None
                or manifest.execution_id != authority.execution_id
                or manifest.fencing_token != authority.fencing_token
                or manifest.lifecycle.value not in {"ready", "active"}
                or manifest.location_ref
                != uow.execution_authorities.prepared_location_ref(
                    grant_id, authority.workspace_id
                )
            ):
                _deny()
            if action in {"workspace.execute", "artifact.read"}:
                if (
                    not isinstance(target, ExecutionWorkspace)
                    or target != authority.execution_workspace
                ):
                    _deny()
                if action == "artifact.read" and target.artifact_directory is None:
                    _deny()
            elif action == "runtime.read":
                if (
                    not isinstance(target, RuntimeRef)
                    or target != authority.runtime_ref
                ):
                    _deny()
            elif action in {"run.start", "profile.read"}:
                if (
                    not isinstance(target, ProfileRef)
                    or target != authority.profile_ref
                ):
                    _deny()
                if action == "run.start" and (
                    link.status not in {"pending", "starting"}
                    or link.run_id is not None
                ):
                    _deny()
            elif action in {"run.input", "run.recover", "run.interrupt"}:
                if (
                    type(target) is not UUID
                    or target != authority.workspace_id
                    or link.run_id is None
                ):
                    _deny()
                if action == "run.input":
                    session = uow.sessions.get_any(
                        uuid5(NAMESPACE_URL, f"sidera.native.session:{link.run_id}"),
                        authority.workspace_id,
                    )
                    if (
                        link.status not in {"starting", "running", "waiting"}
                        or session is None
                        or session.status.value != "active"
                        or session.execution_id != authority.execution_id
                        or session.adapter_session_id != f"agent-run:{link.run_id}"
                    ):
                        _deny()
                elif action == "run.interrupt" and link.status not in {
                    "running",
                    "waiting",
                }:
                    _deny()
            elif action == "tool.execute":
                if not isinstance(target, ToolBinding) or target not in authority.tools:
                    _deny()
            elif action == "source.read":
                if not isinstance(target, SourceRef) or target not in authority.sources:
                    _deny()
            elif action == "secret.read":
                ref = (
                    target.credential_ref
                    if isinstance(target, SecretBinding)
                    else target
                )
                if (
                    not isinstance(ref, CredentialRef)
                    or ref not in authority.credentials
                ):
                    _deny()
            elif action == "resource.read":
                if (
                    not isinstance(target, ResourceRef)
                    or target not in authority.resources
                ):
                    _deny()
            else:
                _deny()

    def _native_handoff_scope(self, uow, execution, binding, link, *, action):
        from sidera_control_plane.application.workspace_handoff import (
            WorkspaceHandoffService,
        )
        from sidera_control_plane.domain.handoffs import (
            HandoffPhase,
            HandoffTransitionStatus,
        )

        transition = uow.handoff_transitions.get(
            execution.handoff_transition_id, execution.workspace_id
        )
        if (
            transition is None
            or transition.handoff_schema_version != 2
            or transition.native_payload is None
            or binding is None
            or (
                transition.target_execution_id != execution.id
                or transition.worker_item_id != execution.worker_item_id
                or transition.target_worker_id != execution.assigned_worker_id
                or transition.target_binding_version != binding.version
                or transition.native_payload.target_profile_ref != binding.profile_ref
                or transition.native_payload.target_profile_ref != link.profile_ref
                or transition.target_binding_version != link.binding_version
            )
        ):
            return False
        if transition.status is HandoffTransitionStatus.CONVERGED:
            session = uow.sessions.get_any(
                transition.target_session_id, execution.workspace_id
            )
            return (
                session is not None
                and session.execution_id == execution.id
                and session.adapter_session_id == f"agent-run:{link.run_id}"
            )
        operation = {
            HandoffPhase.START_TARGET_SESSION: HandoffGateOperation.COORDINATOR_START_TARGET,
            HandoffPhase.ACKNOWLEDGE_CONTEXT: HandoffGateOperation.COORDINATOR_ENABLE_TARGET_WRITER,
        }.get(transition.phase)
        if operation is None:
            return False
        if (
            transition.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT
            and action != "source.read"
        ):
            return False
        gate = HandoffOperationGate(uow).evaluate(
            worker_item_id=execution.worker_item_id,
            workspace_id=execution.workspace_id,
            operation=operation,
            transition_id=transition.id,
            target_execution_id=execution.id,
        )
        if not gate.allowed or transition.source_workspace_freeze_facts is None:
            return False
        claimed = (
            bool(transition.lease_owner)
            and transition.lease_expires_at is not None
            and transition.lease_expires_at > self._clock()
        )
        if not claimed:
            # Admission outlives the short dispatch claim. Only the exact saved
            # Run and active Session may finish projecting under its live Lease.
            # Task-tool initialization also reads the pinned Profile metadata.
            receipt = link.start_receipt
            if (
                transition.phase is not HandoffPhase.START_TARGET_SESSION
                or action not in {
                    "workspace.execute", "source.read", "tool.execute",
                    "secret.read", "resource.read", "profile.read",
                }
                or not isinstance(link.run_id, UUID)
                or link.status not in {"starting", "running", "waiting"}
                or receipt is None
                or receipt.status is not StartStatus.ACCEPTED
                or receipt.operation_id != link.start_operation_id
                or receipt.run_id != link.run_id
            ):
                return False
            session_id = uuid5(NAMESPACE_URL, f"sidera.native.session:{link.run_id}")
            session = uow.sessions.get_any(session_id, execution.workspace_id)
            if (
                session is None
                or session.id != session_id
                or session.execution_id != execution.id
                or session.status.value != "active"
                or session.adapter_session_id != f"agent-run:{link.run_id}"
                or transition.target_session_id not in (None, session_id)
            ):
                return False
        WorkspaceHandoffService._validate_native_cleanup(uow, transition)
        return True


@dataclass(frozen=True, slots=True)
class PlanningActionTarget:
    workspace_id: UUID
    principal_id: str
    planning_request_id: UUID
    planning_run_id: UUID
    planning_profile_id: UUID
    planning_profile_version: int
    profile_ref: ProfileRef
    operation_id: UUID
    target: object


def require_active_planning(
    uow, run, request, *, now, owner_id=None, lease_id=None, fencing_token=None
):
    lease = uow.planning_leases.get_active_for_run(run.id, run.workspace_id)
    if (
        request.id != run.planning_request_id
        or request.workspace_id != run.workspace_id
        or request.current_run_id != run.id
        or request.current_brief_revision != run.brief_revision
        or request.status.value != "planning"
        or run.status.value != "running"
        or run.profile_snapshot != request.profile_snapshot
        or run.profile_snapshot.execution_mode != "native_profile"
        or lease is None
        or lease.status.value != "active"
        or lease.expires_at <= now
        or not owner_id
        or type(lease_id) is not UUID
        or type(fencing_token) is not int
        or fencing_token < 1
        or lease.owner_id != owner_id
        or lease.id != lease_id
        or lease.fencing_token != fencing_token
        or lease.planning_run_id != run.id
        or lease.workspace_id != run.workspace_id
    ):
        _deny()
    return lease


class PlanningAuthorityReader:
    def __init__(self, uow_factory, *, principal, authorizer, clock=None):
        self._uow_factory, self._principal, self._authorizer = (
            uow_factory,
            principal,
            authorizer,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        if not isinstance(grant_id, UUID) or not self._principal():
            _deny()
        with self._uow_factory() as uow:
            authority = uow.planning_authorities.get(grant_id)
            if authority is None or authority.principal_id != self._principal():
                _deny()
            run = uow.planning_runs.get(
                authority.planning_run_id, authority.workspace_id
            )
            request = uow.planning_requests.get(
                authority.planning_request_id, authority.workspace_id
            )
            link = uow.planning_authorities.get_link(
                authority.workspace_id, authority.planning_run_id
            )
            if (
                run is None
                or request is None
                or link is None
                or (
                    link.authority_id,
                    link.start_operation_id,
                    link.profile_ref,
                    run.planning_request_id,
                    run.operation_id,
                    run.profile_snapshot.profile_id,
                    run.profile_snapshot.profile_version,
                    run.profile_snapshot.planner_agent_ref,
                    run.profile_snapshot.execution_mode,
                )
                != (
                    authority.id,
                    authority.operation_id,
                    authority.profile_ref,
                    request.id,
                    authority.operation_id,
                    authority.planning_profile_id,
                    authority.planning_profile_version,
                    authority.profile_ref,
                    "native_profile",
                )
                or link.start_request.workspace != authority.execution_workspace
            ):
                _deny()
            if action in {"run.read", "run.stop"}:
                if (
                    type(target) is not UUID
                    or target != authority.workspace_id
                    or action not in authority.actions
                ):
                    _deny()
                return
            if (
                authority.revoked
                or action not in authority.actions
                or self._authorizer is None
            ):
                _deny()
            require_active_planning(
                uow,
                run,
                request,
                now=self._clock(),
                owner_id=authority.owner_id,
                lease_id=authority.lease_id,
                fencing_token=authority.fencing_token,
            )
            if action in {"run.start", "profile.read"}:
                if (
                    not isinstance(target, ProfileRef)
                    or target != authority.profile_ref
                ):
                    _deny()
                if action == "run.start" and (
                    link.status not in {"pending", "starting"}
                    or link.run_id is not None
                ):
                    _deny()
            elif action == "runtime.read":
                if (
                    not isinstance(target, RuntimeRef)
                    or target != authority.runtime_ref
                ):
                    _deny()
            elif action == "workspace.execute":
                if (
                    not isinstance(target, ExecutionWorkspace)
                    or target != authority.execution_workspace
                ):
                    _deny()
            elif action == "source.read":
                if not isinstance(target, SourceRef) or target not in authority.sources:
                    _deny()
            elif action == "tool.execute":
                if not isinstance(target, ToolBinding) or target not in authority.tools:
                    _deny()
            elif action == "resource.read":
                if (
                    not isinstance(target, ResourceRef)
                    or target not in authority.resources
                ):
                    _deny()
            elif action == "secret.read":
                ref = (
                    target.credential_ref
                    if isinstance(target, SecretBinding)
                    else target
                )
                if (
                    not isinstance(ref, CredentialRef)
                    or ref not in authority.credentials
                ):
                    _deny()
            else:
                _deny()
            try:
                self._authorizer.require(
                    authority.upstream_grant_id,
                    action,
                    PlanningActionTarget(
                        authority.workspace_id,
                        authority.principal_id,
                        request.id,
                        run.id,
                        authority.planning_profile_id,
                        authority.planning_profile_version,
                        authority.profile_ref,
                        authority.operation_id,
                        target,
                    ),
                )
            except (DomainError, AgentError):
                _deny()


class NativeAuthorityReader:
    """Dispatch only grants actually owned by one CP consumer."""

    def __init__(self, uow_factory, *, worker, planner):
        self._uow_factory, self._worker, self._planner = uow_factory, worker, planner

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        if not isinstance(grant_id, UUID):
            _deny()
        with self._uow_factory() as uow:
            worker = uow.execution_authorities.get(grant_id)
            planner = uow.planning_authorities.get(grant_id)
        if (worker is None) == (planner is None):
            _deny()
        reader = self._worker if worker is not None else self._planner
        if reader is None:
            _deny()
        reader.require(grant_id, action, target)
