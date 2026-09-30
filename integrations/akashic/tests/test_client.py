import ast
import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from sidera_task_bridge.client import (
    CommandEnvelope,
    CommandIntent,
    CommandResultUnknown,
    ControlPlaneClient,
    ControlPlaneRejected,
    TransportUnavailable,
)
from sidera_task_bridge.config import TaskBridgeConfig
from sidera_task_bridge.contracts import (
    NotificationThreadDetail,
    NotificationThreadPage,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
BRIDGE_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "sidera_task_bridge"
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
TASK_ID = UUID("00000000-0000-0000-0000-000000000020")
THREAD_ID = UUID("00000000-0000-0000-0000-000000000030")
FACT_ID = UUID("00000000-0000-0000-0000-000000000031")
DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000032")
ROUTE_ID = UUID("00000000-0000-0000-0000-000000000033")


def make_config(**overrides: Any) -> TaskBridgeConfig:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "planning_profile_id": PROFILE_ID,
        "planning_profile_version": 1,
    }
    values.update(overrides)
    return TaskBridgeConfig.model_validate(values)


def make_envelope(**overrides: Any) -> CommandEnvelope:
    values: dict[str, Any] = {
        "command_id": UUID("00000000-0000-0000-0000-000000000010"),
        "command_type": "SubmitPlanningRequest",
        "target_type": "workspace",
        "target_id": None,
        "expected_version": None,
        "idempotency_key": "sidera:SubmitPlanningRequest:000000000010",
        "correlation_id": UUID("00000000-0000-0000-0000-000000000011"),
        "payload": {"objective": "Build an API"},
    }
    values.update(overrides)
    return CommandEnvelope.model_validate(values)


def notification_thread_payload(
    *, thread_id: UUID = THREAD_ID, notification_ref: str = "NTF-1234"
) -> dict[str, Any]:
    return {
        "id": str(thread_id),
        "workspace_id": str(WORKSPACE_ID),
        "recipient_type": "workspace_operator",
        "recipient_id": "local",
        "category": "action",
        "priority": "action",
        "topic": "execution_attention",
        "root_task_id": str(TASK_ID),
        "root_task_title": "Build API",
        "subtask_id": None,
        "subtask_title": None,
        "worker_item_id": None,
        "worker_item_title": None,
        "latest_fact_id": str(FACT_ID),
        "latest_fact_occurred_at": "2026-09-04T00:00:00Z",
        "title": "Execution needs attention",
        "summary": "The execution is waiting for review.",
        "action": "Open the task",
        "deep_link": f"/workspaces/{WORKSPACE_ID}/tasks/{TASK_ID}",
        "notification_ref": notification_ref,
        "business_status": "open",
        "organization_status": "active",
        "last_read_fact_id": None,
        "unread_count": 1,
        "version": 2,
        "opened_at": "2026-09-04T00:00:00Z",
        "resolved_at": None,
        "archived_at": None,
        "updated_at": "2026-09-04T00:00:00Z",
    }


def notification_detail_payload() -> dict[str, Any]:
    return {
        "thread": notification_thread_payload(),
        "facts": {
            "items": [
                {
                    "id": str(FACT_ID),
                    "thread_id": str(THREAD_ID),
                    "policy_version": 1,
                    "kind": "execution_attention",
                    "priority": "action",
                    "transition": "opened",
                    "topic": "execution_attention",
                    "root_task_id": str(TASK_ID),
                    "subtask_id": None,
                    "worker_item_id": None,
                    "source_projection_cursor": 3,
                    "source_projection_type": "task_overview",
                    "source_resource_id": str(TASK_ID),
                    "source_resource_version": 4,
                    "title": "Execution needs attention",
                    "summary": "The execution is waiting for review.",
                    "action": "Open the task",
                    "deep_link": f"/workspaces/{WORKSPACE_ID}/tasks/{TASK_ID}",
                    "correlation_id": "notification-correlation",
                    "baseline_import": False,
                    "occurred_at": "2026-09-04T00:00:00Z",
                    "created_at": "2026-09-04T00:00:00Z",
                }
            ],
            "next_cursor": None,
        },
        "deliveries": [
            {
                "delivery_id": str(DELIVERY_ID),
                "route_id": str(ROUTE_ID),
                "channel_type": "akashic_private",
                "contract_version": 1,
                "status": "exhausted",
                "attempts": 3,
                "next_attempt_at": "2026-09-04T00:00:00Z",
                "last_error_code": "provider_unavailable",
                "delivered_at": None,
                "exhausted_at": "2026-09-04T00:00:00Z",
                "created_at": "2026-09-04T00:00:00Z",
                "updated_at": "2026-09-04T00:00:00Z",
            }
        ],
        "notification_ref": "NTF-1234",
        "available_commands": ["mark_read", "archive"],
        "deep_link": f"/workspaces/{WORKSPACE_ID}/tasks/{TASK_ID}",
        "last_change_cursor": 3,
    }


def command_response(command_id: UUID) -> dict[str, Any]:
    return {
        "command_id": str(command_id),
        "aggregate_id": str(UUID("00000000-0000-0000-0000-000000000020")),
        "aggregate_version": 1,
        "event_ids": [str(UUID("00000000-0000-0000-0000-000000000021"))],
        "data": {"planning_request_id": str(UUID("00000000-0000-0000-0000-000000000022"))},
    }


def test_client_does_not_inherit_host_proxy_for_loopback_control_plane() -> None:
    async def scenario() -> None:
        client = ControlPlaneClient(make_config())
        try:
            assert client._client._trust_env is False
        finally:
            await client.close()

    asyncio.run(scenario())


def test_execute_command_injects_config_identity_and_parses_201() -> None:
    async def scenario() -> None:
        envelope = make_envelope()
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["json"] = request.read().decode()
            return httpx.Response(201, json=command_response(envelope.command_id))

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        result = await client.execute_command(envelope)
        await client.close()

        assert seen["method"] == "POST"
        assert seen["url"] == "http://127.0.0.1:8000/api/v1/commands"
        body = httpx.Response(200, content=seen["json"]).json()
        assert body["workspace_id"] == str(WORKSPACE_ID)
        assert body["actor"] == {
            "type": "integration",
            "id": "akashic-task-bridge",
        }
        assert body["command_id"] == str(envelope.command_id)
        assert result.status == "applied"
        assert result.result is not None
        assert result.result.command_id == envelope.command_id

    asyncio.run(scenario())


def test_client_reads_workspace_scoped_resources_and_task_query() -> None:
    async def scenario() -> None:
        request_urls: list[str] = []
        request_params: list[dict[str, str]] = []
        request_id = UUID("00000000-0000-0000-0000-000000000022")
        task_id = UUID("00000000-0000-0000-0000-000000000023")
        command_id = UUID("00000000-0000-0000-0000-000000000024")

        def handler(request: httpx.Request) -> httpx.Response:
            request_urls.append(request.url.path)
            request_params.append(dict(request.url.params))
            if request.url.path.endswith(f"commands/{command_id}"):
                return httpx.Response(
                    200,
                    json={"command_id": str(command_id), "status": "pending"},
                )
            if request.url.path.endswith("/clarifications"):
                return httpx.Response(200, json={"items": [{"question": "Which API?"}]})
            return httpx.Response(200, json={"status": "ready"})

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        assert (await client.get_command_status(command_id)).status == "pending"
        assert await client.get_planning_detail(request_id) == {"status": "ready"}
        assert await client.list_clarifications(request_id) == ({"question": "Which API?"},)
        assert await client.get_proposal(request_id, 2) == {"status": "ready"}
        assert await client.get_materialization(request_id) == {"status": "ready"}
        assert await client.get_task_overview(task_id) == {"status": "ready"}
        await client.close()

        assert all(str(WORKSPACE_ID) in path for path in request_urls[:-1])
        assert request_urls[-1] == f"/api/v1/tasks/{task_id}/overview"
        assert request_params[-1] == {"workspace_id": str(WORKSPACE_ID)}

    asyncio.run(scenario())


def test_client_reads_strict_notification_views_and_exact_route_scope() -> None:
    async def scenario() -> None:
        request_paths: list[str] = []
        request_params: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_paths.append(request.url.path)
            request_params.append(dict(request.url.params))
            if request.url.path.endswith("/notification-summary"):
                return httpx.Response(
                    200,
                    json={
                        "recipient_type": "workspace_operator",
                        "recipient_id": "local",
                        "open_count": 1,
                        "unread_count": 1,
                        "progress_count": 0,
                        "channel_issue_count": 1,
                        "last_change_cursor": 3,
                    },
                )
            if request.url.path.endswith("/notification-threads"):
                return httpx.Response(
                    200,
                    json={
                        "items": [notification_thread_payload()],
                        "next_cursor": "next-page",
                        "last_change_cursor": 3,
                    },
                )
            if request.url.path.endswith(f"/notification-threads/{THREAD_ID}"):
                return httpx.Response(200, json=notification_detail_payload())
            if request.url.path.endswith(f"/notification-routes/{ROUTE_ID}/threads"):
                return httpx.Response(
                    200,
                    json={
                        "items": [notification_thread_payload()],
                        "next_cursor": None,
                        "last_change_cursor": 3,
                    },
                )
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        summary = await client.get_notification_summary()
        page = await client.list_notification_threads(
            category="action", unread_only=True, cursor="opaque-cursor", limit=20
        )
        route_page = await client.list_notification_route_threads(
            ROUTE_ID, cursor=None, limit=20
        )
        detail = await client.get_notification_thread(
            THREAD_ID, fact_cursor="fact-cursor", fact_limit=50
        )
        await client.close()

        assert summary.unread_count == 1
        assert isinstance(page, NotificationThreadPage)
        assert page.items[0].notification_ref == "NTF-1234"
        assert route_page.items[0].id == THREAD_ID
        assert isinstance(detail, NotificationThreadDetail)
        assert detail.deliveries[0].delivery_id == DELIVERY_ID
        assert request_params[1] == {
            "category": "action",
            "unread_only": "true",
            "cursor": "opaque-cursor",
            "limit": "20",
        }
        assert request_params[2] == {"limit": "20"}
        assert request_params[3] == {"cursor": "fact-cursor", "limit": "50"}
        assert request_paths[2].endswith(f"/notification-routes/{ROUTE_ID}/threads")

    asyncio.run(scenario())


def test_client_rejects_notification_unknown_fields_and_unbounded_cursor() -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "recipient_type": "workspace_operator",
                    "recipient_id": "local",
                    "open_count": 0,
                    "unread_count": 0,
                    "progress_count": 0,
                    "channel_issue_count": 0,
                    "last_change_cursor": 0,
                    "unexpected": "secret-body",
                },
            )

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ControlPlaneRejected) as error:
            await client.get_notification_summary()
        assert error.value.code == "invalid_control_plane_response"
        with pytest.raises(ValueError):
            await client.list_notification_threads(cursor="x" * 513)
        await client.close()

    asyncio.run(scenario())


def test_client_builds_exact_notification_commands_without_duplicate_ids() -> None:
    client = ControlPlaneClient(make_config())
    read = client.build_mark_notification_thread_read(
        THREAD_ID, expected_version=2, expected_latest_fact_id=FACT_ID
    )
    archive = client.build_archive_notification_thread(
        THREAD_ID, expected_version=2
    )
    requeue = client.build_requeue_notification_delivery(
        DELIVERY_ID, expected_route_version=4, reason="retry after operator review"
    )
    asyncio.run(client.close())

    assert read.command_type == "MarkNotificationThreadRead"
    assert read.target_type == "notification_thread"
    assert read.target_id == THREAD_ID
    assert read.expected_version == 2
    assert read.payload == {"expected_latest_fact_id": str(FACT_ID)}
    assert archive.command_type == "ArchiveNotificationThread"
    assert archive.payload == {}
    assert requeue.command_type == "RequeueNotificationDelivery"
    assert requeue.target_type == "notification_delivery"
    assert requeue.target_id == DELIVERY_ID
    assert requeue.expected_version is None
    assert requeue.payload == {
        "expected_status": "exhausted",
        "expected_route_version": 4,
        "reason": "retry after operator review",
    }


def test_client_reads_revision_inputs_and_transition_query() -> None:
    async def scenario() -> None:
        request_id = UUID("00000000-0000-0000-0000-000000000022")
        task_id = UUID("00000000-0000-0000-0000-000000000023")
        transition_id = UUID("00000000-0000-0000-0000-000000000024")
        request_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_paths.append(request.url.path)
            if request.url.path.endswith(f"tasks/{task_id}"):
                return httpx.Response(200, json={"id": str(task_id), "version": 7})
            if request.url.path.endswith(f"tasks/{task_id}/active-plan"):
                return httpx.Response(
                    200,
                    json={
                        "root_task_id": str(task_id),
                        "active_snapshot_id": str(UUID(int=25)),
                        "active_generation": 3,
                        "active_snapshot_hash": "a" * 64,
                        "version": 2,
                    },
                )
            if request.url.path.endswith(f"planning-requests/{request_id}/diff"):
                return httpx.Response(200, json={"diff_hash": "b" * 64})
            if request.url.path.endswith(f"plan-transitions/{transition_id}"):
                return httpx.Response(
                    200,
                    json={"plan_transition_id": str(transition_id), "status": "converged"},
                )
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        assert (await client.get_task(task_id))["version"] == 7
        assert (await client.get_active_plan(task_id))["active_generation"] == 3
        assert (await client.get_revision_diff(request_id))["diff_hash"] == "b" * 64
        assert (
            await client.get_plan_transition(transition_id)
        )["plan_transition_id"] == str(transition_id)
        await client.close()

        assert request_paths == [
            f"/api/v1/tasks/{task_id}",
            f"/api/v1/workspaces/{WORKSPACE_ID}/tasks/{task_id}/active-plan",
            f"/api/v1/workspaces/{WORKSPACE_ID}/planning-requests/{request_id}/diff",
            f"/api/v1/workspaces/{WORKSPACE_ID}/plan-transitions/{transition_id}",
        ]

    asyncio.run(scenario())


def test_client_reads_governance_views_and_worker_version() -> None:
    async def scenario() -> None:
        worker_item_id = UUID("00000000-0000-0000-0000-000000000023")
        proposal_id = UUID("00000000-0000-0000-0000-000000000024")
        request_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_paths.append(request.url.path)
            if request.url.path.endswith("/workspace"):
                return httpx.Response(
                    200,
                    json={
                        "worker_item": {
                            "id": str(worker_item_id),
                            "root_task_id": str(UUID("00000000-0000-0000-0000-000000000025")),
                            "version": 7,
                        }
                    },
                )
            if request.url.path.endswith(f"worker-items/{worker_item_id}"):
                return httpx.Response(200, json={"worker_item_id": str(worker_item_id)})
            if request.url.path.endswith(f"proposals/{proposal_id}"):
                return httpx.Response(200, json={"proposal_id": str(proposal_id)})
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        assert (await client.get_collaboration_governance_worker_item(worker_item_id))[
            "worker_item_id"
        ] == str(worker_item_id)
        assert (await client.get_collaboration_governance_proposal(proposal_id))[
            "proposal_id"
        ] == str(proposal_id)
        workspace = await client.get_worker_item_workspace(worker_item_id)
        worker_item = workspace["worker_item"]
        assert isinstance(worker_item, dict)
        assert worker_item["version"] == 7
        await client.close()

        assert request_paths == [
            f"/api/v1/workspaces/{WORKSPACE_ID}/collaboration-governance/worker-items/{worker_item_id}",
            f"/api/v1/workspaces/{WORKSPACE_ID}/collaboration-governance/proposals/{proposal_id}",
            f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{worker_item_id}/workspace",
        ]

    asyncio.run(scenario())


def test_client_reads_result_input_view_and_immutable_selection() -> None:
    async def scenario() -> None:
        worker_item_id = UUID("00000000-0000-0000-0000-000000000023")
        request_id = UUID("00000000-0000-0000-0000-000000000024")
        selection_id = UUID("00000000-0000-0000-0000-000000000025")
        requests: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.url.path, request.url.params.get("execution_request_id")))
            if request.url.path.endswith(f"worker-items/{worker_item_id}/result-inputs"):
                return httpx.Response(
                    200,
                    json={
                        "schema_version": 1,
                        "workspace_id": str(WORKSPACE_ID),
                        "worker_item_id": str(worker_item_id),
                    },
                )
            if request.url.path.endswith(f"input-selections/{selection_id}"):
                return httpx.Response(
                    200,
                    json={"selection_id": str(selection_id)},
                )
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        result = await client.get_worker_result_inputs(worker_item_id, request_id)
        selection = await client.get_input_selection(selection_id)
        await client.close()

        assert result["schema_version"] == 1
        assert selection["selection_id"] == str(selection_id)
        assert requests == [
            (
                f"/api/v1/workspaces/{WORKSPACE_ID}/worker-items/{worker_item_id}/result-inputs",
                str(request_id),
            ),
            (
                f"/api/v1/workspaces/{WORKSPACE_ID}/collaboration/input-selections/{selection_id}",
                None,
            ),
        ]

    asyncio.run(scenario())


def test_client_uses_governance_principal_only_for_allowlisted_commands() -> None:
    config = make_config(
        governance_actor_type="local",
        governance_actor_id="main-agent",
    )
    client = ControlPlaneClient(config)
    try:
        governance = client.build_intent_request_json(
            CommandIntent(
                command_type="PauseCollaborationRelease",
                target_type="worker_item",
                target_id=UUID("00000000-0000-0000-0000-000000000023"),
                expected_version=7,
                payload={"transport": "web", "target_plan_generation": 3},
            )
        )
        planning = client.build_intent_request_json(
            CommandIntent(
                command_type="SubmitPlanningRequest",
                target_type="workspace",
                target_id=None,
                expected_version=None,
                payload={"objective": "Build an API"},
            )
        )
    finally:
        asyncio.run(client.close())

    assert governance["actor"] == {"type": "local", "id": "main-agent"}
    governance_payload = governance["payload"]
    assert isinstance(governance_payload, dict)
    assert governance_payload["transport"] == "main_agent"
    assert planning["actor"] == {
        "type": "integration",
        "id": "akashic-task-bridge",
    }


def test_client_maps_safe_conflict_details_without_raw_body() -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "planning_conflict",
                        "message": "private server detail must not leak",
                        "details": {"request_version": 3},
                    }
                },
            )

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ControlPlaneRejected) as error:
            await client.execute_command(make_envelope())
        await client.close()

        assert error.value.code == "planning_conflict"
        assert error.value.details == {"request_version": 3}
        assert "private server detail" not in str(error.value)

    asyncio.run(scenario())


def test_client_maps_nonstandard_error_body_to_stable_code() -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="secret raw response")

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ControlPlaneRejected) as error:
            await client.get_planning_detail(uuid4())
        await client.close()

        assert error.value.code == "invalid_control_plane_response"
        assert "secret raw response" not in str(error.value)

    asyncio.run(scenario())


def test_client_distinguishes_read_unavailable_from_post_unknown() -> None:
    async def scenario() -> None:
        def read_timeout(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("wire detail", request=_request)

        read_client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(read_timeout)
        )
        with pytest.raises(TransportUnavailable) as read_error:
            await read_client.get_planning_detail(uuid4())
        await read_client.close()
        assert "wire detail" not in str(read_error.value)

        post_client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(read_timeout)
        )
        envelope = make_envelope()
        with pytest.raises(CommandResultUnknown) as post_error:
            await post_client.execute_command(envelope)
        await post_client.close()
        assert post_error.value.command_id == envelope.command_id
        assert "wire detail" not in str(post_error.value)

    asyncio.run(scenario())


def test_missing_materialization_is_an_empty_read_result() -> None:
    async def scenario() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        client = ControlPlaneClient(
            make_config(), transport=httpx.MockTransport(handler)
        )
        assert await client.get_materialization(uuid4()) == {}
        await client.close()

    asyncio.run(scenario())


def test_neutral_client_modules_have_no_akashic_imports() -> None:
    forbidden = {"agent", "akashic_sdk", "session", "bootstrap"}
    for filename in (
        "config.py",
        "contracts.py",
        "client.py",
        "knowledge_client.py",
        "knowledge_contracts.py",
    ):
        source_path = BRIDGE_PACKAGE_ROOT / filename
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [node.module.split(".", 1)[0]] if node.module else []
            else:
                continue
            assert not forbidden.intersection(roots), (filename, roots)
