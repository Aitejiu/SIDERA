from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from sidera_knowledge.api.commands import router as command_router
from sidera_knowledge.api.queries import router as query_router

ERROR_SCHEMA = {"$ref": "#/components/schemas/ErrorResponse"}
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


@dataclass(frozen=True)
class RouteContract:
    operation_id: str
    tags: tuple[str, ...]
    security: tuple[dict[str, list[str]], ...]
    error_statuses: tuple[int, ...]


_OWNER_MAIN_SECURITY = (
    {"LocalOwnerBearer": []},
    {"MainAgentBearer": []},
)
_ALL_CAPABILITY_SECURITY = (
    {"LocalOwnerBearer": []},
    {"MainAgentBearer": []},
    {"ControlPlaneBearer": []},
)
_CONTROL_PLANE_SECURITY = ({"ControlPlaneBearer": []},)
_LIST_ERRORS = (403, 422, 503)
_DETAIL_ERRORS = (403, 404, 422, 503)
_COMMAND_ERRORS = (403, 409, 422, 503)
_MATERIALIZATION_ERRORS = (403, 409, 422, 503)


_ROUTE_CONTRACTS: dict[tuple[str, str], RouteContract] = {
    ("/api/v1/commands", "post"): RouteContract(
        "knowledge_execute_command", ("commands",), _ALL_CAPABILITY_SECURITY, _COMMAND_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/pages",
        "get",
    ): RouteContract(
        "knowledge_list_pages", ("knowledge",), _OWNER_MAIN_SECURITY, _LIST_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}",
        "get",
    ): RouteContract(
        "knowledge_get_page", ("knowledge",), _OWNER_MAIN_SECURITY, _DETAIL_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}",
        "get",
    ): RouteContract(
        "knowledge_get_revision", ("knowledge",), _OWNER_MAIN_SECURITY, _DETAIL_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/diff",
        "get",
    ): RouteContract(
        "knowledge_diff_revisions", ("knowledge",), _OWNER_MAIN_SECURITY, _DETAIL_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/search",
        "get",
    ): RouteContract(
        "knowledge_search", ("knowledge",), _ALL_CAPABILITY_SECURITY, _LIST_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/candidates",
        "get",
    ): RouteContract(
        "knowledge_list_candidates", ("knowledge",), _OWNER_MAIN_SECURITY, _LIST_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/candidates/{candidate_id}",
        "get",
    ): RouteContract(
        "knowledge_get_candidate", ("knowledge",), _OWNER_MAIN_SECURITY, _DETAIL_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/knowledge/sources",
        "get",
    ): RouteContract(
        "knowledge_list_sources", ("knowledge",), _ALL_CAPABILITY_SECURITY, _LIST_ERRORS
    ),
    (
        "/api/v1/workspaces/{workspace_id}/commands/{command_id}",
        "get",
    ): RouteContract(
        "knowledge_get_command_status",
        ("commands",),
        _ALL_CAPABILITY_SECURITY,
        _LIST_ERRORS,
    ),
    (
        "/api/v1/internal/workspaces/{workspace_id}/materialize",
        "post",
    ): RouteContract(
        "knowledge_materialize_sections",
        ("internal",),
        _CONTROL_PLANE_SECURITY,
        _MATERIALIZATION_ERRORS,
    ),
}


def _error_schemas() -> dict[str, dict[str, Any]]:
    return {
        "ErrorBody": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code", "message", "details"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "details": {"type": "object", "additionalProperties": True},
            },
        },
        "ErrorResponse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["error"],
            "properties": {"error": {"$ref": "#/components/schemas/ErrorBody"}},
        },
    }


def customize_openapi(document: dict[str, Any]) -> dict[str, Any]:
    """Add the stable public Knowledge contract to a FastAPI document."""
    components = document.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes.update(
        {
            "LocalOwnerBearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": "Loopback local owner capability token.",
            },
            "MainAgentBearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": "Main Agent capability token.",
            },
            "ControlPlaneBearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": "Control Plane capability token.",
            },
        }
    )
    schemas = components.setdefault("schemas", {})
    schemas.update(_error_schemas())

    paths = document.setdefault("paths", {})
    for (path, method), contract in _ROUTE_CONTRACTS.items():
        operation = paths[path][method]
        operation["operationId"] = contract.operation_id
        operation["tags"] = list(contract.tags)
        operation["security"] = [dict(item) for item in contract.security]
        responses = operation.setdefault("responses", {})
        for status in contract.error_statuses:
            responses[str(status)] = {
                "description": _error_description(status),
                "content": {"application/json": {"schema": ERROR_SCHEMA}},
            }
        if "422" in responses:
            responses["422"] = {
                "description": _error_description(422),
                "content": {"application/json": {"schema": ERROR_SCHEMA}},
            }
    document["x-sidera-contract"] = "knowledge-api-v1"
    return document


def _error_description(status: int) -> str:
    return {
        403: "Capability token is missing or forbidden.",
        404: "The workspace-scoped resource was not found.",
        409: "The command or exact materialization conflicts with current state.",
        422: "The request or domain payload is invalid.",
        503: "The service or database is temporarily unavailable.",
    }[status]


def build_openapi() -> dict[str, Any]:
    app = FastAPI(title="SIDERA Knowledge Service", version="0.1.0")
    app.include_router(command_router)
    app.include_router(query_router)
    return customize_openapi(app.openapi())


def write_openapi(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the Knowledge OpenAPI contract")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_openapi(args.output)


if __name__ == "__main__":
    main()
