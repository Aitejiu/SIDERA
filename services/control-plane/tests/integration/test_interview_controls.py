from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

if os.environ.get("SIDERA_INTERVIEW_DEMO_TEST_RUN") != "1":
    raise RuntimeError(
        "test_interview_controls.py must run through the isolated demo test runner"
    )

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.demo.test_runner import validate_interview_test_context
from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


def test_demo_api_rejects_a_runtime_provider_outside_the_worker_binding(
    database: Database,
    database_url: str,
    clean_database: None,
) -> None:
    del database, clean_database
    manifest = validate_interview_test_context(os.environ)
    settings = Settings(
        database_url=database_url,
        interview_demo_manifest=manifest.run_root / "manifest.json",
    )
    command = _command(
        manifest.workspace_id,
        "ConfigureWorkerRuntime",
        "worker_runtime_configuration",
        manifest.worker_ids["validation"],
        {
            "adapter_module_instance_id": str(uuid4()),
            "adapter_module_config_version": 1,
            "workspace_module_instance_id": str(manifest.module_ids["workspace"]),
            "workspace_module_config_version": 1,
        },
        expected_version=1,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/commands", json=command)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "demo_runtime_binding_forbidden"


def test_demo_api_rejects_a_pin_to_a_worker_from_another_run(
    database: Database,
    database_url: str,
    clean_database: None,
) -> None:
    del database, clean_database
    manifest = validate_interview_test_context(os.environ)
    settings = Settings(
        database_url=database_url,
        interview_demo_manifest=manifest.run_root / "manifest.json",
    )
    command = _command(
        manifest.workspace_id,
        "PinExecutionWorker",
        "execution",
        uuid4(),
        {"worker_id": str(uuid4())},
        expected_version=1,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/commands", json=command)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "demo_runtime_binding_forbidden"


def _command(
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    target_id: UUID,
    payload: dict[str, object],
    *,
    expected_version: int,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "aitejiu"},
        "target_id": str(target_id),
        "expected_version": expected_version,
        "idempotency_key": f"controls:{uuid4()}",
        "correlation_id": str(uuid4()),
        "payload": payload,
    }
