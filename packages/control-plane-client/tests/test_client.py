import asyncio
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError


def _api():
    import sidera_control_plane_client

    return sidera_control_plane_client


def _config(api, **overrides):
    values = {
        "control_plane_url": "http://127.0.0.1:60800",
        "workspace_id": UUID("10000000-0000-0000-0000-000000000002"),
        "actor": api.ActorRef(type="integration", id="boundary-test"),
    }
    values.update(overrides)
    return api.ControlPlaneClientConfig(**values)


def _envelope(api, command_id=None):
    command_id = command_id or UUID("10000000-0000-0000-0000-000000000001")
    return api.CommandEnvelope(
        command_id=command_id,
        command_type="SubmitPlanningRequest",
        target_type="workspace",
        target_id=None,
        expected_version=None,
        idempotency_key="cfg1:one",
        correlation_id=command_id,
        payload={},
    )


def test_config_is_neutral_and_actor_text_is_bounded():
    api = _api()
    config = _config(api)

    assert set(api.ControlPlaneClientConfig.model_fields) == {
        "control_plane_url",
        "workspace_id",
        "actor",
        "connect_timeout_seconds",
        "request_timeout_seconds",
    }
    assert config.connect_timeout_seconds == 2.0
    assert config.request_timeout_seconds == 15.0
    for host_field in (
        "planning_profile_id",
        "planning_profile_version",
        "thread_id",
        "model",
        "api_key",
        "key",
        "notification_event_limit_bytes",
        "notification_channel_module_instance_id",
    ):
        with pytest.raises(ValidationError):
            api.ControlPlaneClientConfig.model_validate(
                {
                    **config.model_dump(),
                    host_field: "20000000-0000-0000-0000-000000000001",
                }
            )
    for invalid in ("", " \t ", "actor\nname"):
        with pytest.raises(ValidationError):
            api.ActorRef(type="integration", id=invalid)


@pytest.mark.parametrize("field_name", ["type", "id"])
@pytest.mark.parametrize("value", ["\tactor", "actor\n", "actor\t", "\nactor", "ac\x7for"])
def test_actor_identity_rejects_control_characters_before_normalizing(
    field_name, value
):
    api = _api()
    identity = {"type": "integration", "id": "boundary-test"}
    identity[field_name] = value

    with pytest.raises(ValidationError):
        api.ActorRef(**identity)


@pytest.mark.parametrize("field_name", ["type", "id"])
def test_actor_identity_normalizes_ordinary_padding(field_name):
    api = _api()
    identity = {"type": "integration", "id": "boundary-test"}
    identity[field_name] = "  padded-identity  "

    actor = api.ActorRef(**identity)

    assert getattr(actor, field_name) == "padded-identity"


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@127.0.0.1:60800",
        "http://127.0.0.1:60800?next=other",
        "http://127.0.0.1:60800#fragment",
        "https://control.example:60800",
    ],
)
def test_config_rejects_credentials_query_fragment_and_non_loopback(url):
    api = _api()
    with pytest.raises(ValidationError):
        _config(api, control_plane_url=url)


def test_post_unknown_keeps_identity_and_does_not_retry():
    api = _api()
    seen = []
    command_id = UUID("10000000-0000-0000-0000-000000000001")

    async def handler(request):
        seen.append((request.method, request.url.path))
        raise httpx.ReadTimeout("test transport", request=request)

    async def exercise():
        client = api.ControlPlaneClient(
            _config(api), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(api.CommandResultUnknown) as unknown:
                await client.execute_command(_envelope(api, command_id))
            assert unknown.value.command_id == command_id
            with pytest.raises(api.TransportUnavailable):
                await client.get_command_status(command_id)
        finally:
            await client.close()

    asyncio.run(exercise())
    assert seen == [
        ("POST", "/api/v1/commands"),
        (
            "GET",
            (
                "/api/v1/workspaces/10000000-0000-0000-0000-000000000002/commands/"
                "10000000-0000-0000-0000-000000000001"
            ),
        ),
    ]


def test_plain_command_uses_config_actor_without_host_governance_defaults():
    api = _api()
    client = api.ControlPlaneClient(_config(api))
    body = client.build_request_json(
        api.CommandEnvelope(
            command_id=UUID("10000000-0000-0000-0000-000000000003"),
            command_type="PauseCollaborationRelease",
            target_type="worker_item",
            target_id=UUID("10000000-0000-0000-0000-000000000004"),
            expected_version=2,
            idempotency_key="cfg1:two",
            correlation_id=UUID("10000000-0000-0000-0000-000000000003"),
            payload={"transport": "web"},
        )
    )
    asyncio.run(client.close())

    assert body["workspace_id"] == "10000000-0000-0000-0000-000000000002"
    assert body["actor"] == {"type": "integration", "id": "boundary-test"}
    assert body["payload"] == {"transport": "web"}


def test_201_response_is_strict_and_task_routes_remain_distinct():
    api = _api()
    command_id = UUID("10000000-0000-0000-0000-000000000001")
    task_id = UUID("10000000-0000-0000-0000-000000000005")
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "command_id": str(command_id),
                    "aggregate_id": str(task_id),
                    "aggregate_version": 1,
                    "event_ids": [],
                    "data": {},
                },
            )
        if request.url.path == f"/api/v1/tasks/{task_id}":
            return httpx.Response(200, json={"id": str(task_id), "version": 4})
        return httpx.Response(200, json={"active_generation": 2})

    async def exercise():
        client = api.ControlPlaneClient(
            _config(api), transport=httpx.MockTransport(handler)
        )
        try:
            result = await client.execute_command(_envelope(api, command_id))
            task = await client.get_task(task_id)
            active_plan = await client.get_active_plan(task_id)
            assert result.status == "applied"
            assert result.result is not None
            assert result.result.command_id == command_id
            assert task["version"] == 4
            assert active_plan["active_generation"] == 2
        finally:
            await client.close()

    asyncio.run(exercise())
    assert seen == [
        ("POST", "/api/v1/commands", {}),
        (
            "GET",
            f"/api/v1/tasks/{task_id}",
            {"workspace_id": "10000000-0000-0000-0000-000000000002"},
        ),
        (
            "GET",
            (
                f"/api/v1/workspaces/10000000-0000-0000-0000-000000000002/"
                f"tasks/{task_id}/active-plan"
            ),
            {},
        ),
    ]


def test_malformed_201_and_secret_error_details_are_safe():
    api = _api()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "command_id": str(UUID("10000000-0000-0000-0000-000000000001")),
                    "aggregate_id": str(UUID("10000000-0000-0000-0000-000000000005")),
                    "aggregate_version": 1,
                    "event_ids": [],
                    "data": {},
                    "unexpected": "strict",
                },
            )
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "planning_conflict",
                    "message": "server detail sk-test-secret Authorization",
                    "details": {
                        "request_version": 3,
                        "Authorization": "Bearer sk-test-secret",
                        "diagnostic": "Authorization: Bearer sk-test-secret",
                    },
                }
            },
        )

    async def exercise():
        client = api.ControlPlaneClient(
            _config(api), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(api.ControlPlaneRejected) as invalid:
                await client.execute_command(_envelope(api))
            assert invalid.value.code == "invalid_control_plane_response"
            with pytest.raises(api.ControlPlaneRejected) as rejected:
                await client.get_task(UUID("10000000-0000-0000-0000-000000000005"))
            assert rejected.value.code == "planning_conflict"
            assert rejected.value.details == {"request_version": 3}
            safe_text = f"{rejected.value!s} {rejected.value!r} {rejected.value.details!r}"
            assert "sk-test-secret" not in safe_text
            assert "Authorization" not in safe_text
        finally:
            await client.close()

    asyncio.run(exercise())
    assert calls == 2


@pytest.mark.parametrize(
    "raw_code",
    [
        "sk-cfg1-fake-not-a-credential",
        "Authorization: fake",
        "bad\ncode",
        "https://example.test/path",
        "",
        "a" * 129,
        "café",
    ],
)
def test_http_rejection_uses_fixed_code_for_non_protocol_identifiers(raw_code):
    api = _api()
    seen = []
    task_id = UUID("10000000-0000-0000-0000-000000000005")

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": raw_code,
                    "details": {"request_version": 1},
                }
            },
        )

    async def exercise():
        client = api.ControlPlaneClient(
            _config(api), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(api.ControlPlaneRejected) as rejected:
                await client.get_task(task_id)
            assert rejected.value.code == "invalid_control_plane_response"
            assert str(rejected.value) == "invalid_control_plane_response"
            assert rejected.value.details == {}
        finally:
            await client.close()

    asyncio.run(exercise())
    assert seen == [("GET", f"/api/v1/tasks/{task_id}")]


def test_direct_rejection_construction_replaces_invalid_code_and_details():
    api = _api()

    rejected = api.ControlPlaneRejected(
        "sk-cfg1-fake-not-a-credential", {"request_version": 1}
    )

    assert rejected.code == "invalid_control_plane_response"
    assert str(rejected) == "invalid_control_plane_response"
    assert rejected.details == {}


def test_valid_rejection_code_and_request_version_are_preserved():
    api = _api()
    task_id = UUID("10000000-0000-0000-0000-000000000005")

    def handler(_request):
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "notification_fencing_token_stale",
                    "details": {"request_version": 1},
                }
            },
        )

    async def exercise():
        client = api.ControlPlaneClient(
            _config(api), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(api.ControlPlaneRejected) as rejected:
                await client.get_task(task_id)
            assert rejected.value.code == "notification_fencing_token_stale"
            assert str(rejected.value) == "notification_fencing_token_stale"
            assert rejected.value.details == {"request_version": 1}
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "code",
    [
        "resource_not_found",
        "version_conflict",
        "notification_fencing_token_stale",
        "a" * 128,
    ],
)
def test_direct_rejection_construction_preserves_valid_protocol_identifiers(code):
    api = _api()

    rejected = api.ControlPlaneRejected(code, {"request_version": 1})

    assert rejected.code == code
    assert str(rejected) == code
    assert rejected.details == {"request_version": 1}


def test_loopback_client_ignores_host_http_proxy(monkeypatch):
    api = _api()
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8118")
    client = api.ControlPlaneClient(_config(api))
    try:
        assert client._client._trust_env is False
    finally:
        asyncio.run(client.close())


def test_notification_claim_requires_module_and_uses_protocol_defaults():
    api = _api()
    module_id = UUID("10000000-0000-0000-0000-000000000006")
    requests = []

    def handler(request):
        requests.append((request.url.path, request.read()))
        return httpx.Response(200, json={"deliveries": []})

    async def exercise():
        client = api.ControlPlaneClient(
            _config(api), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(api.ControlPlaneRejected) as missing:
                await client.claim_notification_deliveries()
            assert missing.value.code == "notification_channel_module_unavailable"
            assert await client.claim_notification_deliveries(
                module_instance_id=module_id
            ) == ()
            assert await client.claim_notification_deliveries(
                module_instance_id=module_id,
                supported_contract_versions=(2, 1),
                batch_size=7,
                lease_seconds=12,
            ) == ()
        finally:
            await client.close()

    asyncio.run(exercise())
    assert requests == [
        (
            (
                "/api/v1/workspaces/10000000-0000-0000-0000-000000000002/"
                "notification-deliveries/claims"
            ),
            (
                b'{"module_instance_id":"10000000-0000-0000-0000-000000000006",'
                b'"supported_contract_versions":[1],"batch_size":50,"lease_seconds":30}'
            ),
        ),
        (
            (
                "/api/v1/workspaces/10000000-0000-0000-0000-000000000002/"
                "notification-deliveries/claims"
            ),
            (
                b'{"module_instance_id":"10000000-0000-0000-0000-000000000006",'
                b'"supported_contract_versions":[2,1],"batch_size":7,"lease_seconds":12}'
            ),
        ),
    ]


def test_projection_stream_enforces_bounded_event_size():
    api = _api()
    oversized = b"data: " + b"x" * 262_144

    def handler(_request):
        return httpx.Response(200, content=oversized)

    async def exercise():
        client = api.ControlPlaneClient(
            _config(api), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(api.ProjectionResyncRequired) as error:
                async for _change in client.iter_projection_changes(after=0):
                    pass
            assert error.value.reason == "event_too_large"
            assert error.value.last_confirmed_cursor == 0
        finally:
            await client.close()

    asyncio.run(exercise())
