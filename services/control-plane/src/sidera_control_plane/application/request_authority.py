"""Host-verified HTTP identity; CP keeps its business and human decision gates."""

from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from fastapi import HTTPException, Request
from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.application.assignments import TrustedAssignmentIdentity
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.commands import CommandEnvelope

ControlPlaneIdentity = TrustedAssignmentIdentity


@dataclass(frozen=True, slots=True)
class ControlPlaneRequestTarget:
    workspace_id: UUID
    principal_id: str
    method: str
    route: str
    path_parameters: tuple[tuple[str, str], ...]
    query_parameters: tuple[tuple[str, str], ...]
    command: CommandEnvelope | None = None


class ControlPlaneRequestAuthorizer(ExecutionAuthorizer, Protocol):
    def authenticate_request(self, request: Request) -> ControlPlaneIdentity | None: ...

    def identity_for_grant(self, grant_id: UUID) -> ControlPlaneIdentity | None: ...


HUMAN_COMMAND_TYPES = frozenset(
    {
        "ConfirmPlanningProposal",
        "ConfirmPlanRevision",
        "ConfirmProfileHandoff",
        "ConfirmWorkerHandoff",
        "ConfirmWorkerTransitionStopped",
        "ApproveCollaborationReleaseProposal",
        "AuthorizeAdditionalRework",
        "CompleteTask",
    }
)


def require_request_identity(
    identity: ControlPlaneIdentity | None,
) -> ControlPlaneIdentity:
    if (
        not isinstance(identity, ControlPlaneIdentity)
        or not isinstance(identity.principal_id, str)
        or not identity.principal_id.strip()
        or not isinstance(identity.grant_id, UUID)
        or not isinstance(identity.actor_type, str)
        or identity.actor_type not in {"user", "agent"}
    ):
        raise HTTPException(401, detail={"code": "authentication_required"})
    return identity


def require_request_scope(
    authorizer: ControlPlaneRequestAuthorizer,
    identity: ControlPlaneIdentity,
    action: str,
    target: ControlPlaneRequestTarget,
) -> None:
    try:
        authorizer.require(identity.grant_id, action, target)
    except Exception:  # noqa: BLE001 - Host errors must never disclose credentials.
        raise HTTPException(403, detail={"code": "permission_denied"}) from None


def require_command_authority(
    authorizer: ControlPlaneRequestAuthorizer,
    identity: ControlPlaneIdentity,
    target: ControlPlaneRequestTarget,
    settings: Settings,
    envelope: CommandEnvelope,
) -> None:
    try:
        current = authorizer.identity_for_grant(identity.grant_id)
    except Exception:  # noqa: BLE001 - A failed current-grant lookup is fail-closed.
        current = None
    if current != identity:
        raise HTTPException(403, detail={"code": "permission_denied"})
    require_request_identity(current)
    if (
        envelope.workspace_id != target.workspace_id
        or envelope.actor_type != identity.actor_type
        or envelope.actor_id != identity.principal_id
        or (
            envelope.command_type in HUMAN_COMMAND_TYPES
            and (
                identity.actor_type != "user"
                or settings.local_actor_type != "user"
                or identity.principal_id != settings.local_actor_id
            )
        )
    ):
        raise HTTPException(403, detail={"code": "permission_denied"})
    require_request_scope(
        authorizer,
        identity,
        "control_plane.command:" + envelope.command_type,
        replace(target, command=envelope),
    )
