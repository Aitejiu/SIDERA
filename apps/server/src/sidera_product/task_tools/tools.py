import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Self
from uuid import UUID

import anyio
import httpx
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    SecretStr,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)
from sidera_agents.contracts import ProfileRef, ProfileRevision
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneRuntime,
    WorkspaceSetupView,
)
from sidera_control_plane_client import (
    ActorRef,
    ControlPlaneClient,
    ControlPlaneClientConfig,
)

from sidera_product.auth.grants import (
    MainAuthorityRefs,
    ProductGrants,
    VerifiedToolGrant,
)
from sidera_product.postgres.store import PostgresProductStore

from .receipts import ToolReceipts
from .server import TaskToolServer, current_tool_grant, current_tool_token
from .transport import GrantAuth, MutationResult, OriginalCommandTransport

type NonEmptyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]


class GoalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    request_id: UUID
    title: NonEmptyText
    objective: NonEmptyText
    acceptance_criteria: tuple[NonEmptyText, ...] = Field(min_length=1)
    requested_outputs: tuple[NonEmptyText, ...] = Field(min_length=1)
    constraints: tuple[NonEmptyText, ...] = ()
    context_refs: tuple[NonEmptyText, ...] = ()
    planner_agent_ref: ProfileRef | None = None


class RevisionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    request_id: UUID
    root_task_id: UUID
    expected_version: StrictInt = Field(ge=1)
    expected_base_snapshot_id: UUID
    expected_base_generation: StrictInt = Field(ge=1)
    expected_base_content_hash: NonEmptyText
    objective: NonEmptyText
    acceptance_criteria: tuple[NonEmptyText, ...] = Field(min_length=1)
    constraints: tuple[NonEmptyText, ...] = ()
    context_refs: tuple[NonEmptyText, ...] = ()
    planner_agent_ref: ProfileRef | None = None


class ProgressArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    request_id: UUID
    summary: NonEmptyText = Field(max_length=4000)
    steps: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def bounded_finite_data(self) -> Self:
        try:
            encoded = json.dumps(
                {"summary": self.summary, "steps": self.steps},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (ValueError, TypeError):
            raise ValueError("Progress data must be finite JSON") from None
        if len(encoded) > 32768:
            raise ValueError("Progress data exceeds its declared budget")
        return self


class ToolData(RootModel[dict[str, JsonValue]]):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)


_JSON = TypeAdapter(dict[str, JsonValue])
type PublicRead = Callable[[ControlPlaneClient], Awaitable[dict[str, JsonValue]]]


class ProductTaskTools:
    def __init__(
        self,
        grants: ProductGrants,
        receipts: ToolReceipts,
        defaults: PostgresProductStore,
        control_plane: ControlPlaneRuntime,
    ) -> None:
        self.grants, self.receipts = grants, receipts
        self.defaults, self.control_plane = defaults, control_plane
        self.client_transport: httpx.AsyncBaseTransport | None = None

    @asynccontextmanager
    async def _client(
        self, verified: VerifiedToolGrant, token: SecretStr
    ) -> AsyncGenerator[ControlPlaneClient, None]:
        client = ControlPlaneClient(
            ControlPlaneClientConfig(
                control_plane_url=AnyHttpUrl(self.grants.settings.local_url),
                workspace_id=verified.record.workspace_id,
                actor=ActorRef(type="agent", id=verified.record.principal_id),
            ),
            transport=self.client_transport
            if self.client_transport is not None
            else httpx.ASGITransport(self.control_plane.app),
            auth=GrantAuth(token),
        )
        try:
            yield client
        finally:
            await client.close()

    async def _query(self, verified: VerifiedToolGrant, read: PublicRead) -> ToolData:
        async with self._client(verified, current_tool_token(self.grants)) as client:
            data = await read(client)
        current = await current_tool_grant(self.grants)
        if (
            current.run.run_id != verified.run.run_id
            or current.run.active_response_id != verified.run.active_response_id
        ):
            raise AgentError(
                "permission_denied",
                "The original task tool response is no longer current",
            )
        return ToolData.model_validate(data)

    async def list_profiles(self) -> ToolData:
        verified = await current_tool_grant(self.grants)
        if verified.record.role != "main":
            raise AgentError("permission_denied", "Only Main can list Planner choices")
        profiles = await anyio.to_thread.run_sync(lambda: self._profiles(verified))
        current = await current_tool_grant(self.grants)
        if (
            current.run.run_id != verified.run.run_id
            or current.run.active_response_id != verified.run.active_response_id
        ):
            raise AgentError(
                "permission_denied",
                "The original task tool response is no longer current",
            )
        return ToolData.model_validate(
            {
                "profiles": [
                    {
                        "profile_ref": profile.ref.model_dump(mode="json"),
                        "name": profile.draft.name,
                        "description": profile.draft.description,
                    }
                    for profile in profiles
                ]
            }
        )

    async def get_task(self, root_task_id: UUID | None = None) -> ToolData:
        verified = await current_tool_grant(self.grants)
        selected = (
            root_task_id if root_task_id is not None else verified.record.root_task_id
        )
        if type(selected) is not UUID:
            raise AgentError(
                "task_selection_required", "An exact related root task is required"
            )
        if verified.record.role != "main" and selected != verified.record.root_task_id:
            raise AgentError(
                "permission_denied", "The current Run cannot read that root task"
            )
        return await self._query(verified, lambda client: client.get_task(selected))

    async def get_planning(self, planning_request_id: UUID | None = None) -> ToolData:
        verified = await current_tool_grant(self.grants)
        if verified.record.role == "worker":
            raise AgentError(
                "permission_denied", "The current Worker cannot read planning requests"
            )
        selected = (
            planning_request_id
            if planning_request_id is not None
            else verified.record.planning_request_id
        )
        if type(selected) is not UUID:
            raise AgentError(
                "planning_selection_required", "An exact planning request is required"
            )
        if (
            verified.record.role == "planner"
            and selected != verified.record.planning_request_id
        ):
            raise AgentError(
                "permission_denied", "The current Planner cannot read that request"
            )
        return await self._query(
            verified, lambda client: client.get_planning_detail(selected)
        )

    async def list_tasks(
        self, limit: StrictInt = 20, offset: StrictInt = 0
    ) -> ToolData:
        verified = await current_tool_grant(self.grants)
        if verified.record.role != "main":
            raise AgentError("permission_denied", "Only Main can list workspace tasks")
        return await self._query(
            verified, lambda client: client.list_tasks(limit=limit, offset=offset)
        )

    def _profiles(self, verified: VerifiedToolGrant) -> tuple[ProfileRevision, ...]:
        refs = MainAuthorityRefs.model_validate(verified.record.authority_refs)
        captured = {(ref.workspace_id, ref.profile_id) for ref in refs.profiles}
        heads = self.grants.catalog.list_profiles(
            verified.record.workspace_id, grant_id=verified.record.grant_id
        )
        choices: list[ProfileRevision] = []
        for profile in heads:
            if (profile.ref.workspace_id, profile.ref.profile_id) not in captured:
                continue
            self.grants.require(verified.record.grant_id, "profile.read", profile.ref)
            status = self.grants.catalog.get_profile_status(
                profile.ref, grant_id=verified.record.grant_id
            )
            if status.profile_id == profile.ref.profile_id and status.enabled:
                choices.append(profile)
        return tuple(choices)

    def _planner(
        self, verified: VerifiedToolGrant, explicit: ProfileRef | None
    ) -> tuple[WorkspaceSetupView, ProfileRef]:
        if verified.record.role != "main":
            raise AgentError(
                "permission_denied", "Only Main can prepare new planning work"
            )
        setup = self.control_plane.get_workspace_setup(
            verified.record.workspace_id, grant_id=verified.record.grant_id
        )
        if setup is None:
            raise AgentError(
                "workspace_setup_required",
                "An explicit workspace planning setup is required",
            )
        if setup.workspace_id != verified.record.workspace_id:
            raise AgentError("permission_denied", "Workspace planning setup denied")
        selected = explicit
        if selected is None:
            defaults = self.defaults.get_defaults(verified.record.workspace_id)
            if defaults is not None and defaults.planner_profile_id is not None:
                candidates = [
                    profile.ref
                    for profile in self._profiles(verified)
                    if profile.ref.profile_id == defaults.planner_profile_id
                ]
                if len(candidates) != 1:
                    raise AgentError(
                        "planner_selection_required",
                        "The configured Planner is not currently available",
                    )
                selected = candidates[0]
            else:
                selected = setup.planner_agent_ref
        self.grants.require(verified.record.grant_id, "profile.read", selected)
        profile = self.grants.catalog.get_profile(
            selected, grant_id=verified.record.grant_id
        )
        status = self.grants.catalog.get_profile_status(
            selected, grant_id=verified.record.grant_id
        )
        if (
            profile.ref != selected
            or status.profile_id != selected.profile_id
            or not status.enabled
        ):
            raise AgentError(
                "planner_selection_required",
                "The selected Planner is not currently available",
            )
        return setup, selected

    @staticmethod
    def _envelope(
        verified: VerifiedToolGrant,
        command_id: UUID,
        command_type: str,
        payload: dict[str, JsonValue],
        *,
        target_id: UUID | None = None,
        expected_version: int | None = None,
        target_type: Literal["task", "execution"] = "task",
    ) -> dict[str, JsonValue]:
        return {
            "command_id": str(command_id),
            "command_type": command_type,
            "workspace_id": str(verified.record.workspace_id),
            "actor": {"type": "agent", "id": verified.record.principal_id},
            "target_type": "workspace" if target_id is None else target_type,
            "target_id": None if target_id is None else str(target_id),
            "expected_version": expected_version,
            "idempotency_key": str(command_id),
            "correlation_id": str(verified.run.run_id),
            "payload": payload,
        }

    async def prepare_goal(
        self,
        verified: VerifiedToolGrant,
        arguments: GoalArguments,
        command_id: UUID,
    ) -> dict[str, JsonValue]:
        setup, selected = await anyio.to_thread.run_sync(
            lambda: self._planner(verified, arguments.planner_agent_ref)
        )
        payload = _JSON.validate_python(
            arguments.model_dump(
                mode="json", exclude={"request_id", "planner_agent_ref"}
            )
        )
        payload.update(
            planning_profile_id=str(setup.planning_policy_ref.policy_id),
            planning_profile_version=setup.planning_policy_ref.version,
            planner_agent_ref=_JSON.validate_python(selected.model_dump(mode="json")),
        )
        return self._envelope(verified, command_id, "SubmitTaskGoal", payload)

    async def prepare_revision(
        self,
        verified: VerifiedToolGrant,
        arguments: RevisionArguments,
        command_id: UUID,
        *,
        client: ControlPlaneClient,
    ) -> dict[str, JsonValue]:
        if verified.record.role != "main":
            raise AgentError(
                "permission_denied", "Only Main can request a plan revision"
            )
        root = await client.get_task(arguments.root_task_id)
        head = await client.get_active_plan(arguments.root_task_id)
        if (
            root.get("id") != str(arguments.root_task_id)
            or root.get("workspace_id") != str(verified.record.workspace_id)
            or type(root.get("version")) is not int
            or root["version"] != arguments.expected_version
            or head.get("workspace_id") != str(verified.record.workspace_id)
            or head.get("root_task_id") != str(arguments.root_task_id)
            or head.get("active_snapshot_id")
            != str(arguments.expected_base_snapshot_id)
            or type(head.get("active_generation")) is not int
            or head["active_generation"] != arguments.expected_base_generation
            or head.get("active_snapshot_hash") != arguments.expected_base_content_hash
        ):
            raise AgentError(
                "revision_base_conflict",
                "The original public revision base is no longer current",
            )
        setup, selected = await anyio.to_thread.run_sync(
            lambda: self._planner(verified, arguments.planner_agent_ref)
        )
        payload = _JSON.validate_python(
            arguments.model_dump(
                mode="json",
                exclude={"request_id", "expected_version", "planner_agent_ref"},
            )
        )
        payload.update(
            planning_profile_id=str(setup.planning_policy_ref.policy_id),
            planning_profile_version=setup.planning_policy_ref.version,
            planner_agent_ref=_JSON.validate_python(selected.model_dump(mode="json")),
        )
        return self._envelope(
            verified,
            command_id,
            "SubmitPlanRevision",
            payload,
            target_id=arguments.root_task_id,
            expected_version=arguments.expected_version,
        )

    async def submit_goal(
        self,
        request_id: UUID,
        title: NonEmptyText,
        objective: NonEmptyText,
        acceptance_criteria: tuple[NonEmptyText, ...],
        requested_outputs: tuple[NonEmptyText, ...],
        constraints: tuple[NonEmptyText, ...] = (),
        context_refs: tuple[NonEmptyText, ...] = (),
        planner_agent_ref: ProfileRef | None = None,
    ) -> MutationResult:
        arguments = GoalArguments(
            request_id=request_id,
            title=title,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            requested_outputs=requested_outputs,
            constraints=constraints,
            context_refs=context_refs,
            planner_agent_ref=planner_agent_ref,
        )
        verified = await current_tool_grant(self.grants)
        token = current_tool_token(self.grants)
        async with self._client(verified, token) as client:
            return await self.receipts.execute(
                token,
                "sidera_submit_goal",
                arguments.request_id,
                _JSON.validate_python(arguments.model_dump(mode="json")),
                prepare=lambda fresh, command: self.prepare_goal(
                    fresh, arguments, command
                ),
                transport=OriginalCommandTransport(client),
            )

    async def request_revision(
        self,
        request_id: UUID,
        root_task_id: UUID,
        expected_version: StrictInt,
        expected_base_snapshot_id: UUID,
        expected_base_generation: StrictInt,
        expected_base_content_hash: NonEmptyText,
        objective: NonEmptyText,
        acceptance_criteria: tuple[NonEmptyText, ...],
        constraints: tuple[NonEmptyText, ...] = (),
        context_refs: tuple[NonEmptyText, ...] = (),
        planner_agent_ref: ProfileRef | None = None,
    ) -> MutationResult:
        arguments = RevisionArguments(
            request_id=request_id,
            root_task_id=root_task_id,
            expected_version=expected_version,
            expected_base_snapshot_id=expected_base_snapshot_id,
            expected_base_generation=expected_base_generation,
            expected_base_content_hash=expected_base_content_hash,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            context_refs=context_refs,
            planner_agent_ref=planner_agent_ref,
        )
        verified = await current_tool_grant(self.grants)
        token = current_tool_token(self.grants)
        async with self._client(verified, token) as client:
            return await self.receipts.execute(
                token,
                "sidera_request_revision",
                arguments.request_id,
                _JSON.validate_python(arguments.model_dump(mode="json")),
                prepare=lambda fresh, command: self.prepare_revision(
                    fresh, arguments, command, client=client
                ),
                transport=OriginalCommandTransport(client),
            )

    async def prepare_progress(
        self,
        verified: VerifiedToolGrant,
        arguments: ProgressArguments,
        command_id: UUID,
    ) -> dict[str, JsonValue]:
        scope = verified.scope
        response = verified.run.active_response_id
        if (
            verified.record.role != "worker"
            or scope is None
            or scope.role != "worker"
            or scope.execution_id is None
            or scope.execution_version is None
            or scope.worker_item_id is None
            or scope.binding_version is None
            or response is None
        ):
            raise AgentError(
                "permission_denied",
                "Only the current Worker response can report progress",
            )
        payload = _JSON.validate_python(
            arguments.model_dump(mode="json", exclude={"request_id"})
        )
        payload.update(
            worker_item_id=str(scope.worker_item_id),
            authority_id=str(scope.native_grant_id),
            connection_id=str(scope.connection_id),
            binding_version=scope.binding_version,
            lease_id=str(scope.lease_id),
            fencing_token=scope.fencing_token,
            run_id=str(verified.run.run_id),
            response_id=str(response),
        )
        return self._envelope(
            verified,
            command_id,
            "ReportWorkerProgress",
            payload,
            target_id=scope.execution_id,
            expected_version=scope.execution_version,
            target_type="execution",
        )

    async def report_progress(
        self,
        request_id: UUID,
        summary: NonEmptyText,
        steps: tuple[dict[str, JsonValue], ...] = (),
    ) -> MutationResult:
        arguments = ProgressArguments(
            request_id=request_id, summary=summary, steps=steps
        )
        verified = await current_tool_grant(self.grants)
        token = current_tool_token(self.grants)
        async with self._client(verified, token) as client:
            return await self.receipts.execute(
                token,
                "sidera_report_progress",
                arguments.request_id,
                _JSON.validate_python(arguments.model_dump(mode="json")),
                prepare=lambda fresh, command: self.prepare_progress(
                    fresh, arguments, command
                ),
                transport=OriginalCommandTransport(client),
            )

    def register(self, server: TaskToolServer) -> None:
        for name, callback in (
            ("sidera_list_profiles", self.list_profiles),
            ("sidera_submit_goal", self.submit_goal),
            ("sidera_get_planning", self.get_planning),
            ("sidera_get_task", self.get_task),
            ("sidera_list_tasks", self.list_tasks),
            ("sidera_request_revision", self.request_revision),
            ("sidera_report_progress", self.report_progress),
        ):
            server.add_tool(callback, name=name, structured_output=True)
