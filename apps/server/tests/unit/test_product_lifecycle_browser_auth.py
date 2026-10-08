import json
from http.cookies import SimpleCookie
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from sidera_agents.contracts import ProfileRef

from tests.integration import test_product_lifecycle as lifecycle

pytestmark = pytest.mark.anyio


async def test_submit_goal_sends_cookie_csrf_for_original_selection(
    no_database_connections,
):
    workspace = UUID("11111111-1111-4111-8111-111111111111")
    profile = SimpleNamespace(
        ref=ProfileRef(
            workspace_id=workspace,
            profile_id=UUID("22222222-2222-4222-8222-222222222222"),
            revision=3,
        )
    )
    setup = SimpleNamespace(
        planning_policy_ref=SimpleNamespace(
            policy_id=UUID("33333333-3333-4333-8333-333333333333"), version=7
        )
    )
    lab = SimpleNamespace(
        principal=SimpleNamespace(
            workspace=workspace,
            identity=SimpleNamespace(principal_id="owned-unit-human"),
        )
    )
    receipt = {
        "status": "accepted",
        "data": {"planning_request_id": "44444444-4444-4444-8444-444444444444"},
    }
    requests = []

    def record_and_respond(request):
        requests.append((request.method, str(request.url)))
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:8765/api/v1/commands"
        assert request.headers["Origin"] == "http://127.0.0.1:5173"
        assert "Authorization" not in request.headers
        cookies = SimpleCookie()
        cookies.load(request.headers["Cookie"])
        assert {name: value.value for name, value in cookies.items()} == {
            "sidera_local_session": "owned-unit-session",
            "sidera_csrf": "owned-unit-csrf",
        }
        assert request.headers.get("X-Sidera-CSRF") == "owned-unit-csrf"
        body = json.loads(request.content)
        assert body["workspace_id"] == "11111111-1111-4111-8111-111111111111"
        assert body["command_type"] == "SubmitTaskGoal"
        assert body["target_type"] == "workspace"
        assert body["target_id"] is None
        assert body["expected_version"] is None
        assert body["actor"] == {"type": "user", "id": "owned-unit-human"}
        command_id = str(UUID(body["command_id"]))
        assert body["idempotency_key"] == command_id
        assert body["correlation_id"] == command_id
        assert body["payload"]["planning_profile_id"] == (
            "33333333-3333-4333-8333-333333333333"
        )
        assert body["payload"]["planning_profile_version"] == 7
        assert body["payload"]["planner_agent_ref"] == {
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "profile_id": "22222222-2222-4222-8222-222222222222",
            "revision": 3,
        }
        return httpx.Response(201, json=receipt)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(record_and_respond),
        base_url="http://127.0.0.1:8765",
        headers={"Origin": "http://127.0.0.1:5173"},
        cookies={
            "sidera_local_session": "owned-unit-session",
            "sidera_csrf": "owned-unit-csrf",
        },
    ) as client:
        result = await lifecycle.submit_goal(client, lab, profile, setup)

    assert result == receipt
    assert requests == [("POST", "http://127.0.0.1:8765/api/v1/commands")]


async def test_observer_keeps_browser_auth_and_reads_only_original_request(
    monkeypatch, no_database_connections
):
    workspace = UUID("11111111-1111-4111-8111-111111111111")
    request_id = UUID("44444444-4444-4444-8444-444444444444")
    run_id = UUID("55555555-5555-4555-8555-555555555555")
    lab = SimpleNamespace(
        principal=SimpleNamespace(workspace=workspace),
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                local_url="http://127.0.0.1:8765",
                web_origin="http://127.0.0.1:5173",
            )
        ),
        native=SimpleNamespace(calls=[]),
        evidence=None,
    )
    goal = {"status": "accepted", "data": {"planning_request_id": str(request_id)}}
    receipts = [{"status": "accepted", "run_id": str(run_id)}]
    knowledge = SimpleNamespace(facts=lambda: {"requests": []})
    detail_url = (
        f"http://127.0.0.1:8765/api/v1/workspaces/{workspace}"
        f"/planning-requests/{request_id}"
    )
    runs_url = detail_url + "/runs"
    package_url = (
        f"http://127.0.0.1:8765/api/v1/workspaces/{workspace}"
        f"/planning-runs/{run_id}/context-package"
    )
    responses = {
        detail_url: {
            "id": str(request_id),
            "workspace_id": str(workspace),
            "status": "submitted",
            "version": 1,
            "active_run": str(run_id),
            "error": None,
        },
        runs_url: {
            "items": [
                {
                    "id": str(run_id),
                    "workspace_id": str(workspace),
                    "planning_request_id": str(request_id),
                    "status": "queued",
                }
            ]
        },
        package_url: {
            "package_id": "66666666-6666-4666-8666-666666666666",
            "workspace_id": str(workspace),
            "consumer_id": str(run_id),
            "status": "active",
            "assembly": {"status": "ready", "error_code": None},
            "budget": {},
            "knowledge_refs": [],
            "package_hash": "owned-unit-package-hash",
            "work_context_revision_id": None,
            "work_context_content_hash": None,
            "materialized_at": None,
            "error": None,
        },
    }
    requests = []

    def record_and_respond(request):
        requests.append((request.method, str(request.url)))
        assert request.method == "GET"
        assert str(request.url) in responses
        assert request.headers["Origin"] == "http://127.0.0.1:5173"
        assert "Authorization" not in request.headers
        cookies = SimpleCookie()
        cookies.load(request.headers["Cookie"])
        assert {name: value.value for name, value in cookies.items()} == {
            "sidera_local_session": "owned-unit-session",
            "sidera_csrf": "owned-unit-csrf",
        }
        return httpx.Response(200, json=responses[str(request.url)])

    transport = httpx.MockTransport(record_and_respond)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
        headers={"Origin": "http://127.0.0.1:5173"},
        cookies={
            "sidera_local_session": "owned-unit-session",
            "sidera_csrf": "owned-unit-csrf",
        },
    ) as browser:
        browser_cookies = httpx.Cookies(browser.cookies)
    assert browser.is_closed
    monkeypatch.setattr(lifecycle.httpx, "ASGITransport", lambda *, app: transport)

    observation = await lifecycle.capture_original_planning(
        lab, object(), goal, receipts, knowledge, browser_cookies=browser_cookies
    )

    assert observation["workspace_id"] == str(workspace)
    assert observation["request_id"] == str(request_id)
    assert observation["goal_receipt"] == goal
    assert observation["start_receipts"] == receipts
    assert observation["request"]["http_status"] == 200
    assert observation["request"]["data"]["id"] == str(request_id)
    assert observation["request"]["data"]["active_run"] == str(run_id)
    assert observation["runs"]["http_status"] == 200
    assert len(observation["runs"]["data"]["items"]) == 1
    observed_run = observation["runs"]["data"]["items"][0]
    assert observed_run["id"] == str(run_id)
    assert observed_run["planning_request_id"] == str(request_id)
    assert len(observation["packages"]) == 1
    assert observation["packages"][0]["http_status"] == 200
    assert observation["packages"][0]["data"]["consumer_id"] == str(run_id)
    assert requests == [
        ("GET", detail_url),
        ("GET", runs_url),
        ("GET", package_url),
    ]
