from collections.abc import Sequence
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ContentBlock, Tool
from pydantic import AnyHttpUrl, JsonValue, SecretStr, TypeAdapter, ValidationError
from sidera_agents.errors import AgentError

from sidera_product.auth.grants import ProductGrants, VerifiedToolGrant
from sidera_product.postgres.store import ProductStoreError


class TaskTokenVerifier:
    def __init__(self, grants: ProductGrants) -> None:
        self.grants = grants

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            verified = await anyio.to_thread.run_sync(
                self.grants.verify, SecretStr(token)
            )
        except (AgentError, ProductStoreError):
            return None
        return AccessToken(
            token=token,
            client_id=str(verified.record.grant_id),
            subject=verified.record.principal_id,
            scopes=["task_tools"],
            expires_at=int(verified.record.expires_at.timestamp()),
            resource=self.grants.settings.local_url + "/product/v1/task-tools/mcp",
        )


def current_tool_token(grants: ProductGrants) -> SecretStr:
    access = get_access_token()
    if (
        access is None
        or access.resource != grants.settings.local_url + "/product/v1/task-tools/mcp"
    ):
        raise AgentError("permission_denied", "A verified task tool bearer is required")
    return SecretStr(access.token)


async def current_tool_grant(
    grants: ProductGrants, *, require_running: bool = True
) -> VerifiedToolGrant:
    token = current_tool_token(grants)
    return await anyio.to_thread.run_sync(
        lambda: grants.verify(token, require_running=require_running)
    )


def visible_tools(verified: VerifiedToolGrant) -> frozenset[str]:
    if verified.record.role == "main":
        return frozenset(
            {
                "sidera_list_profiles",
                "sidera_submit_goal",
                "sidera_get_planning",
                "sidera_get_task",
                "sidera_list_tasks",
                "sidera_request_revision",
            }
        )
    if verified.record.role == "worker":
        return frozenset({"sidera_get_task", "sidera_report_progress"})
    names = {"sidera_get_planning"}
    if verified.record.root_task_id is not None:
        names.add("sidera_get_task")
    return frozenset(names)


class TaskToolServer(FastMCP[None]):
    def __init__(self, grants: ProductGrants) -> None:
        self.grants = grants
        settings = grants.settings
        audience = settings.local_url + "/product/v1/task-tools/mcp"
        super().__init__(
            "SIDERA Task Tools",
            host=settings.bind_host,
            port=settings.port,
            streamable_http_path="/mcp",
            json_response=True,
            token_verifier=TaskTokenVerifier(grants),
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(settings.local_url),
                resource_server_url=AnyHttpUrl(audience),
                required_scopes=["task_tools"],
                validate_token_resource=True,
            ),
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=list(
                    {
                        urlsplit(settings.local_url).netloc,
                        urlsplit(settings.web_origin).netloc,
                    }
                ),
                allowed_origins=[settings.web_origin],
            ),
        )

    async def list_tools(self) -> list[Tool]:
        allowed = visible_tools(
            await current_tool_grant(self.grants, require_running=False)
        )
        return [
            tool.model_copy(
                update={
                    "inputSchema": {**tool.inputSchema, "additionalProperties": False}
                }
            )
            for tool in await super().list_tools()
            if tool.name in allowed
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, JsonValue]
    ) -> Sequence[ContentBlock] | dict[str, JsonValue]:
        if name not in visible_tools(await current_tool_grant(self.grants)):
            raise AgentError(
                "permission_denied", "The current Run cannot use this task tool"
            )
        tool = next(
            (tool for tool in await super().list_tools() if tool.name == name), None
        )
        properties = None if tool is None else tool.inputSchema.get("properties")
        if not isinstance(properties, dict):
            raise AgentError(
                "invalid_tool_arguments",
                "Task tool arguments contain unsupported fields",
            )
        try:
            property_schema = TypeAdapter(dict[str, JsonValue]).validate_python(
                properties
            )
        except ValidationError:
            raise AgentError(
                "invalid_tool_arguments",
                "Task tool arguments contain unsupported fields",
            ) from None
        if set(arguments).difference(property_schema):
            raise AgentError(
                "invalid_tool_arguments",
                "Task tool arguments contain unsupported fields",
            )
        return await super().call_tool(name, arguments)
