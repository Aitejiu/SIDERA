from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sidera_agents.contracts import (
    ConfigValidation,
    ProfileRef,
    ProfileStatus,
    ResourceRef,
    ResourceStatus,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeRef,
    RuntimeSnapshot,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

from sidera_product.defaults import DefaultsBody, DefaultsError, DefaultsView

from .configuration import ConfigurationError
from .contracts import ConfigurationResult
from .schemas import (
    ConversationWorkView,
    ProfileCreateBody,
    ProfileListView,
    ProfileRevisionBody,
    ProfileView,
    ProfileWorkView,
    ResourceCreateBody,
    ResourceListView,
    ResourceRevisionBody,
    ResourceView,
    RuntimeLimitsBody,
    RuntimeListView,
    RuntimeRefreshBody,
    RuntimeRegisterBody,
    RuntimeStatusBody,
    StatusBody,
    ToolConnectionView,
)

if TYPE_CHECKING:
    from sidera_product.lifecycle import ProductRuntime


async def call[T](operation: Callable[[], T]) -> T:
    try:
        return await anyio.to_thread.run_sync(operation)
    except (ConfigurationError, DefaultsError) as error:
        raise HTTPException(error.status_code, detail={"code": error.code}) from None
    except AgentError as error:
        status = (
            403
            if error.code == "permission_denied"
            else 404
            if error.code == "not_found"
            else 422
            if error.code == "configuration_invalid"
            else 503
            if error.code
            in {"product_unavailable", "store_unavailable", "runtime_unavailable"}
            else 409
        )
        raise HTTPException(status, detail={"code": error.code}) from None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def current_human(
    owner: ProductRuntime, request: Request, workspace_id: UUID
) -> ControlPlaneIdentity:
    identity = await call(lambda: owner.local_sessions.authenticate_request(request))
    if (
        identity is None
        or identity.actor_type != "user"
        or identity.principal_id != owner.settings.local_principal_id
    ):
        raise HTTPException(401, detail={"code": "local_session_required"})
    if workspace_id != owner.settings.workspace_id:
        raise HTTPException(403, detail={"code": "permission_denied"})
    current = await call(
        lambda: owner.local_sessions.identity_for_grant(identity.grant_id)
    )
    if current != identity:
        raise HTTPException(401, detail={"code": "local_session_required"})
    return identity


def _result_status(
    result: ConfigurationResult, response: Response, *, create: bool = False
) -> ConfigurationResult:
    if result.state in {"claimed", "dispatching", "unknown"}:
        response.status_code = 202
    elif result.state == "rejected":
        response.status_code = (
            422
            if result.reason_code == "configuration_invalid"
            else 403
            if result.reason_code == "permission_denied"
            else 404
            if result.reason_code == "not_found"
            else 409
        )
    else:
        response.status_code = 201 if create else 200
    return result


def agents_router(owner: ProductRuntime) -> APIRouter:
    router = APIRouter(
        prefix="/agents/v1/workspaces/{workspace_id}", dependencies=[Depends(_no_store)]
    )

    async def profile_ref(
        workspace_id: UUID,
        profile_id: UUID,
        revision: int | None,
        identity: ControlPlaneIdentity,
    ) -> ProfileRef:
        if revision is not None:
            return ProfileRef(
                workspace_id=workspace_id, profile_id=profile_id, revision=revision
            )
        profiles = await call(
            lambda: owner.agents.catalog.list_profiles(
                workspace_id, grant_id=identity.grant_id
            )
        )
        matches = [
            profile.ref
            for profile in profiles
            if profile.ref.workspace_id == workspace_id
            and profile.ref.profile_id == profile_id
        ]
        if len(matches) != 1:
            raise HTTPException(404, detail={"code": "not_found"})
        return matches[0]

    async def resource_ref(
        workspace_id: UUID,
        resource_id: UUID,
        revision: int | None,
        identity: ControlPlaneIdentity,
    ) -> ResourceRef:
        if revision is not None:
            return ResourceRef(
                workspace_id=workspace_id, resource_id=resource_id, revision=revision
            )
        resources = await call(
            lambda: owner.agents.catalog.list_resources(
                workspace_id, grant_id=identity.grant_id
            )
        )
        matches = [
            resource.ref
            for resource in resources
            if resource.ref.workspace_id == workspace_id
            and resource.ref.resource_id == resource_id
        ]
        if len(matches) != 1:
            raise HTTPException(404, detail={"code": "not_found"})
        return matches[0]

    @router.get(
        "/profiles", response_model=ProfileListView, operation_id="listAgentProfiles"
    )
    async def list_profiles(workspace_id: UUID, request: Request) -> ProfileListView:
        identity = await current_human(owner, request, workspace_id)
        profiles = await call(
            lambda: owner.agents.catalog.list_profiles(
                workspace_id, grant_id=identity.grant_id
            )
        )
        items = [
            ProfileView(
                profile=profile,
                status=await call(
                    lambda profile=profile: owner.agents.catalog.get_profile_status(
                        profile.ref, grant_id=identity.grant_id
                    )
                ),
            )
            for profile in profiles
        ]
        return ProfileListView(items=tuple(items))

    @router.post(
        "/profiles",
        response_model=ConfigurationResult,
        status_code=201,
        responses={202: {"model": ConfigurationResult}},
        operation_id="createAgentProfile",
    )
    async def create_profile(
        workspace_id: UUID,
        body: ProfileCreateBody,
        request: Request,
        response: Response,
    ) -> ConfigurationResult:
        identity = await current_human(owner, request, workspace_id)
        result = await call(
            lambda: owner.configuration.submit(
                identity,
                action="profile.create",
                operation_id=body.operation_id,
                draft=body.draft,
            )
        )
        return _result_status(result, response, create=True)

    @router.get(
        "/profiles/{profile_id}",
        response_model=ProfileView,
        operation_id="readAgentProfile",
    )
    async def read_profile(
        workspace_id: UUID,
        profile_id: UUID,
        request: Request,
        revision: Annotated[int | None, Query(ge=1)] = None,
    ) -> ProfileView:
        identity = await current_human(owner, request, workspace_id)
        ref = await profile_ref(workspace_id, profile_id, revision, identity)
        profile = await call(
            lambda: owner.agents.catalog.get_profile(ref, grant_id=identity.grant_id)
        )
        status = await call(
            lambda: owner.agents.catalog.get_profile_status(
                ref, grant_id=identity.grant_id
            )
        )
        return ProfileView(profile=profile, status=status)

    @router.post(
        "/profiles/{profile_id}/revisions",
        response_model=ConfigurationResult,
        responses={202: {"model": ConfigurationResult}},
        operation_id="reviseAgentProfile",
    )
    async def revise_profile(
        workspace_id: UUID,
        profile_id: UUID,
        body: ProfileRevisionBody,
        request: Request,
        response: Response,
    ) -> ConfigurationResult:
        identity = await current_human(owner, request, workspace_id)
        if (
            body.expected_ref.workspace_id != workspace_id
            or body.expected_ref.profile_id != profile_id
        ):
            raise HTTPException(403, detail={"code": "permission_denied"})
        result = await call(
            lambda: owner.configuration.submit(
                identity,
                action="profile.revise",
                operation_id=body.operation_id,
                draft=body.draft,
                expected_ref=body.expected_ref,
            )
        )
        return _result_status(result, response)

    @router.put(
        "/profiles/{profile_id}/status",
        response_model=ProfileStatus,
        operation_id="setAgentProfileStatus",
    )
    async def profile_status(
        workspace_id: UUID, profile_id: UUID, body: StatusBody, request: Request
    ) -> ProfileStatus:
        identity = await current_human(owner, request, workspace_id)
        return await call(
            lambda: owner.agents.catalog.set_profile_enabled(
                workspace_id,
                profile_id,
                body.enabled,
                body.expected_version,
                grant_id=identity.grant_id,
            )
        )

    @router.get(
        "/profiles/{profile_id}/validation",
        response_model=ConfigValidation,
        operation_id="validateAgentProfile",
    )
    async def validate_profile(
        workspace_id: UUID,
        profile_id: UUID,
        request: Request,
        revision: Annotated[int, Query(ge=1)],
    ) -> ConfigValidation:
        identity = await current_human(owner, request, workspace_id)
        ref = ProfileRef(
            workspace_id=workspace_id, profile_id=profile_id, revision=revision
        )
        return await call(
            lambda: owner.agents.runtimes.validate_profile(
                ref, grant_id=identity.grant_id
            )
        )

    @router.get(
        "/profiles/{profile_id}/work",
        response_model=ProfileWorkView,
        operation_id="listProductAgentWork",
    )
    async def profile_work(
        workspace_id: UUID,
        profile_id: UUID,
        request: Request,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ProfileWorkView:
        identity = await current_human(owner, request, workspace_id)
        head = await profile_ref(workspace_id, profile_id, None, identity)
        await call(
            lambda: owner.agents.catalog.get_profile(head, grant_id=identity.grant_id)
        )
        tasks = await call(
            lambda: owner.control_plane.list_profile_work(
                workspace_id,
                profile_id,
                limit=limit,
                after=cursor,
                grant_id=identity.grant_id,
            )
        )
        conversations = await call(lambda: owner.chat.list_threads(identity))
        owned = tuple(
            ConversationWorkView(
                conversation_id=thread.id,
                title=thread.title,
                profile_ref=thread.profile_ref,
                run_id=thread.run_id,
                stop_state=thread.stop_state,
                updated_at=thread.updated_at,
            )
            for thread in conversations
            if thread.profile_ref.profile_id == profile_id
            and thread.profile_ref.workspace_id == workspace_id
        )[:limit]
        return ProfileWorkView(task_work=tasks, conversations=owned)

    @router.get(
        "/resources", response_model=ResourceListView, operation_id="listAgentResources"
    )
    async def list_resources(workspace_id: UUID, request: Request) -> ResourceListView:
        identity = await current_human(owner, request, workspace_id)
        resources = await call(
            lambda: owner.agents.catalog.list_resources(
                workspace_id, grant_id=identity.grant_id
            )
        )
        items = [
            ResourceView(
                resource=resource,
                status=await call(
                    lambda resource=resource: owner.agents.catalog.get_resource_status(
                        resource.ref, grant_id=identity.grant_id
                    )
                ),
            )
            for resource in resources
        ]
        return ResourceListView(items=tuple(items))

    @router.post(
        "/resources",
        response_model=ConfigurationResult,
        status_code=201,
        responses={202: {"model": ConfigurationResult}},
        operation_id="createAgentResource",
    )
    async def create_resource(
        workspace_id: UUID,
        body: ResourceCreateBody,
        request: Request,
        response: Response,
    ) -> ConfigurationResult:
        identity = await current_human(owner, request, workspace_id)
        result = await call(
            lambda: owner.configuration.submit(
                identity,
                action="resource.create",
                operation_id=body.operation_id,
                draft=body.draft,
            )
        )
        return _result_status(result, response, create=True)

    @router.get(
        "/resources/{resource_id}",
        response_model=ResourceView,
        operation_id="readAgentResource",
    )
    async def read_resource(
        workspace_id: UUID,
        resource_id: UUID,
        request: Request,
        revision: Annotated[int | None, Query(ge=1)] = None,
    ) -> ResourceView:
        identity = await current_human(owner, request, workspace_id)
        ref = await resource_ref(workspace_id, resource_id, revision, identity)
        resource = await call(
            lambda: owner.agents.catalog.get_resource(ref, grant_id=identity.grant_id)
        )
        status = await call(
            lambda: owner.agents.catalog.get_resource_status(
                ref, grant_id=identity.grant_id
            )
        )
        return ResourceView(resource=resource, status=status)

    @router.post(
        "/resources/{resource_id}/revisions",
        response_model=ConfigurationResult,
        responses={202: {"model": ConfigurationResult}},
        operation_id="reviseAgentResource",
    )
    async def revise_resource(
        workspace_id: UUID,
        resource_id: UUID,
        body: ResourceRevisionBody,
        request: Request,
        response: Response,
    ) -> ConfigurationResult:
        identity = await current_human(owner, request, workspace_id)
        if (
            body.expected_ref.workspace_id != workspace_id
            or body.expected_ref.resource_id != resource_id
        ):
            raise HTTPException(403, detail={"code": "permission_denied"})
        result = await call(
            lambda: owner.configuration.submit(
                identity,
                action="resource.revise",
                operation_id=body.operation_id,
                draft=body.draft,
                expected_ref=body.expected_ref,
            )
        )
        return _result_status(result, response)

    @router.put(
        "/resources/{resource_id}/status",
        response_model=ResourceStatus,
        operation_id="setAgentResourceStatus",
    )
    async def resource_status(
        workspace_id: UUID, resource_id: UUID, body: StatusBody, request: Request
    ) -> ResourceStatus:
        identity = await current_human(owner, request, workspace_id)
        return await call(
            lambda: owner.agents.catalog.set_resource_enabled(
                workspace_id,
                resource_id,
                body.enabled,
                body.expected_version,
                grant_id=identity.grant_id,
            )
        )

    @router.get(
        "/runtime-descriptors",
        response_model=tuple[RuntimeDescriptor, ...],
        operation_id="listRuntimeDescriptors",
    )
    async def descriptors(
        workspace_id: UUID, request: Request
    ) -> tuple[RuntimeDescriptor, ...]:
        identity = await current_human(owner, request, workspace_id)
        return await call(
            lambda: owner.agents.runtimes.list_descriptors(
                workspace_id, grant_id=identity.grant_id
            )
        )

    @router.get(
        "/runtimes", response_model=RuntimeListView, operation_id="listAgentRuntimes"
    )
    async def runtimes(workspace_id: UUID, request: Request) -> RuntimeListView:
        identity = await current_human(owner, request, workspace_id)
        selected = await call(
            lambda: owner.agents.runtimes.list_runtimes(
                workspace_id, grant_id=identity.grant_id
            )
        )
        await call(owner.refresh_execution_availability)
        return RuntimeListView(items=selected)

    @router.post(
        "/runtimes",
        response_model=RuntimeSnapshot,
        status_code=201,
        operation_id="registerAgentRuntime",
    )
    async def register(
        workspace_id: UUID, body: RuntimeRegisterBody, request: Request
    ) -> RuntimeSnapshot:
        identity = await current_human(owner, request, workspace_id)
        selected = await call(
            lambda: owner.agents.runtimes.register(
                workspace_id,
                body.harness_id,
                body.executable,
                body.max_active,
                body.max_sessions,
                grant_id=identity.grant_id,
            )
        )
        await call(owner.refresh_execution_availability)
        return selected

    @router.post(
        "/runtimes/discovery",
        response_model=tuple[RuntimeCandidate, ...],
        operation_id="discoverAgentRuntimes",
    )
    async def discover(
        workspace_id: UUID, request: Request
    ) -> tuple[RuntimeCandidate, ...]:
        identity = await current_human(owner, request, workspace_id)
        return await call(
            lambda: owner.agents.runtimes.discover(
                workspace_id, grant_id=identity.grant_id
            )
        )

    @router.get(
        "/runtimes/{runtime_id}",
        response_model=RuntimeSnapshot,
        operation_id="readAgentRuntime",
    )
    async def read_runtime(
        workspace_id: UUID, runtime_id: UUID, request: Request
    ) -> RuntimeSnapshot:
        identity = await current_human(owner, request, workspace_id)
        ref = RuntimeRef(workspace_id=workspace_id, runtime_id=runtime_id)
        selected = await call(
            lambda: owner.agents.runtimes.get_runtime(ref, grant_id=identity.grant_id)
        )
        await call(owner.refresh_execution_availability)
        return selected

    def exact_runtime(ref: RuntimeRef, workspace_id: UUID, runtime_id: UUID) -> None:
        if ref != RuntimeRef(workspace_id=workspace_id, runtime_id=runtime_id):
            raise HTTPException(403, detail={"code": "permission_denied"})

    @router.post(
        "/runtimes/{runtime_id}/refresh",
        response_model=RuntimeSnapshot,
        operation_id="refreshAgentRuntime",
    )
    async def refresh(
        workspace_id: UUID, runtime_id: UUID, body: RuntimeRefreshBody, request: Request
    ) -> RuntimeSnapshot:
        identity = await current_human(owner, request, workspace_id)
        exact_runtime(body.ref, workspace_id, runtime_id)
        selected = await call(
            lambda: owner.agents.runtimes.refresh(body.ref, grant_id=identity.grant_id)
        )
        await call(owner.refresh_execution_availability)
        return selected

    @router.put(
        "/runtimes/{runtime_id}/limits",
        response_model=RuntimeSnapshot,
        operation_id="setAgentRuntimeLimits",
    )
    async def limits(
        workspace_id: UUID, runtime_id: UUID, body: RuntimeLimitsBody, request: Request
    ) -> RuntimeSnapshot:
        identity = await current_human(owner, request, workspace_id)
        exact_runtime(body.ref, workspace_id, runtime_id)
        selected = await call(
            lambda: owner.agents.runtimes.set_runtime_limits(
                body.ref,
                body.max_active,
                body.max_sessions,
                body.expected_version,
                grant_id=identity.grant_id,
            )
        )
        await call(owner.refresh_execution_availability)
        return selected

    @router.put(
        "/runtimes/{runtime_id}/status",
        response_model=RuntimeSnapshot,
        operation_id="setAgentRuntimeStatus",
    )
    async def runtime_status(
        workspace_id: UUID, runtime_id: UUID, body: RuntimeStatusBody, request: Request
    ) -> RuntimeSnapshot:
        identity = await current_human(owner, request, workspace_id)
        exact_runtime(body.ref, workspace_id, runtime_id)
        selected = await call(
            lambda: owner.agents.runtimes.set_runtime_enabled(
                body.ref,
                body.enabled,
                body.expected_version,
                grant_id=identity.grant_id,
            )
        )
        await call(owner.refresh_execution_availability)
        return selected

    return router


def product_management_router(owner: ProductRuntime) -> APIRouter:
    router = APIRouter(
        prefix="/product/v1/workspaces/{workspace_id}",
        dependencies=[Depends(_no_store)],
    )

    @router.get(
        "/tool-connections",
        response_model=tuple[ToolConnectionView, ...],
        operation_id="listProductToolConnections",
    )
    async def tool_connections(
        workspace_id: UUID, request: Request
    ) -> tuple[ToolConnectionView, ...]:
        await current_human(owner, request, workspace_id)
        return (
            ToolConnectionView(
                workspace_id=owner.settings.workspace_id,
                connection_id=owner.task_tool_connections.connection_id,
                name="SIDERA Task Tools",
                availability="configured",
            ),
        )

    @router.get(
        "/configuration-requests",
        response_model=ConfigurationResult,
        responses={202: {"model": ConfigurationResult}},
        operation_id="readConfigurationRequest",
    )
    async def original(
        workspace_id: UUID,
        request: Request,
        response: Response,
        operation_id: Annotated[
            str, Query(alias="operationId", min_length=1, max_length=128)
        ],
    ) -> ConfigurationResult:
        identity = await current_human(owner, request, workspace_id)
        result = await call(
            lambda: owner.configuration.read_original(identity, operation_id)
        )
        return _result_status(result, response)

    @router.get(
        "/defaults", response_model=DefaultsView, operation_id="readProductDefaults"
    )
    async def read_defaults(workspace_id: UUID, request: Request) -> DefaultsView:
        identity = await current_human(owner, request, workspace_id)
        return await call(lambda: owner.defaults.read(identity))

    @router.put(
        "/defaults", response_model=DefaultsView, operation_id="writeProductDefaults"
    )
    async def write_defaults(
        workspace_id: UUID, body: DefaultsBody, request: Request
    ) -> DefaultsView:
        identity = await current_human(owner, request, workspace_id)
        return await call(lambda: owner.defaults.write(identity, body))

    return router
