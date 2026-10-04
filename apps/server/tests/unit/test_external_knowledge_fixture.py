import importlib.util
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sidera_control_plane.adapters.knowledge_http import (
    KnowledgeAdapterError,
    KnowledgeHttpAdapter,
)
from sidera_control_plane.application import planner_contract


def knowledge_fixture_module():
    assert importlib.util.find_spec("tests.support.controlled_knowledge") is not None, (
        "The owned Planner lifecycle fixture needs its exact external prerequisite"
    )
    from tests.support import controlled_knowledge

    return controlled_knowledge


def test_exact_known_empty_catalog_uses_real_adapter_and_bounded_owned_server():
    server = knowledge_fixture_module().OwnedKnowledgeServer(uuid4())
    server.start()
    adapter = KnowledgeHttpAdapter(
        base_url=server.url, control_plane_token=server.token
    )
    try:
        assert (
            adapter.search(
                server.workspace,
                "owned lifecycle",
                correlation_id=uuid4(),
                caller_request_id=uuid4(),
            )
            == ()
        )
        assert server.facts()["calls"] == [
            {
                "method": "GET",
                "path": f"/api/v1/workspaces/{server.workspace}/knowledge/search",
                "workspace_id": str(server.workspace),
                "status": 200,
                "item_count": 0,
            }
        ]
    finally:
        adapter.close()
        assert server.close() is True
    assert server.facts()["thread_alive"] is False
    assert server.facts()["closed"] is True


@pytest.mark.parametrize(
    "case",
    [
        "no_token",
        "wrong_token",
        "wrong_workspace",
        "unknown_reference",
        "unknown_route",
    ],
)
def test_external_known_empty_catalog_denies_other_scopes_and_references(case):
    server = knowledge_fixture_module().OwnedKnowledgeServer(uuid4())
    server.start()
    token = (
        None
        if case == "no_token"
        else SecretStr("test-owned-wrong-token")
        if case == "wrong_token"
        else server.token
    )
    adapter = KnowledgeHttpAdapter(base_url=server.url, control_plane_token=token)
    try:
        if case == "unknown_route":
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.get(
                    server.url + "/unknown",
                    headers={
                        "authorization": "Bearer " + server.token.get_secret_value()
                    },
                )
            assert response.status_code == 404
        else:
            with pytest.raises(KnowledgeAdapterError) as denied:
                if case == "unknown_reference":
                    adapter.get_revision_manifest(
                        server.workspace,
                        uuid4(),
                        uuid4(),
                        correlation_id=uuid4(),
                        caller_request_id=uuid4(),
                    )
                else:
                    adapter.search(
                        uuid4() if case == "wrong_workspace" else server.workspace,
                        "owned lifecycle",
                        correlation_id=uuid4(),
                        caller_request_id=uuid4(),
                    )
            assert denied.value.code == (
                "reference_not_found"
                if case == "unknown_reference"
                else "permission_denied"
            )
        assert all(call["status"] != 200 for call in server.facts()["calls"])
        assert server.token.get_secret_value() not in json.dumps(server.facts())
    finally:
        adapter.close()
        assert server.close() is True
    assert server.facts()["thread_alive"] is False


def test_lifecycle_fixture_budget_fits_actual_mandatory_native_schema_guidance():
    from tests.support import product_lab

    assert hasattr(product_lab, "CONTROLLED_PLANNING_BUDGET"), (
        "The lifecycle fixture must declare its ample functional admission envelope"
    )
    budget = product_lab.CONTROLLED_PLANNING_BUDGET
    shape = planner_contract._proposal_shape_hint(6)
    shape.pop("usage", None)
    mandatory_bytes = len(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ) + len(planner_contract._DEPENDENCY_PROMPT_CONTRACT.encode("utf-8"))
    assert 0 < mandatory_bytes < budget.max_context_bytes
    assert (
        budget.max_context_bytes,
        budget.max_tokens,
        budget.max_runtime_seconds,
        budget.max_nodes,
        budget.max_concurrency,
        budget.max_cost_microunits,
        budget.max_revisions,
    ) == (1_048_576, 65_536, 3600, 64, 3, 10_000_000, 3)
