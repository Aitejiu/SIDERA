"""Read current native task-tool scope from CP-owned saved authority."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import NoReturn
from uuid import UUID

from sidera_agents.api import AgentCatalog
from sidera_agents.contracts import ResourceKind, ToolBinding
from sidera_agents.errors import AgentError
from sidera_agents.ports import ExecutionAuthorizer

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.agent_tools import ControlPlaneToolScope


def _deny() -> NoReturn:
    raise AgentError("permission_denied", "Native task-tool scope denied")


def pinned_task_tool_binding(
    *,
    catalog: AgentCatalog,
    authorizer: ExecutionAuthorizer,
    grant_id: UUID,
    workspace_id: UUID,
    connection_id: UUID,
    tools: tuple[ToolBinding, ...],
) -> ToolBinding:
    for binding in tools:
        if binding.connection_id is not None:
            if binding.connection_id == connection_id:
                authorizer.require(grant_id, "tool.execute", binding)
                return binding
            continue
        ref = binding.resource_ref
        if ref is None or ref.workspace_id != workspace_id:
            _deny()
        authorizer.require(grant_id, "tool.execute", binding)
        resource = catalog.get_resource(ref, grant_id=grant_id)
        status = catalog.get_resource_status(ref, grant_id=grant_id)
        if (
            resource.ref != ref
            or status.resource_id != ref.resource_id
            or not status.enabled
        ):
            _deny()
        if resource.draft.kind is not ResourceKind.MCP:
            continue
        configured = resource.draft.settings.get("connection_id")
        if not isinstance(configured, str):
            continue
        try:
            selected = UUID(configured)
        except ValueError:
            continue
        if selected == connection_id:
            return binding
    _deny()


def read_task_tool_scope(
    uow_factory: Callable[[], AbstractContextManager[ControlPlaneUnitOfWork]],
    *,
    catalog: AgentCatalog,
    authorizer: ExecutionAuthorizer,
    grant_id: UUID,
    connection_id: UUID,
) -> ControlPlaneToolScope:
    if type(grant_id) is not UUID or type(connection_id) is not UUID:
        _deny()
    with uow_factory() as uow:
        worker = uow.execution_authorities.get(grant_id)
        planner = uow.planning_authorities.get(grant_id)
        if (worker is None) == (planner is None):
            _deny()
        authority = worker if worker is not None else planner
        if authority is None:
            _deny()
        binding = pinned_task_tool_binding(
            catalog=catalog,
            authorizer=authorizer,
            grant_id=grant_id,
            workspace_id=authority.workspace_id,
            connection_id=connection_id,
            tools=authority.tools,
        )
        authorizer.require(grant_id, "profile.read", authority.profile_ref)
        profile = catalog.get_profile(authority.profile_ref, grant_id=grant_id)
        status = catalog.get_profile_status(authority.profile_ref, grant_id=grant_id)
        if (
            profile.ref != authority.profile_ref
            or status.profile_id != profile.ref.profile_id
            or not status.enabled
        ):
            _deny()
        if worker is not None:
            execution = uow.executions.get(worker.execution_id, worker.workspace_id)
            link = uow.execution_authorities.get_link(
                worker.workspace_id, worker.execution_id
            )
            if execution is None or link is None or execution.worker_item_id is None:
                _deny()
            item = uow.worker_items.get(execution.worker_item_id, worker.workspace_id)
            if (
                item is None
                or item.status.value != "active"
                or item.workspace_id != worker.workspace_id
                or link.status not in {"pending", "starting", "running", "waiting"}
            ):
                _deny()
            scope = ControlPlaneToolScope(
                worker.workspace_id,
                worker.principal_id,
                "worker",
                worker.id,
                connection_id,
                worker.profile_ref,
                link.start_operation_id,
                link.run_id,
                item.root_task_id,
                execution.id,
                item.id,
                execution.version,
                worker.binding_version,
                None,
                None,
                worker.lease_id,
                worker.fencing_token,
            )
        else:
            if planner is None:
                _deny()
            link = uow.planning_authorities.get_link(
                planner.workspace_id, planner.planning_run_id
            )
            request = uow.planning_requests.get(
                planner.planning_request_id, planner.workspace_id
            )
            if (
                link is None
                or request is None
                or link.status not in {"pending", "starting", "running"}
            ):
                _deny()
            goal = uow.task_goals.get(planner.workspace_id, request.id)
            root_id = request.root_task_id if goal is None else goal.root_task_id
            scope = ControlPlaneToolScope(
                planner.workspace_id,
                planner.principal_id,
                "planner",
                planner.id,
                connection_id,
                planner.profile_ref,
                link.start_operation_id,
                link.run_id,
                root_id,
                None,
                None,
                None,
                None,
                request.id,
                planner.planning_run_id,
                planner.lease_id,
                planner.fencing_token,
            )
        authorizer.require(grant_id, "tool.execute", binding)
        return scope
