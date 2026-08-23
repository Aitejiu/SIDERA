import ast
import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from sidera_task_bridge.client import (
    CommandEnvelope,
    CommandResultUnknown,
    ControlPlaneClient,
    ControlPlaneRejected,
    TransportUnavailable,
)
from sidera_task_bridge.config import TaskBridgeConfig

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


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


def command_response(command_id: UUID) -> dict[str, Any]:
    return {
        "command_id": str(command_id),
        "aggregate_id": str(UUID("00000000-0000-0000-0000-000000000020")),
        "aggregate_version": 1,
        "event_ids": [str(UUID("00000000-0000-0000-0000-000000000021"))],
        "data": {"planning_request_id": str(UUID("00000000-0000-0000-0000-000000000022"))},
    }


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
    for filename in ("config.py", "contracts.py", "client.py"):
        tree = ast.parse(
            Path(f"integrations/akashic/sidera_task_bridge/{filename}").read_text(),
            filename=filename,
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [node.module.split(".", 1)[0]] if node.module else []
            else:
                continue
            assert not forbidden.intersection(roots), (filename, roots)
