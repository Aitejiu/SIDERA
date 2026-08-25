from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sidera_knowledge.api.openapi_contract import build_openapi, write_openapi

pytestmark = pytest.mark.integration

EXPECTED_PATHS = {
    "/api/v1/commands",
    "/api/v1/workspaces/{workspace_id}/knowledge/pages",
    "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}",
    "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}",
    "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/diff",
    "/api/v1/workspaces/{workspace_id}/knowledge/search",
    "/api/v1/workspaces/{workspace_id}/knowledge/candidates",
    "/api/v1/workspaces/{workspace_id}/knowledge/candidates/{candidate_id}",
    "/api/v1/workspaces/{workspace_id}/knowledge/sources",
    "/api/v1/workspaces/{workspace_id}/commands/{command_id}",
    "/api/v1/internal/workspaces/{workspace_id}/materialize",
}

BASE_ERROR_STATUSES = {"403", "422", "503"}
ERROR_SCHEMA = {"$ref": "#/components/schemas/ErrorResponse"}


def _operations(document: dict[str, object]) -> list[dict[str, object]]:
    paths = document["paths"]
    assert isinstance(paths, dict)
    operations: list[dict[str, object]] = []
    for path_item in paths.values():
        assert isinstance(path_item, dict)
        operations.extend(
            value
            for method, value in path_item.items()
            if method in {"get", "post", "put", "patch", "delete"}
            and isinstance(value, dict)
        )
    return operations


def test_knowledge_openapi_is_complete_and_security_scoped() -> None:
    document = build_openapi()
    paths = document["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == EXPECTED_PATHS

    operations = _operations(document)
    operation_ids = [operation["operationId"] for operation in operations]
    assert len(operation_ids) == len(set(operation_ids))
    assert all(str(operation_id).startswith("knowledge_") for operation_id in operation_ids)

    schemes = document["components"]["securitySchemes"]
    assert set(schemes) == {
        "LocalOwnerBearer",
        "MainAgentBearer",
        "ControlPlaneBearer",
    }
    owner_main = [{"LocalOwnerBearer": []}, {"MainAgentBearer": []}]
    all_capabilities = [
        {"LocalOwnerBearer": []},
        {"MainAgentBearer": []},
        {"ControlPlaneBearer": []},
    ]
    assert paths["/api/v1/workspaces/{workspace_id}/knowledge/pages"]["get"][
        "security"
    ] == owner_main
    assert paths["/api/v1/workspaces/{workspace_id}/knowledge/search"]["get"][
        "security"
    ] == all_capabilities
    assert paths["/api/v1/commands"]["post"]["security"] == all_capabilities
    materialize = paths["/api/v1/internal/workspaces/{workspace_id}/materialize"]["post"]
    assert materialize["tags"] == ["internal"]
    assert materialize["security"] == [{"ControlPlaneBearer": []}]

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method not in {"get", "post"}:
                continue
            statuses = set(operation["responses"])
            expected_statuses = set(BASE_ERROR_STATUSES)
            if path in {
                "/api/v1/commands",
                "/api/v1/internal/workspaces/{workspace_id}/materialize",
            }:
                expected_statuses.add("409")
            if path in {
                "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}",
                "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}",
                "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/diff",
                "/api/v1/workspaces/{workspace_id}/knowledge/candidates/{candidate_id}",
            }:
                expected_statuses.add("404")
            assert expected_statuses.issubset(statuses), (path, method, statuses)
            for status in expected_statuses:
                assert operation["responses"][status]["content"]["application/json"][
                    "schema"
                ] == ERROR_SCHEMA

    schemas = document["components"]["schemas"]
    section = schemas["SectionPayload"]
    body_markdown = section["properties"]["body_markdown"]
    assert body_markdown["maxLength"] == 131_072
    assert body_markdown["contentMediaType"] == "text/markdown"

    command_schema = schemas["SubmitCommandRequest"]
    assert command_schema["type"] == "object"
    command_request = paths["/api/v1/commands"]["post"]["requestBody"]
    command_request_schema = command_request["content"]["application/json"]["schema"]
    assert command_request_schema["discriminator"]["propertyName"] == "command_type"
    assert len(command_request_schema["oneOf"]) == 4


def test_knowledge_openapi_export_is_deterministic(tmp_path: Path) -> None:
    output = tmp_path / "knowledge-openapi.json"
    write_openapi(output)
    first = output.read_bytes()
    first_digest = hashlib.sha256(first).hexdigest()
    write_openapi(output)
    second = output.read_bytes()

    assert first == second
    assert hashlib.sha256(second).hexdigest() == first_digest
    assert first.endswith(b"\n")
    assert json.loads(first)
