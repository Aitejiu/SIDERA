import importlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sidera_agents.contracts import ProfileStatus, RunState
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import WorkspaceSetupView
from sidera_control_plane.domain.native_setup import PlanningPolicyRef

from tests.unit.test_product_grants import grant_fixture
from tests.unit.test_tool_receipts import MemoryReceiptStore


async def authenticated_mcp_call(access, action):
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    results = []

    async def endpoint(scope, receive, send):
        results.append(await action())

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pytest.fail("The direct public-method auth harness must not send HTTP data")

    await AuthContextMiddleware(endpoint)(
        {"type": "http", "user": AuthenticatedUser(access)}, receive, send
    )
    return results[0]


def mcp_fixture(
    settings_values, tmp_path, *, role="main", state=RunState.STARTING, root_exists=True
):
    from dataclasses import replace

    from pydantic import SecretStr

    assert importlib.util.find_spec("sidera_product.task_tools.server") is not None, (
        "N18 requires the official authenticated MCP server"
    )
    module = importlib.import_module("sidera_product.task_tools.server")
    lab = grant_fixture(settings_values, tmp_path, role=role, state=state)
    if not root_exists:
        lab.scope[0] = replace(lab.scope[0], root_task_id=None)
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    server = module.TaskToolServer(lab.grants)
    calls = []

    async def owned_read() -> dict[str, str]:
        calls.append("handler")
        return {"status": "owned_read"}

    for name in (
        "sidera_list_profiles",
        "sidera_submit_goal",
        "sidera_get_planning",
        "sidera_get_task",
        "sidera_list_tasks",
        "sidera_request_revision",
        "sidera_report_progress",
        "sidera_confirm_plan",
    ):
        server.add_tool(owned_read, name=name, structured_output=True)
    return module, lab, token, server, calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    "role,root_exists,want",
    [
        (
            "main",
            True,
            {
                "sidera_list_profiles",
                "sidera_submit_goal",
                "sidera_get_planning",
                "sidera_get_task",
                "sidera_list_tasks",
                "sidera_request_revision",
            },
        ),
        ("worker", True, {"sidera_get_task", "sidera_report_progress"}),
        ("planner", True, {"sidera_get_planning", "sidera_get_task"}),
        ("planner", False, {"sidera_get_planning"}),
    ],
)
async def test_mcp_role_enumeration_uses_current_starting_grant(
    settings_values, tmp_path, role, root_exists, want
):
    module, lab, token, server, calls = mcp_fixture(
        settings_values, tmp_path, role=role, root_exists=root_exists
    )
    access = await module.TaskTokenVerifier(lab.grants).verify_token(
        token.get_secret_value()
    )
    assert access is not None
    result = await authenticated_mcp_call(access, server.list_tools)
    assert {tool.name for tool in result} == want
    assert calls == []


@pytest.mark.anyio
async def test_mcp_token_verifier_never_treats_uuid_or_profile_text_as_bearer(
    settings_values, tmp_path
):
    module, lab, token, _, _ = mcp_fixture(settings_values, tmp_path)
    verifier = module.TaskTokenVerifier(lab.grants)
    access = await verifier.verify_token(token.get_secret_value())
    assert access is not None
    assert (
        access.client_id == str(lab.owner)
        and access.subject == lab.sessions.settings.local_principal_id
    )
    assert (
        access.resource
        == lab.sessions.settings.local_url + "/product/v1/task-tools/mcp"
    )
    for forged in (
        str(lab.owner),
        str(lab.parent.session_id),
        lab.profile.draft.instructions,
        "Bearer " + token.get_secret_value(),
    ):
        assert await verifier.verify_token(forged) is None
    lab.sessions.revoke(lab.parent.session_id)
    assert await verifier.verify_token(token.get_secret_value()) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "variant", ["starting", "no_response", "wrong_role", "unregistered", "revoked"]
)
async def test_mcp_call_rechecks_running_role_before_tool_handler(
    settings_values, tmp_path, variant
):
    module, lab, token, server, calls = mcp_fixture(
        settings_values, tmp_path, role="worker", state=RunState.RUNNING
    )
    access = await module.TaskTokenVerifier(lab.grants).verify_token(
        token.get_secret_value()
    )
    assert access is not None
    name = "sidera_get_task"
    if variant == "starting":
        lab.run[0] = lab.run[0].model_copy(update={"state": RunState.STARTING})
    elif variant == "no_response":
        lab.run[0] = lab.run[0].model_copy(update={"active_response_id": None})
    elif variant == "wrong_role":
        name = "sidera_submit_goal"
    elif variant == "unregistered":
        name = "sidera_confirm_plan"
    else:
        lab.store.revoke_grant(next(iter(lab.store.rows)), now=lab.clock[0])
    with pytest.raises(AgentError):
        await authenticated_mcp_call(access, lambda: server.call_tool(name, {}))
    assert calls == []


@pytest.mark.anyio
async def test_mcp_call_derives_role_from_bearer_not_client_metadata(
    settings_values, tmp_path
):
    module, lab, token, server, calls = mcp_fixture(
        settings_values, tmp_path, role="worker", state=RunState.RUNNING
    )
    access = await module.TaskTokenVerifier(lab.grants).verify_token(
        token.get_secret_value()
    )
    assert access is not None
    access = access.model_copy(update={"client_id": "main", "subject": "user"})
    result = await authenticated_mcp_call(
        access, lambda: server.call_tool("sidera_get_task", {})
    )
    assert result is not None and calls == ["handler"]
    with pytest.raises(AgentError):
        await authenticated_mcp_call(
            access, lambda: server.call_tool("sidera_submit_goal", {})
        )
    assert calls == ["handler"]


def host_tool_fixture(settings_values, tmp_path, *, role="main"):
    from pydantic import SecretStr

    lab = grant_fixture(settings_values, tmp_path, role=role, state=RunState.RUNNING)
    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    token = SecretStr(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    lab.host.grants = lab.grants
    lab.host.tool_receipts = MemoryReceiptStore()
    return lab, token, lab.grants.verify(token, require_running=True)


def cp_read_target(verified, route, **paths):
    from sidera_control_plane.application.request_authority import (
        ControlPlaneRequestTarget,
    )

    return ControlPlaneRequestTarget(
        verified.record.workspace_id,
        verified.record.principal_id,
        "GET",
        route,
        tuple((key, str(value)) for key, value in paths.items()),
        (("workspace_id", str(verified.record.workspace_id)),),
    )


@pytest.mark.parametrize("role", ["main", "worker", "planner"])
def test_direct_cp_bearer_authenticates_actual_run_as_agent_not_parent(
    settings_values, tmp_path, role
):
    from starlette.requests import Request

    lab, token, verified = host_tool_fixture(settings_values, tmp_path, role=role)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tasks",
            "headers": [
                (b"authorization", ("Bearer " + token.get_secret_value()).encode()),
                (
                    b"host",
                    lab.sessions.settings.local_url.removeprefix("http://").encode(),
                ),
            ],
        }
    )
    assert lab.host.authenticate_request(request) == verified.identity
    assert (
        verified.identity.actor_type == "agent"
        and verified.identity.grant_id != lab.parent.session_id
    )
    lab.run[0] = lab.run[0].model_copy(update={"state": RunState.STARTING})
    assert lab.host.authenticate_request(request) is None


@pytest.mark.parametrize("role", ["main", "worker", "planner"])
def test_direct_cp_role_reads_exact_related_public_targets(
    settings_values, tmp_path, role
):
    lab, _, verified = host_tool_fixture(settings_values, tmp_path, role=role)
    root = uuid4() if role == "main" else verified.record.root_task_id
    lab.host.require(
        verified.record.grant_id,
        "control_plane.query",
        cp_read_target(verified, "/api/v1/tasks/{task_id}", task_id=root),
    )
    if role == "main":
        lab.host.require(
            verified.record.grant_id,
            "control_plane.query",
            cp_read_target(verified, "/api/v1/tasks"),
        )
        lab.host.require(
            verified.record.grant_id,
            "workspace.setup.read",
            verified.record.workspace_id,
        )
    elif role == "worker":
        lab.host.require(
            verified.record.grant_id,
            "control_plane.query",
            cp_read_target(
                verified,
                "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace",
                workspace_id=verified.record.workspace_id,
                worker_item_id=verified.record.worker_item_id,
            ),
        )
    else:
        lab.host.require(
            verified.record.grant_id,
            "control_plane.query",
            cp_read_target(
                verified,
                "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}",
                workspace_id=verified.record.workspace_id,
                request_id=verified.record.planning_request_id,
            ),
        )


@pytest.mark.parametrize(
    "variant",
    [
        "foreign_root",
        "foreign_worker",
        "all_tasks",
        "planning",
        "raw_execution",
        "receipt",
        "foreign_workspace",
        "wrong_principal",
    ],
)
def test_direct_worker_cp_queries_cannot_disclose_unrelated_data(
    settings_values, tmp_path, variant
):
    from dataclasses import replace

    lab, _, verified = host_tool_fixture(settings_values, tmp_path, role="worker")
    target = cp_read_target(
        verified, "/api/v1/tasks/{task_id}", task_id=verified.record.root_task_id
    )
    if variant == "foreign_root":
        target = cp_read_target(verified, "/api/v1/tasks/{task_id}", task_id=uuid4())
    elif variant == "foreign_worker":
        target = cp_read_target(
            verified,
            "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace",
            workspace_id=verified.record.workspace_id,
            worker_item_id=uuid4(),
        )
    elif variant == "all_tasks":
        target = cp_read_target(verified, "/api/v1/tasks")
    elif variant == "planning":
        target = cp_read_target(
            verified,
            "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}",
            workspace_id=verified.record.workspace_id,
            request_id=uuid4(),
        )
    elif variant == "raw_execution":
        target = cp_read_target(
            verified,
            "/api/v1/executions/{execution_id}",
            execution_id=verified.record.execution_id,
        )
    elif variant == "receipt":
        target = cp_read_target(
            verified,
            "/api/v1/workspaces/{workspace_id}/commands/{command_id}",
            workspace_id=verified.record.workspace_id,
            command_id=uuid4(),
        )
    elif variant == "foreign_workspace":
        target = replace(target, workspace_id=uuid4())
    else:
        target = replace(target, principal_id="not-the-saved-principal")
    with pytest.raises(AgentError):
        lab.host.require(verified.record.grant_id, "control_plane.query", target)


def test_direct_cp_receipt_read_requires_original_owned_run_and_response(
    settings_values, tmp_path
):
    from sidera_product.postgres.authority_store import ToolReceiptRecord

    lab, _, verified = host_tool_fixture(settings_values, tmp_path)
    command, logical = uuid4(), uuid4()
    row = ToolReceiptRecord(
        uuid4(),
        verified.record.grant_id,
        verified.run.run_id,
        verified.run.active_response_id,
        "sidera_submit_goal",
        logical,
        "a" * 64,
        command,
        {},
        "unknown",
        None,
        lab.clock[0],
        lab.clock[0],
    )
    lab.host.tool_receipts.save_receipt(row)
    target = cp_read_target(
        verified,
        "/api/v1/workspaces/{workspace_id}/commands/{command_id}",
        workspace_id=verified.record.workspace_id,
        command_id=command,
    )
    lab.host.require(verified.record.grant_id, "control_plane.query", target)
    lab.run[0] = lab.run[0].model_copy(update={"active_response_id": uuid4()})
    with pytest.raises(AgentError):
        lab.host.require(verified.record.grant_id, "control_plane.query", target)


@pytest.mark.parametrize("role", ["main", "worker", "planner"])
def test_direct_cp_agent_never_inherits_human_or_raw_run_actions(
    settings_values, tmp_path, role
):
    from dataclasses import replace

    from sidera_control_plane.domain.commands import CommandEnvelope

    lab, _, verified = host_tool_fixture(settings_values, tmp_path, role=role)
    base = cp_read_target(verified, "/api/v1/commands")
    for name in (
        "ConfirmPlanningProposal",
        "SetWorkerItemAssignee",
        "CancelPlanningRequest",
        "SendExecutionInput",
    ):
        command_id = uuid4()
        command = CommandEnvelope(
            id=command_id,
            workspace_id=verified.record.workspace_id,
            command_type=name,
            target_type="workspace",
            actor_type="agent",
            actor_id=verified.record.principal_id,
            target_id=None,
            payload={},
            expected_version=None,
            idempotency_key=str(command_id),
            correlation_id=verified.run.run_id,
        )
        with pytest.raises(AgentError):
            lab.host.require(
                verified.record.grant_id,
                "control_plane.command:" + name,
                replace(base, method="POST", command=command),
            )
    for action in ("run.start", "run.input", "run.stop", "workspace.configure"):
        with pytest.raises(AgentError):
            lab.host.require(
                verified.record.grant_id, action, verified.record.workspace_id
            )


def test_direct_main_planning_submit_requires_captured_current_parent_profile(
    settings_values, tmp_path
):
    from sidera_control_plane.application.planning_commands import PlanningCommandTarget

    lab, _, verified = host_tool_fixture(settings_values, tmp_path)
    target = PlanningCommandTarget(
        verified.record.workspace_id,
        verified.record.principal_id,
        None,
        uuid4(),
        1,
        lab.profile.ref,
        None,
        (),
    )
    lab.host.require(verified.record.grant_id, "planning.submit", target)
    from dataclasses import replace

    for changed in (
        replace(
            target,
            planner_agent_ref=lab.profile.ref.model_copy(
                update={"profile_id": uuid4()}
            ),
        ),
        replace(target, planning_request_id=uuid4()),
        replace(target, principal_id="forged"),
    ):
        with pytest.raises(AgentError):
            lab.host.require(verified.record.grant_id, "planning.submit", changed)
    with pytest.raises(AgentError):
        lab.host.require(verified.record.grant_id, "planning.confirm", target)


def test_direct_worker_progress_uses_fresh_version_and_all_immutable_ownership_pins(
    settings_values, tmp_path
):
    from dataclasses import replace

    from sidera_control_plane.composition.native_runtime import WorkerProgressTarget

    lab, _, verified = host_tool_fixture(settings_values, tmp_path, role="worker")
    lab.scope[0] = replace(
        lab.scope[0], execution_version=4, run_id=verified.run.run_id
    )
    scope = lab.scope[0]
    target = WorkerProgressTarget(
        scope.workspace_id,
        scope.principal_id,
        scope.execution_id,
        scope.worker_item_id,
        scope.native_grant_id,
        scope.connection_id,
        4,
        scope.binding_version,
        scope.lease_id,
        scope.fencing_token,
        verified.run.run_id,
        verified.run.active_response_id,
    )
    lab.host.require(verified.record.grant_id, "progress.report", target)
    for field, value in (
        ("execution_version", 3),
        ("binding_version", 3),
        ("fencing_token", 8),
        ("native_grant_id", uuid4()),
        ("connection_id", uuid4()),
        ("worker_item_id", uuid4()),
        ("execution_id", uuid4()),
        ("lease_id", uuid4()),
        ("run_id", uuid4()),
        ("response_id", uuid4()),
        ("workspace_id", uuid4()),
        ("principal_id", "forged"),
    ):
        with pytest.raises(AgentError):
            lab.host.require(
                verified.record.grant_id,
                "progress.report",
                replace(target, **{field: value}),
            )


def tools_module():
    assert importlib.util.find_spec("sidera_product.task_tools.tools") is not None, (
        "N18 must validate actual task tool arguments"
    )
    return importlib.import_module("sidera_product.task_tools.tools")


def goal_arguments():
    return {
        "request_id": str(uuid4()),
        "title": "Explicit owned goal",
        "objective": "Short real planning receipt",
        "acceptance_criteria": ["Exactly one logical intent"],
        "requested_outputs": ["Owned output"],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "not-a-uuid"),
        ("title", " "),
        ("objective", ""),
        ("acceptance_criteria", []),
        ("requested_outputs", []),
        ("constraints", [""]),
        ("workspace_id", str(uuid4())),
        ("grant_id", str(uuid4())),
        ("actor_type", "user"),
    ],
)
def test_goal_arguments_reject_empty_content_and_client_authority(field, value):
    module = tools_module()
    with pytest.raises(ValidationError):
        module.GoalArguments.model_validate({**goal_arguments(), field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "not revision"),
        ("requested_outputs", ["not revision"]),
        ("expected_version", True),
        ("expected_base_generation", 0),
    ],
)
def test_revision_arguments_follow_existing_exact_public_shape(field, value):
    module = tools_module()
    arguments = {
        "request_id": str(uuid4()),
        "root_task_id": str(uuid4()),
        "expected_version": 4,
        "expected_base_snapshot_id": str(uuid4()),
        "expected_base_generation": 2,
        "expected_base_content_hash": "a" * 64,
        "objective": "Bounded revision",
        "acceptance_criteria": ["Original immutable base remains exact"],
    }
    with pytest.raises(ValidationError):
        module.RevisionArguments.model_validate({**arguments, field: value})


def test_goal_and_revision_schemas_never_offer_privileged_identity_fields():
    module = tools_module()
    for schema in (
        module.GoalArguments.model_json_schema(),
        module.RevisionArguments.model_json_schema(),
    ):
        properties = schema["properties"]
        assert "request_id" in properties and "planner_agent_ref" in properties
        assert not {
            "workspace_id",
            "actor_type",
            "grant_id",
            "authority_id",
            "run_id",
            "response_id",
            "planning_profile_id",
        }.intersection(properties)
        assert schema["additionalProperties"] is False


def preparation_fixture(settings_values, tmp_path):
    module = tools_module()
    lab = grant_fixture(
        settings_values, tmp_path, state=RunState.RUNNING, catalogue=True
    )
    lab.grants.resolve(lab.owner, lab.grants.connection_id)
    lab.catalog.get_profile = lambda ref, **_: next(
        profile for profile in lab.available if profile.ref == ref
    )
    lab.catalog.get_profile_status = lambda ref, **_: ProfileStatus(
        profile_id=ref.profile_id, enabled=True, version=1
    )
    setup = WorkspaceSetupView(
        lab.profile.ref.workspace_id,
        1,
        PlanningPolicyRef(uuid4(), 1),
        "saved-opaque-repository",
        lab.profile.ref,
    )
    control_plane = SimpleNamespace(get_workspace_setup=lambda workspace, **_: setup)
    defaults = SimpleNamespace(get_defaults=lambda workspace: None)
    from sidera_product.task_tools.receipts import ToolReceipts

    assert hasattr(module, "ProductTaskTools"), (
        "N18 requires public setup/command preparation"
    )
    tools = module.ProductTaskTools(
        lab.grants,
        ToolReceipts(lab.grants, MemoryReceiptStore()),
        defaults,
        control_plane,
    )
    return module, lab, tools, defaults, control_plane, setup


@pytest.mark.anyio
async def test_goal_preparation_pins_exact_policy_and_new_authorized_captured_head(
    settings_values, tmp_path
):
    module, lab, tools, defaults, _, setup = preparation_fixture(
        settings_values, tmp_path
    )
    b2 = lab.available[1].model_copy(
        update={"ref": lab.available[1].ref.model_copy(update={"revision": 2})}
    )
    lab.available[1] = b2
    defaults.get_defaults = lambda _: SimpleNamespace(
        planner_profile_id=b2.ref.profile_id
    )
    verified = lab.grants.verify_grant(lab.owner)
    command = uuid4()
    body = await tools.prepare_goal(
        verified, module.GoalArguments.model_validate(goal_arguments()), command
    )
    assert body["command_id"] == str(command) and body["actor"] == {
        "type": "agent",
        "id": lab.sessions.settings.local_principal_id,
    }
    assert body["workspace_id"] == str(lab.profile.ref.workspace_id)
    payload = body["payload"]
    assert payload["planner_agent_ref"] == b2.ref.model_dump(mode="json")
    assert payload["planning_profile_id"] == str(setup.planning_policy_ref.policy_id)
    assert payload["planning_profile_version"] == setup.planning_policy_ref.version
    assert setup.planner_agent_ref == lab.profile.ref
    assert "request_id" not in payload and "grant_id" not in payload
    assert [call[0] for call in lab.calls].count("get_start") >= 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "variant",
    ["missing_setup", "missing_default", "new_identity", "disabled", "foreign"],
)
async def test_goal_selection_fails_closed_without_first_row_fallback(
    settings_values, tmp_path, variant
):
    module, lab, tools, defaults, cp, setup = preparation_fixture(
        settings_values, tmp_path
    )
    arguments = goal_arguments()
    if variant == "missing_setup":
        cp.get_workspace_setup = lambda *_, **__: None
    elif variant == "missing_default":
        defaults.get_defaults = lambda _: SimpleNamespace(planner_profile_id=uuid4())
    elif variant == "new_identity":
        extra = lab.profile.model_copy(
            update={"ref": lab.profile.ref.model_copy(update={"profile_id": uuid4()})}
        )
        lab.available.append(extra)
        arguments["planner_agent_ref"] = extra.ref.model_dump(mode="json")
    elif variant == "disabled":
        lab.catalog.get_profile_status = lambda ref, **_: ProfileStatus(
            profile_id=ref.profile_id, enabled=False, version=2
        )
    else:
        arguments["planner_agent_ref"] = setup.planner_agent_ref.model_copy(
            update={"workspace_id": uuid4()}
        ).model_dump(mode="json")
    with pytest.raises(AgentError):
        await tools.prepare_goal(
            lab.grants.verify_grant(lab.owner),
            module.GoalArguments.model_validate(arguments),
            uuid4(),
        )


@pytest.mark.anyio
async def test_revision_preparation_revalidates_actual_public_root_and_exact_head(
    settings_values, tmp_path
):
    from sidera_control_plane_client import (
        ActorRef,
        ControlPlaneClient,
        ControlPlaneClientConfig,
    )

    module, lab, tools, _, _, setup = preparation_fixture(settings_values, tmp_path)
    root, snapshot, command = uuid4(), uuid4(), uuid4()
    calls = []

    def reply(request):
        calls.append(request.url.path)
        if request.url.path == f"/api/v1/tasks/{root}":
            return httpx.Response(
                200,
                json={
                    "id": str(root),
                    "workspace_id": str(lab.profile.ref.workspace_id),
                    "version": 4,
                },
            )
        return httpx.Response(
            200,
            json={
                "workspace_id": str(lab.profile.ref.workspace_id),
                "root_task_id": str(root),
                "active_snapshot_id": str(snapshot),
                "active_generation": 2,
                "active_snapshot_hash": "a" * 64,
            },
        )

    client = ControlPlaneClient(
        ControlPlaneClientConfig(
            control_plane_url=lab.sessions.settings.local_url,
            workspace_id=lab.profile.ref.workspace_id,
            actor=ActorRef(type="agent", id=lab.sessions.settings.local_principal_id),
        ),
        transport=httpx.MockTransport(reply),
    )
    arguments = module.RevisionArguments(
        request_id=uuid4(),
        root_task_id=root,
        expected_version=4,
        expected_base_snapshot_id=snapshot,
        expected_base_generation=2,
        expected_base_content_hash="a" * 64,
        objective="Bounded revision",
        acceptance_criteria=("Keep exact base",),
    )
    try:
        body = await tools.prepare_revision(
            lab.grants.verify_grant(lab.owner), arguments, command, client=client
        )
        assert (
            body["target_type"] == "task"
            and body["target_id"] == str(root)
            and body["expected_version"] == 4
        )
        payload = body["payload"]
        assert (
            payload["expected_base_snapshot_id"] == str(snapshot)
            and payload["expected_base_generation"] == 2
        )
        assert payload["planning_profile_id"] == str(
            setup.planning_policy_ref.policy_id
        )
        assert "title" not in payload and "requested_outputs" not in payload
        assert calls == [
            f"/api/v1/tasks/{root}",
            f"/api/v1/workspaces/{lab.profile.ref.workspace_id}/tasks/{root}/active-plan",
        ]
        with pytest.raises(AgentError):
            await tools.prepare_revision(
                lab.grants.verify_grant(lab.owner),
                arguments.model_copy(update={"expected_version": 5}),
                uuid4(),
                client=client,
            )
    finally:
        await client.close()


async def function_access(lab):
    from sidera_product.task_tools.server import TaskTokenVerifier

    connection = lab.grants.resolve(lab.owner, lab.grants.connection_id)
    access = await TaskTokenVerifier(lab.grants).verify_token(
        connection.headers["Authorization"].get_secret_value().removeprefix("Bearer ")
    )
    assert access is not None
    return access


@pytest.mark.anyio
async def test_profile_query_rejects_response_changed_during_catalogue_read(
    settings_values, tmp_path
):
    _, lab, tools, _, _, _ = preparation_fixture(settings_values, tmp_path)
    access = await function_access(lab)
    original_reader = lab.catalog.list_profiles
    original_response = lab.run[0].active_response_id

    def late_catalogue_result(*args, **kwargs):
        result = original_reader(*args, **kwargs)
        lab.run[0] = lab.run[0].model_copy(update={"active_response_id": uuid4()})
        return result

    lab.catalog.list_profiles = late_catalogue_result
    with pytest.raises(AgentError) as error:
        await authenticated_mcp_call(access, tools.list_profiles)
    assert error.value.code == "permission_denied"
    assert lab.run[0].active_response_id != original_response
    assert lab.run[0].state is RunState.RUNNING


@pytest.mark.anyio
async def test_public_profile_tool_filters_new_identity_and_returns_current_captured_head(
    settings_values, tmp_path
):
    _, lab, tools, _, _, _ = preparation_fixture(settings_values, tmp_path)
    old = dict(lab.store.rows)
    b2 = lab.available[1].model_copy(
        update={"ref": lab.available[1].ref.model_copy(update={"revision": 2})}
    )
    c = lab.profile.model_copy(
        update={"ref": lab.profile.ref.model_copy(update={"profile_id": uuid4()})}
    )
    lab.available[1] = b2
    lab.available.append(c)
    assert hasattr(tools, "list_profiles"), (
        "The actual MCP profile function is required"
    )
    result = await authenticated_mcp_call(
        await function_access(lab), tools.list_profiles
    )
    body = result.model_dump(mode="json")
    assert {row["profile_ref"]["profile_id"] for row in body["profiles"]} == {
        str(lab.profile.ref.profile_id),
        str(b2.ref.profile_id),
    }
    assert (
        next(
            row
            for row in body["profiles"]
            if row["profile_ref"]["profile_id"] == str(b2.ref.profile_id)
        )["profile_ref"]["revision"]
        == 2
    )
    assert next(iter(lab.store.rows.values())).profile_ref == lab.profile.ref
    assert (
        next(iter(old.values())).authority_refs
        == next(iter(lab.store.rows.values())).authority_refs
    )


@pytest.mark.anyio
async def test_public_goal_function_replay_keeps_old_intent_new_goal_pins_new_head(
    settings_values, tmp_path
):
    import json

    _, lab, tools, defaults, _, setup = preparation_fixture(settings_values, tmp_path)
    bodies = []

    def reply(request):
        assert request.method == "POST" and request.headers["authorization"].startswith(
            "Bearer "
        )
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            201,
            json={
                "command_id": body["command_id"],
                "aggregate_id": str(uuid4()),
                "aggregate_version": 1,
                "event_ids": [],
                "data": {
                    "root_task_id": str(uuid4()),
                    "planning_request_id": str(uuid4()),
                    "status": "pending_planning",
                },
            },
        )

    tools.client_transport = httpx.MockTransport(reply)
    defaults.get_defaults = lambda _: SimpleNamespace(
        planner_profile_id=lab.available[1].ref.profile_id
    )
    original_ref = lab.available[1].ref
    arguments = goal_arguments()
    assert hasattr(tools, "submit_goal"), (
        "The actual short-receipt goal function is required"
    )
    access = await function_access(lab)
    first = await authenticated_mcp_call(access, lambda: tools.submit_goal(**arguments))
    b2 = lab.available[1].model_copy(
        update={"ref": lab.available[1].ref.model_copy(update={"revision": 2})}
    )
    lab.available[1] = b2
    replay = await authenticated_mcp_call(
        access, lambda: tools.submit_goal(**arguments)
    )
    fresh = await authenticated_mcp_call(
        access, lambda: tools.submit_goal(**{**arguments, "request_id": str(uuid4())})
    )
    assert first.outcome == fresh.outcome == "applied" and replay == first
    assert len(bodies) == 2 and first.command_id != fresh.command_id
    assert bodies[0]["payload"]["planner_agent_ref"] == original_ref.model_dump(
        mode="json"
    )
    assert bodies[1]["payload"]["planner_agent_ref"] == b2.ref.model_dump(mode="json")
    assert all(
        body["payload"]["planning_profile_id"]
        == str(setup.planning_policy_ref.policy_id)
        for body in bodies
    )
    assert all(
        body["actor"]
        == {"type": "agent", "id": lab.sessions.settings.local_principal_id}
        for body in bodies
    )
    assert len(tools.receipts.store.rows) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["worker", "planner"])
async def test_public_tool_queries_validate_related_ids_before_public_client_io(
    settings_values, tmp_path, role
):
    from sidera_product.task_tools.receipts import ToolReceipts

    module = tools_module()
    lab, _, verified = host_tool_fixture(settings_values, tmp_path, role=role)
    requests = []

    def reply(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "id": str(verified.record.root_task_id),
                "workspace_id": str(verified.record.workspace_id),
                "version": 3,
            },
        )

    tools = module.ProductTaskTools(
        lab.grants,
        ToolReceipts(lab.grants, MemoryReceiptStore()),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    tools.client_transport = httpx.MockTransport(reply)
    assert hasattr(tools, "get_task"), (
        "The actual scoped task query function is required"
    )
    access = await function_access(lab)
    result = await authenticated_mcp_call(access, lambda: tools.get_task())
    assert result.model_dump()["id"] == str(verified.record.root_task_id)
    with pytest.raises(AgentError):
        await authenticated_mcp_call(
            access, lambda: tools.get_task(root_task_id=uuid4())
        )
    assert requests == [f"/api/v1/tasks/{verified.record.root_task_id}"]
    if role == "planner":
        await authenticated_mcp_call(access, lambda: tools.get_planning())
        with pytest.raises(AgentError):
            await authenticated_mcp_call(
                access, lambda: tools.get_planning(planning_request_id=uuid4())
            )
        assert (
            requests[-1]
            == f"/api/v1/workspaces/{verified.record.workspace_id}/planning-requests/{verified.record.planning_request_id}"
        )


@pytest.mark.anyio
async def test_mcp_call_rejects_extra_authority_argument_before_handler(
    settings_values, tmp_path
):
    module, lab, token, server, calls = mcp_fixture(
        settings_values, tmp_path, state=RunState.RUNNING
    )
    access = await module.TaskTokenVerifier(lab.grants).verify_token(
        token.get_secret_value()
    )
    assert access is not None
    with pytest.raises(AgentError):
        await authenticated_mcp_call(
            access,
            lambda: server.call_tool(
                "sidera_list_profiles", {"grant_id": str(lab.parent.session_id)}
            ),
        )
    assert calls == []


@pytest.mark.anyio
async def test_mcp_call_rejects_non_json_properties_before_handler(
    settings_values,
    tmp_path,
    no_database_connections,
    monkeypatch,
):
    from mcp.types import Tool

    module, lab, token, server, calls = mcp_fixture(
        settings_values,
        tmp_path,
        state=RunState.RUNNING,
    )
    access = await module.TaskTokenVerifier(lab.grants).verify_token(
        token.get_secret_value()
    )
    assert access is not None

    async def observed_schema(current):
        assert current is server
        return [
            Tool(
                name="sidera_list_profiles",
                inputSchema={
                    "type": "object",
                    "properties": {"invalid": object()},
                },
            )
        ]

    monkeypatch.setattr(module.FastMCP, "list_tools", observed_schema)
    with pytest.raises(AgentError) as caught:
        await authenticated_mcp_call(
            access, lambda: server.call_tool("sidera_list_profiles", {})
        )
    assert caught.value.code == "invalid_tool_arguments"
    assert calls == []


@pytest.mark.anyio
async def test_actual_cold_app_mounts_one_public_mcp_and_explicit_manager_lifetime(
    settings_values, no_database_connections, monkeypatch
):
    from contextlib import asynccontextmanager

    from sidera_product.app import create_app
    from sidera_product.composition import build_product_runtime
    from sidera_product.config import ProductSettings

    runtime = build_product_runtime(ProductSettings.model_validate(settings_values))
    try:
        app = create_app(runtime=runtime)
        assert hasattr(app.state, "task_tool_server"), (
            "Product lifespan must own the mounted public MCP session manager"
        )
        server = app.state.task_tool_server
        observed = []
        original_manager = server.session_manager.run
        original_close = runtime.close

        @asynccontextmanager
        async def manager():
            observed.append("manager_enter")
            async with original_manager():
                yield
            observed.append("manager_exit")

        def close(*, grace_seconds=0):
            observed.append("owner_close")
            return original_close(grace_seconds=grace_seconds)

        monkeypatch.setattr(server.session_manager, "run", manager)
        monkeypatch.setattr(runtime, "start", lambda: observed.append("owner_start"))
        monkeypatch.setattr(runtime, "close", close)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=runtime.settings.local_url
            ) as client:
                response = await client.get(
                    "/product/v1/task-tools/mcp",
                    headers={"Accept": "text/event-stream"},
                )
                assert response.status_code == 401 and response.headers[
                    "www-authenticate"
                ].startswith("Bearer ")
            observed.append("request_done")
        assert observed == [
            "manager_enter",
            "owner_start",
            "request_done",
            "owner_close",
            "manager_exit",
        ]
    finally:
        assert runtime.close(grace_seconds=0) is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", ""),
        ("summary", "a" * 4001),
        ("steps", [{}] * 65),
        ("steps", [{"fraction": float("nan")}]),
        ("steps", [{"detail": "a" * 32768}]),
        ("run_id", str(uuid4())),
        ("response_id", str(uuid4())),
        ("execution_id", str(uuid4())),
    ],
)
def test_progress_arguments_reject_privileged_identity_nonfinite_and_oversized_data(
    field, value
):
    module = tools_module()
    assert hasattr(module, "ProgressArguments"), (
        "The report tool requires bounded finite worker-defined data"
    )
    with pytest.raises((ValidationError, AgentError)):
        module.ProgressArguments.model_validate(
            {
                "request_id": str(uuid4()),
                "summary": "Unverified worker report",
                field: value,
            }
        )


@pytest.mark.anyio
async def test_public_progress_function_fills_fresh_scope_pins_not_mint_version(
    settings_values, tmp_path
):
    import json
    from dataclasses import replace

    from sidera_product.task_tools.receipts import ToolReceipts

    module = tools_module()
    lab, _, verified = host_tool_fixture(settings_values, tmp_path, role="worker")
    lab.scope[0] = replace(
        lab.scope[0], execution_version=4, run_id=verified.run.run_id
    )
    bodies = []

    def reply(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            201,
            json={
                "command_id": body["command_id"],
                "aggregate_id": str(lab.scope[0].execution_id),
                "aggregate_version": 4,
                "event_ids": [str(uuid4())],
                "data": {"status": "progress_reported", "report_id": str(uuid4())},
            },
        )

    tools = module.ProductTaskTools(
        lab.grants,
        ToolReceipts(lab.grants, MemoryReceiptStore()),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    tools.client_transport = httpx.MockTransport(reply)
    assert hasattr(tools, "report_progress"), (
        "The Worker progress function must fill immutable scope fields"
    )
    result = await authenticated_mcp_call(
        await function_access(lab),
        lambda: tools.report_progress(
            request_id=uuid4(),
            summary="Unverified work in progress",
            steps=({"custom": {"state": "running"}},),
        ),
    )
    assert result.outcome == "applied" and len(bodies) == 1
    body = bodies[0]
    assert body["target_type"] == "execution" and body["target_id"] == str(
        lab.scope[0].execution_id
    )
    assert (
        body["expected_version"] == 4
        and next(iter(lab.store.rows.values())).execution_version == 3
    )
    assert body["payload"] == {
        "worker_item_id": str(lab.scope[0].worker_item_id),
        "authority_id": str(lab.scope[0].native_grant_id),
        "connection_id": str(lab.scope[0].connection_id),
        "binding_version": lab.scope[0].binding_version,
        "lease_id": str(lab.scope[0].lease_id),
        "fencing_token": lab.scope[0].fencing_token,
        "run_id": str(verified.run.run_id),
        "response_id": str(verified.run.active_response_id),
        "summary": "Unverified work in progress",
        "steps": [{"custom": {"state": "running"}}],
    }


def test_task_tool_harness_has_distinct_exact_memory_installation_limits(
    no_database_connections,
):
    import sys

    from sidera_agents.contracts import RuntimeRef
    from sidera_agents.harnesses.registry import HarnessRegistry
    from sidera_agents.memory import MemoryAgentStore
    from sidera_agents.runtimes import RuntimeCatalog

    from tests.integration.test_task_tool_progress import ToolJourneyHarness
    from tests.support.controlled_native import ControlledHarness

    store, old_native, native = (
        MemoryAgentStore(),
        ControlledHarness(),
        ToolJourneyHarness(),
    )
    grant, old_workspace, workspace = uuid4(), uuid4(), uuid4()
    authorizations = []

    class Authorizer:
        def require(self, grant_id, permission, target):
            assert grant_id == grant
            assert permission in {"runtime.register", "runtime.read"}
            if permission == "runtime.read":
                assert isinstance(target, RuntimeRef)
                assert target.workspace_id in {old_workspace, workspace}
            else:
                assert target in {old_workspace, workspace}
            authorizations.append((permission, target))

    old_catalog = RuntimeCatalog(
        store, Authorizer(), registry=HarnessRegistry((old_native,))
    )
    old = old_catalog.register(
        old_workspace, old_native.harness_id, sys.executable, 2, 2, grant_id=grant
    )
    reused = old_catalog.register(
        workspace, old_native.harness_id, sys.executable, 1, 2, grant_id=grant
    )
    assert old.limits == reused.limits == {"max_active": 2, "max_sessions": 2}
    assert native.harness_id == "controlled-task-tools"
    catalog = RuntimeCatalog(
        store, Authorizer(), registry=HarnessRegistry((old_native, native))
    )
    isolated = catalog.register(
        workspace, native.harness_id, sys.executable, 1, 2, grant_id=grant
    )
    assert isolated.limits == {"max_active": 1, "max_sessions": 2}
    assert old_catalog.get_runtime(old.ref, grant_id=grant) == old
    assert old_catalog.get_runtime(reused.ref, grant_id=grant) == reused
    old_key = store.get_runtime_entry(old.ref).installation.installation_key
    assert store.get_runtime_entry(reused.ref).installation.installation_key == old_key
    assert (
        store.get_runtime_entry(isolated.ref).installation.installation_key != old_key
    )
    assert isolated.ref != reused.ref
    assert len(authorizations) == 5
    assert [call[0] for call in native.calls] == ["probe"]
    assert [call[0] for call in old_native.calls] == ["probe", "probe"]


@pytest.mark.parametrize("outcome", ("stopped", "unknown", "transport_unknown"))
def test_composed_main_cleanup_submits_original_stop_once_and_queries_original_receipt(
    no_database_connections, tmp_path, outcome
):
    import json

    from sidera_agents.contracts import RunStop, StopReceipt, StopStatus

    from tests.integration.test_task_tool_progress import (
        TaskToolLab,
        ToolJourneyHarness,
    )

    original = RunStop(
        workspace_id=uuid4(), run_id=uuid4(), operation_id=uuid4(), grant_id=uuid4()
    )
    calls = []
    queried_status = [
        StopStatus.STOPPED if outcome == "stopped" else StopStatus.UNKNOWN
    ]

    def receipt(status):
        return StopReceipt(
            operation_id=original.operation_id,
            run_id=original.run_id,
            status=status,
            reason_code=None
            if status is StopStatus.STOPPED
            else "native_close_unknown",
            evidence_sequence=1,
        )

    def stop(request):
        assert request is original and lab.main_stop_submitted
        calls.append(("stop", request))
        if outcome == "transport_unknown":
            raise httpx.ReadError("private transport detail must not enter evidence")
        return receipt(
            StopStatus.STOPPED if outcome == "stopped" else StopStatus.UNKNOWN
        )

    def get_stop(workspace, run, operation, *, grant_id):
        assert (workspace, run, operation, grant_id) == (
            original.workspace_id,
            original.run_id,
            original.operation_id,
            original.grant_id,
        )
        calls.append(("get_stop", operation))
        return receipt(queried_status[0])

    runtime = SimpleNamespace(execution=SimpleNamespace(stop=stop, get_stop=get_stop))
    lab = TaskToolLab(runtime, ToolJourneyHarness(), None, None, {}, tmp_path, None)
    lab.main_stop = original
    first = lab.stop_original_main(grace_seconds=0)
    assert first is (outcome == "stopped")
    assert lab.main_stop_submitted and lab.main_stop_confirmed is first
    queried_status[0] = StopStatus.STOPPED
    assert lab.stop_original_main(grace_seconds=0)
    assert lab.main_stop is original and lab.main_stop_confirmed
    assert [call[0] for call in calls] == (
        ["stop", "get_stop"]
        if outcome == "stopped"
        else ["stop", "get_stop", "get_stop"]
    )
    assert lab.facts["main_stop_request"] == original.model_dump(mode="json")
    assert lab.facts["main_stop_confirmed"] is True
    assert lab.facts["main_stop_observations"][-1] == receipt(
        StopStatus.STOPPED
    ).model_dump(mode="json")
    assert "private transport detail" not in json.dumps(lab.facts)
    assert lab.native.calls == []


@pytest.mark.parametrize(
    "cleanup", ("complete", "incomplete", "partial_setup", "main_stop_unknown")
)
def test_composed_fixture_retains_original_incomplete_work_before_target_finalization(
    settings_values,
    tmp_path,
    no_database_connections,
    monkeypatch,
    cleanup,
):
    from pydantic import SecretStr
    from sidera_agents.contracts import RunStop, StopReceipt, StopStatus
    from sqlalchemy.engine import make_url

    from tests.integration import test_task_tool_progress as module

    calls, retained, originals = [], [], {}

    class Listener:
        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def listen(self, backlog):
            assert backlog == 128

        def getsockname(self):
            return "127.0.0.1", 37017

        def close(self):
            calls.append("listener.close")

    class Knowledge:
        def __init__(self, workspace):
            self.workspace, self.url, self.token = (
                workspace,
                "http://127.0.0.1:37018",
                SecretStr("owned-unit-token"),
            )
            originals["knowledge"] = self

        def start(self):
            calls.append("knowledge.start")

        def close(self):
            calls.append("knowledge.close")
            return True

        def facts(self):
            return {"closed": "knowledge.close" in calls}

    class Server:
        def __init__(self, app, listener):
            self.errors = []
            originals["server"] = self

        def close(self):
            calls.append("server.close")
            return True

    def build(settings, *, harnesses, secret_store):
        originals["native"] = harnesses[0]
        if cleanup == "partial_setup":
            raise RuntimeError("owned construction failure")

        def close(**kwargs):
            originals.setdefault("owner_close", []).append(kwargs)
            return cleanup == "complete"

        def stop(request):
            originals.setdefault("main_stop", []).append(request)
            return StopReceipt(
                operation_id=request.operation_id,
                run_id=request.run_id,
                status=StopStatus.UNKNOWN,
                reason_code="native_close_unknown",
                evidence_sequence=1,
            )

        def get_stop(workspace, run, operation, *, grant_id):
            originals.setdefault("main_stop_reads", []).append(
                (workspace, run, operation, grant_id)
            )
            return StopReceipt(
                operation_id=operation,
                run_id=run,
                status=StopStatus.UNKNOWN,
                reason_code="native_close_unknown",
                evidence_sequence=1,
            )

        runtime = SimpleNamespace(
            settings=settings,
            close=close,
            execution=SimpleNamespace(stop=stop, get_stop=get_stop),
        )
        originals["runtime"] = runtime
        return runtime

    listener = Listener()
    fields = {
        "product": "product_database_url",
        "agents": "agent_database_url",
        "cp": "cp_database_url",
        "credentials": "credential_database_url",
    }
    targets = tuple(
        SimpleNamespace(component=name, url=make_url(settings_values[field]))
        for name, field in fields.items()
    )
    guards = [SimpleNamespace(target=target) for target in targets]
    monkeypatch.setattr(module.socket, "socket", lambda *args: listener)
    monkeypatch.setattr(module, "OwnedKnowledgeServer", Knowledge)
    monkeypatch.setattr(module, "LoopbackProduct", Server)
    monkeypatch.setattr(module, "build_product_runtime", build)
    monkeypatch.setattr(module, "create_app", lambda **kwargs: object())
    monkeypatch.setattr(
        module, "account_outstanding", lambda *args: {"read_only": True}
    )
    fixture = module.task_tool_lab.__wrapped__(
        (targets, guards, None), tmp_path, retained.extend, monkeypatch
    )
    if cleanup == "partial_setup":
        with pytest.raises(RuntimeError, match="owned construction failure"):
            next(fixture)
        assert calls == ["knowledge.start", "knowledge.close", "listener.close"]
        assert retained == []
        return
    lab = next(fixture)
    assert lab.runtime is originals["runtime"] and lab.native is originals["native"]
    if cleanup == "main_stop_unknown":
        lab.main_stop = RunStop(
            workspace_id=lab.runtime.settings.workspace_id,
            run_id=uuid4(),
            operation_id=uuid4(),
            grant_id=uuid4(),
        )
        clock = iter((0, 10))
        monkeypatch.setattr(module, "monotonic", lambda: next(clock))
    if cleanup in {"incomplete", "main_stop_unknown"}:
        with pytest.raises(AssertionError, match="cleanup pending"):
            next(fixture)
        assert retained == [lab, guards]
        assert calls == ["knowledge.start"]
        if cleanup == "main_stop_unknown":
            assert originals.get("owner_close", []) == []
            assert originals["main_stop"] == [lab.main_stop]
            assert originals["main_stop_reads"] == [
                (
                    lab.main_stop.workspace_id,
                    lab.main_stop.run_id,
                    lab.main_stop.operation_id,
                    lab.main_stop.grant_id,
                )
            ]
    else:
        with pytest.raises(StopIteration):
            next(fixture)
        assert calls == ["knowledge.start", "server.close", "knowledge.close"]
        assert retained == []
    assert originals["native"].calls == []


def test_owned_composed_check_converts_through_actual_setup_body_without_io(
    no_database_connections,
):
    import sys
    from pathlib import Path

    from sidera_agents.contracts import ProfileRef
    from sidera_control_plane.domain.result_integration import CheckSpecV1

    from sidera_product.app import WorkspaceSetupBody
    from tests.integration.test_task_tool_progress import owned_verification_check
    from tests.support.product_lab import CONTROLLED_PLANNING_BUDGET

    workspace = uuid4()
    profile = ProfileRef(workspace_id=workspace, profile_id=uuid4(), revision=1)
    directory = Path("/private/tmp/n18-inert-uncreated-repository")
    request = WorkspaceSetupBody.model_validate(
        {
            "repository_directory": str(directory),
            "expected_version": 0,
            "planner_agent_ref": profile.model_dump(mode="json"),
            "planning_budget": {
                "max_context_bytes": 1_048_576,
                "max_tokens": 65_536,
                "max_cost_microunits": 10_000_000,
                "max_runtime_seconds": 3600,
                "max_revisions": 3,
                "max_nodes": 64,
                "max_concurrency": 3,
            },
            "verification_checks": [owned_verification_check().to_mapping()],
        }
    ).to_request(workspace)
    assert (
        request.workspace_id == workspace and request.repository_directory == directory
    )
    assert request.planner_agent_ref == profile
    assert request.planning_budget == CONTROLLED_PLANNING_BUDGET
    assert request.verification_checks == (
        CheckSpecV1(
            key="owned-check",
            version=1,
            argv=(sys.executable, "-c", "pass"),
            cwd=".",
            environment_ref="owned",
            prepare_argvs=(),
            timeout_seconds=10,
            success_exit_codes=(0,),
            report_paths=(),
            required=True,
        ),
    )


def test_composed_probe_supports_exact_mcp_resource_through_actual_validator(
    no_database_connections,
):
    import sys
    from datetime import UTC, datetime

    from sidera_agents.contracts import (
        CapabilityResourceDraft,
        ProfileRef,
        ProfileRevision,
        ResourceRef,
        ResourceRevision,
        ResourceStatus,
        RuntimeCandidate,
        RuntimeRef,
        RuntimeSnapshot,
    )
    from sidera_agents.harnesses.registry import HarnessRegistry
    from sidera_agents.runtimes import RuntimeCatalog

    from tests.integration.test_task_tool_progress import (
        ToolJourneyHarness,
        profile_draft,
    )
    from tests.support.controlled_native import ControlledHarness

    workspace, grant = uuid4(), uuid4()
    checked_at = datetime.now(UTC)
    runtime_ref = RuntimeRef(workspace_id=workspace, runtime_id=uuid4())
    resource = ResourceRevision(
        ref=ResourceRef(workspace_id=workspace, resource_id=uuid4(), revision=1),
        draft=CapabilityResourceDraft(
            name="Owned connection-backed MCP",
            kind="mcp",
            settings={"connection_id": str(uuid4())},
            secret_refs=(),
        ),
        content_hash="sha256:" + "1" * 64,
        created_at=checked_at,
    )
    profile = ProfileRevision(
        ref=ProfileRef(workspace_id=workspace, profile_id=uuid4(), revision=1),
        draft=profile_draft(runtime_ref, resource.ref, "Generic controlled profile"),
        config_hash="sha256:" + "2" * 64,
        created_at=checked_at,
    )
    reads = []

    def require(selected_grant, action, target):
        assert (
            selected_grant == grant
            and action == "resource.read"
            and target == resource.ref
        )
        reads.append("require")

    def get_resource(ref):
        assert ref == resource.ref
        reads.append("resource")
        return resource

    def get_resource_status(selected_workspace, resource_id):
        assert (
            selected_workspace == workspace and resource_id == resource.ref.resource_id
        )
        reads.append("status")
        return ResourceStatus(resource_id=resource_id, enabled=True, version=1)

    baseline, journey = ControlledHarness(), ToolJourneyHarness()
    candidate = RuntimeCandidate(
        harness_id=journey.harness_id, executable=sys.executable
    )
    inherited_probe = baseline.probe(candidate, checked_at)
    journey_probe = journey.probe(candidate, checked_at)
    catalog = RuntimeCatalog(
        SimpleNamespace(
            get_resource=get_resource, get_resource_status=get_resource_status
        ),
        SimpleNamespace(require=require),
        registry=HarnessRegistry((journey,)),
    )

    def observed_runtime(probe):
        return RuntimeSnapshot(
            ref=runtime_ref,
            harness_id=journey.harness_id,
            executable=sys.executable,
            installed=True,
            enabled=True,
            version=1,
            auth_state="ready",
            limits={"max_active": 1, "max_sessions": 2},
            **probe.model_dump(),
        )

    rejected = catalog._resource_issues(
        profile, observed_runtime(inherited_probe), grant
    )
    assert [
        (issue.field, issue.code, issue.support.status.value, issue.support.reason_code)
        for issue in rejected
    ] == [
        (
            "resource_refs[0].mcp",
            "unsupported_capability",
            "unconfirmed",
            "resource_capability_unconfirmed",
        ),
    ]
    assert (
        catalog._resource_issues(profile, observed_runtime(journey_probe), grant) == []
    )
    assert (
        journey_probe.model_copy(update={"capabilities": inherited_probe.capabilities})
        == inherited_probe
    )
    assert set(journey_probe.capabilities) - set(inherited_probe.capabilities) == {
        "mcp"
    }
    support = journey_probe.capabilities["mcp"]
    assert (
        support.status.value == "supported"
        and support.reason_code == "controlled-task-tool-sdk"
    )
    assert support.constraints == {}
    assert reads == ["require", "resource", "status"] * 2
    assert baseline.calls == journey.calls == [("probe", sys.executable)]


@pytest.mark.anyio
async def test_owned_reply_transport_hides_early_original_get_and_loses_post_once(
    no_database_connections,
):
    from datetime import UTC, datetime
    from threading import get_ident

    from sidera_product.postgres.authority_store import ToolReceiptRecord
    from tests.integration.test_task_tool_progress import LoseOriginalReply

    now = datetime.now(UTC)
    row = ToolReceiptRecord(
        receipt_id=uuid4(),
        grant_id=uuid4(),
        run_id=uuid4(),
        response_id=uuid4(),
        tool_name="sidera_submit_goal",
        logical_request_id=uuid4(),
        payload_hash="a" * 64,
        command_id=uuid4(),
        intent={},
        outcome="dispatching",
        result=None,
        created_at=now,
        updated_at=now,
    )
    event_loop_thread = get_ident()
    receipt_reads, forwarded = [], []

    def read_original_receipt():
        assert get_ident() != event_loop_thread
        receipt_reads.append(row.receipt_id)
        return row

    async def inner_http(request):
        forwarded.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"command_id": str(row.command_id), "status": "applied"}
        )

    transport = LoseOriginalReply(None, read_original_receipt=read_original_receipt)
    transport.inner = httpx.MockTransport(inner_http)
    assert receipt_reads == [] and transport.original_command is None
    original_path = "/api/v1/commands/" + str(row.command_id)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        with pytest.raises(httpx.ReadError, match="original status not yet observable"):
            await client.get(original_path)
        assert transport.original_command == str(row.command_id)
        assert receipt_reads == [row.receipt_id] and forwarded == []
        with pytest.raises(httpx.ReadError, match="reply lost after commit"):
            await client.post(
                "/api/v1/commands", json={"command_id": str(row.command_id)}
            )
        assert transport.lost_reply
        assert forwarded == [("POST", "/api/v1/commands")]
        with pytest.raises(httpx.ReadError, match="original status not yet observable"):
            await client.get(original_path)
        transport.hide_original_status = False
        reply = await client.get(original_path)
        assert reply.status_code == 200 and reply.json()["command_id"] == str(
            row.command_id
        )
        other_path = "/api/v1/commands/" + str(uuid4())
        assert (await client.get(other_path)).status_code == 200
    assert transport.posts == [str(row.command_id)]
    assert transport.reads == [str(row.command_id)] * 3
    assert transport.original_command == str(row.command_id)
    assert receipt_reads == [row.receipt_id]
    assert forwarded == [
        ("POST", "/api/v1/commands"),
        ("GET", original_path),
        ("GET", other_path),
    ]
