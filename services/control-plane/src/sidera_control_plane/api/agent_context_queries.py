from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sidera_agents.contracts import ContextItem, SourceContent, SourceRef
from sidera_agents.errors import AgentError

from sidera_control_plane.api.dependencies import (
    get_agent_context_identity,
    get_agent_context_source_reader,
)
from sidera_control_plane.application.agent_sources import TaskContextSourceReader
from sidera_control_plane.application.assignments import TrustedAssignmentIdentity

router = APIRouter(prefix="/api/v1/workspaces")
_PATH = "/{workspace_id}/executions/{execution_id}/agent-context/{operation_id}"


def _error(error: AgentError) -> HTTPException:
    denied = error.code == "permission_denied"
    return HTTPException(
        status_code=403 if denied else 409,
        detail={
            "code": "permission_denied" if denied else "context_unavailable",
            "message": "Task source access denied."
            if denied
            else "Pinned task context is unavailable.",
        },
    )


@router.get(
    _PATH,
    response_model=tuple[ContextItem, ...],
    operation_id="get_authorized_agent_context",
)
def get_agent_context(
    workspace_id: UUID,
    execution_id: UUID,
    operation_id: UUID,
    response: Response,
    identity: Annotated[TrustedAssignmentIdentity, Depends(get_agent_context_identity)],
    reader: Annotated[
        TaskContextSourceReader, Depends(get_agent_context_source_reader)
    ],
) -> tuple[ContextItem, ...]:
    response.headers["Cache-Control"] = "no-store"
    try:
        reader.require_execution(
            identity.grant_id, workspace_id, execution_id, operation_id
        )
        return reader.context_items(identity.grant_id, execution_id)
    except AgentError as exc:
        raise _error(exc) from None


@router.post(
    _PATH + "/source",
    response_model=SourceContent,
    operation_id="read_authorized_agent_source",
)
def read_agent_source(
    workspace_id: UUID,
    execution_id: UUID,
    operation_id: UUID,
    ref: SourceRef,
    response: Response,
    identity: Annotated[TrustedAssignmentIdentity, Depends(get_agent_context_identity)],
    reader: Annotated[
        TaskContextSourceReader, Depends(get_agent_context_source_reader)
    ],
) -> SourceContent:
    response.headers["Cache-Control"] = "no-store"
    try:
        reader.require_execution(
            identity.grant_id, workspace_id, execution_id, operation_id
        )
        return reader.read(identity.grant_id, ref)
    except AgentError as exc:
        raise _error(exc) from None
