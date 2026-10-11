from collections.abc import Callable
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from sidera_model_connections.contracts import (
    ConnectionAvailability,
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    CredentialWrite,
    ModelConnectionRevision,
)
from sidera_model_connections.credentials import CredentialRevocationResult
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.http.auth import ManagementPrincipal
from sidera_model_connections.http.schemas import (
    ConnectionInput,
    ConnectionRevisionInput,
    ConnectionSummarySchema,
    CredentialRevocationSchema,
    CredentialRevokeInput,
    CredentialSummarySchema,
    CredentialWriteInput,
    CredentialWriteSummarySchema,
    EnvCredentialInput,
)
from sidera_model_connections.ports import ConnectionCatalog

CONFIGURATION_ERROR_STATUS = MappingProxyType(
    {
        "invalid_configuration": 422,
        "configuration_not_found": 404,
        "configuration_version_conflict": 409,
        "credential_not_found": 404,
        "credential_revoked": 409,
        "credential_unresolved": 503,
        "credential_store_unavailable": 503,
        "credential_store_locked": 503,
        "credential_write_unknown": 503,
        "credential_operation_conflict": 409,
        "credential_already_exists": 409,
        "runtime_binding_denied": 403,
        "connection_role_denied": 403,
        "connection_target_denied": 403,
        "management_unauthorized": 401,
        "management_scope_denied": 403,
    }
)

_ERROR_MESSAGES = MappingProxyType(
    {
        "invalid_configuration": "Invalid configuration request",
        "configuration_not_found": "Configuration not found",
        "configuration_version_conflict": "Configuration version conflict",
        "credential_not_found": "Credential not found",
        "credential_revoked": "Credential revoked",
        "credential_unresolved": "Credential unresolved",
        "credential_store_unavailable": "Credential store unavailable",
        "credential_store_locked": "Credential store locked",
        "credential_write_unknown": "Credential write status unknown",
        "credential_operation_conflict": "Credential operation conflict",
        "credential_already_exists": "Credential already exists",
        "runtime_binding_denied": "Runtime binding denied",
        "connection_role_denied": "Connection role denied",
        "connection_target_denied": "Connection target denied",
        "management_unauthorized": "Management authorization required",
        "management_scope_denied": "Management workspace access denied",
    }
)

_AVAILABILITY_BY_CREDENTIAL_STATUS: dict[str, ConnectionAvailability] = {
    "active": "configured",
    "pending": "credential_unresolved",
    "failed": "credential_unresolved",
    "revoked": "credential_revoked",
}


class SafeConfigurationRoute(APIRoute):
    def get_route_handler(self) -> Callable[..., Any]:
        original_handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                response = await original_handler(request)
            except RequestValidationError:
                response = _error_response("invalid_configuration")
            except ConfigurationError as error:
                response = _error_response(error.code)
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe_handler


def _error_response(code: str) -> JSONResponse:
    return JSONResponse(
        status_code=CONFIGURATION_ERROR_STATUS[code],
        content={"error": {"code": code, "message": _ERROR_MESSAGES[code]}},
        headers={"Cache-Control": "no-store"},
    )


def _credential_summary(credential: CredentialMetadata) -> CredentialSummarySchema:
    return CredentialSummarySchema(
        workspace_id=credential.ref.workspace_id,
        credential_id=credential.ref.credential_id,
        alias=credential.alias,
        source=credential.source,
        status=credential.status,
        version=credential.version,
        replaces_id=(
            credential.replaces_ref.credential_id
            if credential.replaces_ref is not None
            else None
        ),
    )


def _connection_summary(
    connection: ModelConnectionRevision,
    catalog: ConnectionCatalog,
) -> ConnectionSummarySchema:
    credential = catalog.get_credential(connection.credential_ref)
    availability = _AVAILABILITY_BY_CREDENTIAL_STATUS.get(credential.status)
    if availability is None:
        raise ConfigurationError("invalid_configuration") from None
    return ConnectionSummarySchema(
        workspace_id=connection.ref.workspace_id,
        connection_id=connection.ref.connection_id,
        revision=connection.ref.revision,
        name=connection.name,
        provider_id=connection.provider_id,
        protocol=connection.protocol,
        base_url=connection.base_url,
        network_scope=connection.network_scope,
        allowed_roles=connection.allowed_roles,
        credential_id=connection.credential_ref.credential_id,
        availability=availability,
    )


def _write_summary(write: CredentialWrite) -> CredentialWriteSummarySchema:
    return CredentialWriteSummarySchema(
        workspace_id=write.workspace_id,
        operation_id=write.operation_id,
        status=write.status,
        credential=_credential_summary(write.credential),
    )


def create_management_router(
    connection_service: Any,
    credential_service: Any,
    catalog: ConnectionCatalog,
    *,
    authorize_management: Callable[[Request], ManagementPrincipal],
) -> APIRouter:
    if not callable(authorize_management):
        raise TypeError("authorize_management is required")

    router = APIRouter(
        prefix="/model-config/v1",
        route_class=SafeConfigurationRoute,
    )

    def authorize_workspace(
        request: Request,
        workspace_id: UUID,
    ) -> ManagementPrincipal:
        principal = authorize_management(request)
        if not isinstance(principal, ManagementPrincipal):
            raise ConfigurationError("management_unauthorized") from None
        if workspace_id not in principal.workspace_ids:
            raise ConfigurationError("management_scope_denied") from None
        return principal

    @router.get(
        "/workspaces/{workspace_id}/credentials",
        response_model=list[CredentialSummarySchema],
    )
    def list_credentials(
        workspace_id: UUID,
        _principal: Annotated[ManagementPrincipal, Depends(authorize_workspace)],
    ) -> list[CredentialSummarySchema]:
        return [
            _credential_summary(item) for item in catalog.list_credentials(workspace_id)
        ]

    @router.get(
        "/workspaces/{workspace_id}/connections",
        response_model=list[ConnectionSummarySchema],
    )
    def list_connections(
        workspace_id: UUID,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> list[ConnectionSummarySchema]:
        connections = catalog.list_connections(workspace_id)
        return [_connection_summary(connection, catalog) for connection in connections]

    @router.get(
        "/workspaces/{workspace_id}/connections/{id}/revisions/{revision}",
        response_model=ConnectionSummarySchema,
    )
    def get_connection_revision(
        workspace_id: UUID,
        connection_id: Annotated[UUID, Path(alias="id")],
        revision: Annotated[int, Path(ge=1)],
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> ConnectionSummarySchema:
        connection = catalog.get_connection(
            ConnectionRef(workspace_id, connection_id, revision)
        )
        return _connection_summary(connection, catalog)

    @router.post(
        "/workspaces/{workspace_id}/connections",
        response_model=ConnectionSummarySchema,
        status_code=201,
    )
    def create_connection(
        workspace_id: UUID,
        payload: ConnectionInput,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> ConnectionSummarySchema:
        connection = _connection_from_input(
            workspace_id,
            uuid4(),
            1,
            payload,
        )
        saved = connection_service.create(connection)
        return _connection_summary(saved, catalog)

    @router.post(
        "/workspaces/{workspace_id}/connections/{id}/revisions",
        response_model=ConnectionSummarySchema,
        status_code=201,
    )
    def revise_connection(
        workspace_id: UUID,
        connection_id: Annotated[UUID, Path(alias="id")],
        payload: ConnectionRevisionInput,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> ConnectionSummarySchema:
        connection = _connection_from_input(
            workspace_id,
            connection_id,
            payload.expected_revision + 1,
            payload,
        )
        saved = connection_service.revise(
            connection,
            expected_revision=payload.expected_revision,
        )
        return _connection_summary(saved, catalog)

    @router.post(
        "/workspaces/{workspace_id}/credential-writes",
        response_model=CredentialWriteSummarySchema,
        status_code=201,
    )
    def save_credential(
        workspace_id: UUID,
        payload: CredentialWriteInput,
        response: Response,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> CredentialWriteSummarySchema:
        replaces_ref = (
            CredentialRef(workspace_id, payload.replaces_id)
            if payload.replaces_id is not None
            else None
        )
        write = credential_service.save_managed(
            workspace_id,
            payload.operation_id,
            payload.alias,
            payload.secret,
            replaces_ref=replaces_ref,
        )
        _set_write_status(write, response, completed_status=201)
        return _write_summary(write)

    @router.get(
        "/workspaces/{workspace_id}/credential-writes/{operation_id}",
        response_model=CredentialWriteSummarySchema,
    )
    def get_credential_write(
        workspace_id: UUID,
        operation_id: UUID,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> CredentialWriteSummarySchema:
        write = catalog.get_credential_write(workspace_id, operation_id)
        return _write_summary(write)

    @router.post(
        "/workspaces/{workspace_id}/credential-writes/{operation_id}/recover",
        response_model=CredentialWriteSummarySchema,
    )
    async def recover_credential_write(
        workspace_id: UUID,
        operation_id: UUID,
        request: Request,
        response: Response,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> CredentialWriteSummarySchema:
        if await request.body():
            raise ConfigurationError("invalid_configuration") from None
        write = await run_in_threadpool(
            credential_service.recover_write,
            workspace_id,
            operation_id,
        )
        _set_write_status(write, response, completed_status=200)
        return _write_summary(write)

    @router.post(
        "/workspaces/{workspace_id}/env-credentials",
        response_model=CredentialSummarySchema,
        status_code=201,
    )
    def register_env_credential(
        workspace_id: UUID,
        payload: EnvCredentialInput,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> CredentialSummarySchema:
        credential = credential_service.register_env(
            workspace_id,
            payload.alias,
            payload.env_name,
        )
        return _credential_summary(credential)

    @router.post(
        "/workspaces/{workspace_id}/credentials/{id}/revoke",
        response_model=CredentialRevocationSchema,
    )
    def revoke_credential(
        workspace_id: UUID,
        credential_id: Annotated[UUID, Path(alias="id")],
        payload: CredentialRevokeInput,
        _principal: Annotated[
            ManagementPrincipal, Depends(authorize_workspace)
        ],
    ) -> CredentialRevocationSchema:
        result = credential_service.revoke(
            CredentialRef(workspace_id, credential_id),
            expected_version=payload.expected_version,
        )
        if not isinstance(result, CredentialRevocationResult):
            raise ConfigurationError("credential_write_unknown") from None
        return CredentialRevocationSchema(
            credential=_credential_summary(result.credential),
            cleanup_pending=result.cleanup_pending,
        )

    return router


def _connection_from_input(
    workspace_id: UUID,
    connection_id: UUID,
    revision: int,
    payload: ConnectionInput,
) -> ModelConnectionRevision:
    return ModelConnectionRevision(
        ref=ConnectionRef(workspace_id, connection_id, revision),
        name=payload.name,
        provider_id=payload.provider_id,
        protocol=payload.protocol,
        base_url=payload.base_url,
        network_scope=payload.network_scope,
        allowed_roles=payload.allowed_roles,
        credential_ref=CredentialRef(workspace_id, payload.credential_id),
        created_at=datetime.now(UTC),
    )


def _set_write_status(
    write: CredentialWrite,
    response: Response,
    *,
    completed_status: int,
) -> None:
    if write.status == "failed":
        raise ConfigurationError("credential_unresolved") from None
    if write.status == "completed":
        response.status_code = completed_status
    else:
        response.status_code = 202
