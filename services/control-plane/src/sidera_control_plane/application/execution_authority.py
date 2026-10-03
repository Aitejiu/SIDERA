from datetime import UTC, datetime
from uuid import UUID

from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RuntimeRef,
    SecretBinding,
    SourceRef,
    ToolBinding,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.domain.errors import DomainError


def _deny():
    raise AgentError("permission_denied", "Execution authority denied.")


class ExecutionAuthorityReader:
    def __init__(self, uow_factory, *, principal, authorizer, clock=None):
        self._uow_factory = uow_factory
        self._principal = principal
        self._authorizer = authorizer
        self._clock = clock or (lambda: datetime.now(UTC))

    def require(self, grant_id: UUID, action: str, target: object) -> None:
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
            if (
                authority.revoked
                or action not in authority.actions
                or self._authorizer is None
            ):
                _deny()
            try:
                self._authorizer.require(
                    grant_id,
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
                or execution.handoff_transition_id is not None
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
                if action in {"run.input", "run.interrupt"} and link.status not in {
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
