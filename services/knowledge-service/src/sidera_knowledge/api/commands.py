from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sidera_knowledge.api.dependencies import authorize_actor
from sidera_knowledge.api.errors import api_error_response, status_for_error
from sidera_knowledge.api.schemas import CommandRequest

router = APIRouter(prefix="/api/v1")


@router.post("/commands")
def execute_command(request: Request, body: CommandRequest) -> JSONResponse:
    authorize_actor(request, body.actor.type)
    outcome = request.app.state.command_service.handle(body.to_domain())
    if outcome.status == "applied":
        return JSONResponse(
            status_code=200,
            content={
                "status": outcome.status,
                "command_id": str(outcome.command_id),
                "result": outcome.result,
            },
        )

    code = outcome.error_code or "command_rejected"
    return api_error_response(
        code,
        status_code=status_for_error(code),
        details=outcome.result,
    )
