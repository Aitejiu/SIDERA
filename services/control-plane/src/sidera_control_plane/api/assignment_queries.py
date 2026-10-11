from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sidera_agents.errors import AgentError

from sidera_control_plane.api.dependencies import get_trusted_assignment_identity
from sidera_control_plane.application.errors import NotFound, ServiceUnavailable
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.domain.assignment_views import (
    AgentBindingView,
    AgentExecutionView,
    AssignmentKind,
    NativeRunSummary,
    ProfileWorkKind,
    ProfileWorkPage,
)
from sidera_control_plane.domain.errors import ValidationError


class AssignmentReads(Protocol):
    def read_assignment(
        self,
        workspace_id: UUID,
        target_type: AssignmentKind,
        target_id: UUID,
        *,
        grant_id: UUID,
    ) -> AgentBindingView | None: ...

    def read_agent_execution(
        self, workspace_id: UUID, execution_id: UUID, *, grant_id: UUID
    ) -> AgentExecutionView: ...

    def list_profile_work(
        self,
        workspace_id: UUID,
        profile_id: UUID,
        *,
        grant_id: UUID,
        after: str | None = None,
        limit: int = 50,
    ) -> ProfileWorkPage: ...

    def read_profile_work_run(
        self,
        workspace_id: UUID,
        kind: ProfileWorkKind,
        work_id: UUID,
        *,
        grant_id: UUID,
        target_type: AssignmentKind | None = None,
    ) -> NativeRunSummary: ...


def get_assignment_reads() -> AssignmentReads | None:
    return None


def require_human_identity(
    identity: Annotated[
        ControlPlaneIdentity | None, Depends(get_trusted_assignment_identity)
    ],
) -> ControlPlaneIdentity:
    if (
        not isinstance(identity, ControlPlaneIdentity)
        or identity.actor_type != "user"
        or type(identity.grant_id) is not UUID
    ):
        raise HTTPException(403, detail={"code": "permission_denied"})
    return identity


def require_reads(
    reads: Annotated[AssignmentReads | None, Depends(get_assignment_reads)],
) -> AssignmentReads:
    if reads is None:
        raise ServiceUnavailable("Work reads are unavailable.")
    return reads


@contextmanager
def _read_errors() -> Iterator[None]:
    try:
        yield
    except AgentError as error:
        if error.code == "permission_denied":
            raise HTTPException(403, detail={"code": "permission_denied"}) from None
        if error.code == "not_found":
            raise NotFound("Work is unavailable.") from None
        if error.code == "configuration_invalid":
            raise ValidationError("Invalid work read request.") from None
        raise ServiceUnavailable("Work reads are unavailable.") from None


router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}")
type Identity = Annotated[ControlPlaneIdentity, Depends(require_human_identity)]
type Reads = Annotated[AssignmentReads, Depends(require_reads)]


@router.get(
    "/assignments/{target_type}/{target_id}",
    response_model=AgentBindingView | None,
    operation_id="readAgentAssignment",
)
def read_assignment(
    workspace_id: UUID,
    target_type: AssignmentKind,
    target_id: UUID,
    identity: Identity,
    reads: Reads,
) -> AgentBindingView | None:
    with _read_errors():
        return reads.read_assignment(
            workspace_id, target_type, target_id, grant_id=identity.grant_id
        )


@router.get(
    "/agent-executions/{execution_id}",
    response_model=AgentExecutionView,
    operation_id="readAgentExecution",
)
def read_agent_execution(
    workspace_id: UUID, execution_id: UUID, identity: Identity, reads: Reads
) -> AgentExecutionView:
    with _read_errors():
        return reads.read_agent_execution(
            workspace_id, execution_id, grant_id=identity.grant_id
        )


@router.get(
    "/profiles/{profile_id}/work",
    response_model=ProfileWorkPage,
    operation_id="listAgentProfileWork",
)
def list_profile_work(
    workspace_id: UUID,
    profile_id: UUID,
    identity: Identity,
    reads: Reads,
    after: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ProfileWorkPage:
    with _read_errors():
        return reads.list_profile_work(
            workspace_id,
            profile_id,
            grant_id=identity.grant_id,
            after=after,
            limit=limit,
        )


@router.get(
    "/profile-work/{kind}/{work_id}/run",
    response_model=NativeRunSummary,
    operation_id="readAgentProfileWorkRun",
)
def read_profile_work_run(
    workspace_id: UUID,
    kind: ProfileWorkKind,
    work_id: UUID,
    identity: Identity,
    reads: Reads,
    target_type: Annotated[AssignmentKind | None, Query()] = None,
) -> NativeRunSummary:
    with _read_errors():
        return reads.read_profile_work_run(
            workspace_id,
            kind,
            work_id,
            grant_id=identity.grant_id,
            target_type=target_type,
        )
