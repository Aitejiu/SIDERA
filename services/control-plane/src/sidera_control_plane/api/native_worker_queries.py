from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sidera_agents.errors import AgentError

from sidera_control_plane.api.assignment_queries import require_human_identity
from sidera_control_plane.application.errors import NotFound, ServiceUnavailable
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.native_worker_views import (
    NativeWorkerContextView,
    NativeWorkerTraceView,
)


class _NativeTraceUnsupported(ServiceUnavailable):
    code = "native_trace_unsupported"


class NativeWorkerReads(Protocol):
    def read_native_worker_context(
        self, workspace_id: UUID, worker_item_id: UUID, *, grant_id: UUID
    ) -> NativeWorkerContextView: ...

    def read_native_worker_trace(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
        execution_id: UUID,
        *,
        grant_id: UUID,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> NativeWorkerTraceView: ...


def get_native_worker_reads() -> NativeWorkerReads | None:
    return None


def _require_reads(
    reads: Annotated[NativeWorkerReads | None, Depends(get_native_worker_reads)],
) -> NativeWorkerReads:
    if reads is None:
        raise ServiceUnavailable("Native worker reads are unavailable.")
    return reads


def _reject_context_query(request: Request) -> None:
    if request.query_params:
        raise HTTPException(422, detail={"code": "validation_error"})


def _reject_trace_query_extras(request: Request) -> None:
    allowed = {"after_sequence", "limit"}
    items = request.query_params.multi_items()
    keys = [key for key, _ in items]
    if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
        raise HTTPException(422, detail={"code": "validation_error"})


@contextmanager
def _read_errors(*, classify_native_trace_unsupported: bool = False) -> Iterator[None]:
    try:
        yield
    except AgentError as error:
        if error.code == "permission_denied":
            raise HTTPException(403, detail={"code": "permission_denied"}) from None
        if (
            classify_native_trace_unsupported
            and error.code == "native_trace_unsupported"
        ):
            raise _NativeTraceUnsupported("Native trace is unsupported.") from None
        if error.code == "not_found":
            raise NotFound("Native worker target is unavailable.") from None
        if error.code == "configuration_invalid":
            raise ValidationError("Invalid native worker read request.") from None
        raise ServiceUnavailable("Native worker reads are unavailable.") from None
    except Exception:  # noqa: BLE001 - Do not expose store or adapter details.
        raise ServiceUnavailable("Native worker reads are unavailable.") from None


router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}")
type Identity = Annotated[ControlPlaneIdentity, Depends(require_human_identity)]
type Reads = Annotated[NativeWorkerReads, Depends(_require_reads)]


@router.get(
    "/worker-items/{worker_item_id}/native-context",
    response_model=NativeWorkerContextView,
    operation_id="readNativeWorkerContext",
)
def read_native_worker_context(
    workspace_id: UUID,
    worker_item_id: UUID,
    identity: Identity,
    reads: Reads,
    _query: None = Depends(_reject_context_query),
) -> NativeWorkerContextView:
    with _read_errors():
        return reads.read_native_worker_context(
            workspace_id, worker_item_id, grant_id=identity.grant_id
        )


@router.get(
    "/worker-items/{worker_item_id}/executions/{execution_id}/native-trace",
    response_model=NativeWorkerTraceView,
    operation_id="readNativeWorkerTrace",
)
def read_native_worker_trace(
    workspace_id: UUID,
    worker_item_id: UUID,
    execution_id: UUID,
    identity: Identity,
    reads: Reads,
    _query: None = Depends(_reject_trace_query_extras),
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> NativeWorkerTraceView:
    with _read_errors(classify_native_trace_unsupported=True):
        return reads.read_native_worker_trace(
            workspace_id,
            worker_item_id,
            execution_id,
            grant_id=identity.grant_id,
            after_sequence=after_sequence,
            limit=limit,
        )
