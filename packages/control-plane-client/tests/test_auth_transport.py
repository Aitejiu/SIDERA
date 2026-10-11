import asyncio
from uuid import uuid4

import httpx
import pytest

from sidera_control_plane_client import (
    ActorRef,
    CommandEnvelope,
    ControlPlaneClient,
    ControlPlaneClientConfig,
)
from sidera_control_plane_client.errors import CommandResultUnknown


class OwnedAuth(httpx.Auth):
    def auth_flow(self, request):
        request.headers["Authorization"] = "Bearer owned-test-transport-only"
        yield request


def configuration():
    return ControlPlaneClientConfig(
        control_plane_url="http://127.0.0.1:60800",
        workspace_id=uuid4(),
        actor=ActorRef(type="agent", id="operator"),
    )


def test_auth_is_only_http_transport_and_task_page_preserves_public_shape():
    config = configuration()
    requests = []
    expected = {"items": [], "total": 0, "limit": 20, "offset": 0}

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer owned-test-transport-only"
        assert request.url.path == "/api/v1/tasks"
        assert dict(request.url.params) == {
            "workspace_id": str(config.workspace_id),
            "limit": "20",
            "offset": "0",
        }
        return httpx.Response(200, json=expected)

    async def exercise():
        client = ControlPlaneClient(
            config, transport=httpx.MockTransport(respond), auth=OwnedAuth()
        )
        try:
            assert await client.list_tasks() == expected
        finally:
            await client.close()

    asyncio.run(exercise())
    assert len(requests) == 1
    assert "owned-test" not in config.model_dump_json()
    assert "auth" not in config.model_fields


def test_authenticated_lost_command_reply_is_unknown_without_resend():
    config, command = configuration(), uuid4()
    requests = []

    def lose_reply(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer owned-test-transport-only"
        raise httpx.ReadError("Controlled reply loss")

    async def exercise():
        client = ControlPlaneClient(
            config, transport=httpx.MockTransport(lose_reply), auth=OwnedAuth()
        )
        try:
            with pytest.raises(CommandResultUnknown) as caught:
                await client.execute_command(
                    CommandEnvelope(
                        command_id=command,
                        command_type="SubmitTaskGoal",
                        target_type="workspace",
                        target_id=None,
                        expected_version=None,
                        idempotency_key=str(command),
                        correlation_id=uuid4(),
                        payload={},
                    )
                )
            assert caught.value.command_id == command
        finally:
            await client.close()

    asyncio.run(exercise())
    assert len(requests) == 1 and requests[0].method == "POST"


@pytest.mark.parametrize(
    "limit,offset", ((True, 0), (101, 0), (1, -1), (1, True), (1, 10001))
)
def test_task_pagination_denies_before_transport(limit, offset):
    requests = []

    async def exercise():
        client = ControlPlaneClient(
            configuration(),
            transport=httpx.MockTransport(lambda request: requests.append(request)),
        )
        try:
            with pytest.raises(ValueError):
                await client.list_tasks(limit=limit, offset=offset)
        finally:
            await client.close()

    asyncio.run(exercise())
    assert requests == []


@pytest.mark.parametrize("status", ["pending", "applied", "rejected"])
def test_command_status_accepts_actual_ten_field_public_wire(status):
    config, command_id, aggregate_id = configuration(), uuid4(), uuid4()
    body = {
        "command_id": str(command_id),
        "status": status,
        "result": None,
        "error_code": "permission_denied" if status == "rejected" else None,
        "command_type": None if status == "pending" else "SubmitTaskGoal",
        "target_type": None if status == "pending" else "workspace",
        "target_id": None,
        "expected_version": None,
        "payload_sha256": None if status == "pending" else "a" * 64,
        "payload": None
        if status == "pending"
        else {"objective": "Exact original intent"},
    }
    if status == "applied":
        body["result"] = {
            "command_id": str(command_id),
            "aggregate_id": str(aggregate_id),
            "aggregate_version": 1,
            "event_ids": [],
            "data": {"root_task_id": str(aggregate_id)},
        }

    async def exercise():
        client = ControlPlaneClient(
            config,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        )
        try:
            result = await client.get_command_status(command_id)
            assert result.command_id == command_id and result.status == status
            assert result.command_type == body["command_type"]
            assert result.payload_sha256 == body["payload_sha256"]
            assert result.payload == body["payload"]
            if status == "applied":
                assert result.result.command_id == command_id
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("invalid_hash", ["A" * 64, "a" * 63, "not-a-hash"])
def test_command_status_retains_exact_hash_validation(invalid_hash):
    from sidera_control_plane_client.errors import ControlPlaneRejected

    async def exercise():
        command = uuid4()
        client = ControlPlaneClient(
            configuration(),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "command_id": str(command),
                        "status": "pending",
                        "payload_sha256": invalid_hash,
                    },
                )
            ),
        )
        try:
            with pytest.raises(ControlPlaneRejected) as caught:
                await client.get_command_status(command)
            assert caught.value.code == "invalid_control_plane_response"
        finally:
            await client.close()

    asyncio.run(exercise())
