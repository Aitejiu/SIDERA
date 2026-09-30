from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sidera_control_plane.api import app as app_module
from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.demo.supervisor import write_run_phase
from tests.unit.interview_demo_support import manifest_for


def test_demo_status_is_not_registered_in_normal_mode() -> None:
    app = create_app(Settings(database_url="postgresql+psycopg://unused/unused"))

    response = TestClient(app).get("/api/v1/demo/status")

    assert response.status_code == 404


def test_demo_status_exposes_only_the_configured_run_identity(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "sidera"
    project_root.mkdir()
    manifest = manifest_for(tmp_path)
    run_root = (
        project_root / ".sidera" / "interview-demo" / str(manifest.run_id)
    )
    manifest = manifest.model_copy(update={"run_root": run_root})
    run_root.mkdir(parents=True)
    manifest_path = run_root / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    write_run_phase(manifest, "running")
    monkeypatch.setattr(app_module, "_PROJECT_ROOT", project_root)
    app = create_app(
        Settings(
            database_url="postgresql+psycopg://unused/unused",
            interview_demo_manifest=manifest_path,
        )
    )

    response = TestClient(app).get("/api/v1/demo/status")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "interview-demo",
        "run_id": str(manifest.run_id),
        "workspace_id": str(manifest.workspace_id),
        "scenario": "mainline",
        "phase": "running",
    }
