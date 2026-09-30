from __future__ import annotations

import httpx
import pytest

from sidera_control_plane.demo.http_client import (
    DemoClient,
    DemoClientError,
    DemoCommandOutcomeUnknown,
)
from tests.unit.interview_demo_support import manifest_for


def _envelope(manifest, command_id: str = "00000000-0000-0000-0000-000000000123"):
    return {
        "command_id": command_id,
        "workspace_id": str(manifest.workspace_id),
        "command_type": "HeartbeatWorker",
        "target_type": "worker",
        "target_id": str(manifest.worker_ids["validation"]),
        "expected_version": 1,
        "idempotency_key": "heartbeat:one",
        "correlation_id": command_id,
        "actor": {"type": "local", "id": "aitejiu"},
        "payload": {"healthy": True},
    }


def test_timeout_queries_the_same_command_id_without_resubmitting(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    envelope = _envelope(manifest)
    posts: list[str] = []
    queries: list[str] = []
    result = {
        "command_id": envelope["command_id"],
        "aggregate_id": envelope["target_id"],
        "aggregate_version": 2,
        "event_ids": [],
        "data": {},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = httpx.Request(request.method, request.url, content=request.content)
            command_id = str(__import__("json").loads(body.content)["command_id"])
            posts.append(command_id)
            raise httpx.ReadTimeout("response lost", request=request)
        queries.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            json={
                "command_id": envelope["command_id"],
                "status": "applied",
                "result": result,
                "error_code": None,
            },
            request=request,
        )

    client = DemoClient(manifest, transport=httpx.MockTransport(respond))
    try:
        assert client.command(envelope) == result
    finally:
        client.close()

    assert posts == [envelope["command_id"]]
    assert queries == [envelope["command_id"]]


def test_timeout_with_pending_status_is_unknown_and_does_not_retry(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    envelope = _envelope(manifest)
    posts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(
            200,
            json={
                "command_id": envelope["command_id"],
                "status": "pending",
                "result": None,
                "error_code": None,
            },
            request=request,
        )

    client = DemoClient(manifest, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(DemoCommandOutcomeUnknown):
            client.command(envelope)
    finally:
        client.close()

    assert posts == 1


def test_demo_client_rejects_absolute_urls_before_sending(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    sent: list[httpx.Request] = []
    client = DemoClient(
        manifest,
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200)
        ),
    )
    try:
        with pytest.raises(DemoClientError, match="invalid_local_path"):
            client.get("https://example.invalid/api")
    finally:
        client.close()

    assert sent == []
