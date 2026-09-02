from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_planning_command_api import _seed_waiting_request

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(
        create_app(Settings(database_url=database_url, local_actor_id="operator"))
    ) as test_client:
        yield test_client


def test_planning_queries_return_safe_request_views(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    source_type = "akashic_private"
    source_ref = "src_7f6ea4c5b91c4a089c91e3108a746550"
    request_id, _, _ = _seed_waiting_request(
        database,
        workspace_id,
        source_type=source_type,
        source_ref=source_ref,
    )
    base = f"/api/v1/workspaces/{workspace_id}"

    profiles = client.get(f"{base}/planning-profiles")
    requests = client.get(f"{base}/planning-requests", params={"limit": 1})
    detail = client.get(f"{base}/planning-requests/{request_id}")
    briefs = client.get(f"{base}/planning-requests/{request_id}/briefs")
    runs = client.get(f"{base}/planning-requests/{request_id}/runs")
    clarifications = client.get(
        f"{base}/planning-requests/{request_id}/clarifications"
    )

    assert profiles.status_code == 200, profiles.text
    assert profiles.json()["items"][0]["profile_key"] == "integration"
    assert "planner_secret_refs" not in profiles.json()["items"][0]
    assert "policy" not in profiles.json()["items"][0]

    assert requests.status_code == 200, requests.text
    requests_body = requests.json()
    assert requests_body["last_change_cursor"] == detail.json()["last_change_cursor"]
    list_item = requests_body["items"][0]
    assert list_item["id"] == str(request_id)
    assert list_item["requires_attention"] is True
    assert list_item["source_type"] == source_type
    assert list_item["source_ref"] == source_ref

    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["id"] == str(request_id)
    assert detail_body["status"] == "waiting_for_user"
    assert detail_body["source_type"] == source_type
    assert detail_body["source_ref"] == source_ref
    assert "channel" not in detail_body
    assert "chat_id" not in detail_body
    assert detail_body["active_run"]["status"] == "needs_clarification"
    assert detail_body["pending_clarifications"]
    assert detail_body["revision_base"] is None
    assert detail_body["revision_freshness"] is None
    assert "error_message" not in detail_body["active_run"]
    assert "provider_raw_response" not in detail_body

    assert briefs.status_code == 200, briefs.text
    assert briefs.json()["items"][0]["objective"] == "Implement planning"
    assert runs.status_code == 200, runs.text
    assert runs.json()["items"][0]["status"] == "needs_clarification"
    assert clarifications.status_code == 200, clarifications.text
    assert clarifications.json()["items"][0]["blocking"] is True


def test_planning_queries_preserve_nullable_web_source_ref(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    request_id, _, _ = _seed_waiting_request(
        database,
        workspace_id,
        source_type="web",
        source_ref=None,
    )
    base = f"/api/v1/workspaces/{workspace_id}"

    requests = client.get(f"{base}/planning-requests", params={"limit": 1})
    detail = client.get(f"{base}/planning-requests/{request_id}")

    assert requests.status_code == 200, requests.text
    assert requests.json()["items"][0]["source_type"] == "web"
    assert requests.json()["items"][0]["source_ref"] is None
    assert detail.status_code == 200, detail.text
    assert detail.json()["source_type"] == "web"
    assert detail.json()["source_ref"] is None


def test_planning_queries_support_attention_filter_and_same_workspace_not_found(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    request_id, _, _ = _seed_waiting_request(database, workspace_id)
    base = f"/api/v1/workspaces/{workspace_id}"

    filtered = client.get(
        f"{base}/planning-requests",
        params={"requires_attention": "true", "status": "waiting_for_user"},
    )
    missing = client.get(f"{base}/planning-requests/{uuid4()}")
    cross_workspace = client.get(
        f"/api/v1/workspaces/{UUID('00000000-0000-0000-0000-000000000099')}"
        f"/planning-requests/{request_id}"
    )

    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()["items"]] == [str(request_id)]
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert cross_workspace.status_code == 404
    assert cross_workspace.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("suffix", ["proposals/1", "materialization"])
def test_optional_planning_records_have_uniform_not_found_contract(
    client: TestClient,
    workspace_id: UUID,
    suffix: str,
) -> None:
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/planning-requests/{uuid4()}/{suffix}"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
