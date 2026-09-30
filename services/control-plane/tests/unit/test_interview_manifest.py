from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.manifest import (
    DemoConfigurationError,
    load_manifest,
    stable_id,
    validate_manifest,
)


def _payload(tmp_path: Path) -> dict[str, object]:
    run_id = uuid4()
    sidera_root = tmp_path / "sidera"
    run_root = sidera_root / ".sidera" / "interview-demo" / str(run_id)
    repository = tmp_path / f"sample-repository-{run_id.hex}"
    run_root.mkdir(parents=True, exist_ok=True)
    repository.mkdir(exist_ok=True)
    return {
        "run_id": run_id,
        "workspace_id": uuid4(),
        "scenario": "mainline",
        "run_root": run_root,
        "repository": repository,
        "base_commit": "a" * 40,
        "code_commit": "b" * 40,
        "database_name": f"sidera_interview_test_{run_id.hex}",
        "knowledge_database_name": f"sidera_interview_knowledge_{run_id.hex}",
        "endpoints": {
            "control_plane": "http://127.0.0.1:15432",
            "knowledge": "http://127.0.0.1:15433",
            "main_agent": "http://127.0.0.1:15434",
            "web": "http://127.0.0.1:15435",
        },
        "module_ids": {key: uuid4() for key in (
            "worker_primary",
            "worker_secondary",
            "workspace",
            "planner",
            "collaboration",
            "verification",
            "notification",
        )},
        "worker_ids": {key: uuid4() for key in (
            "validation",
            "report",
            "review",
            "integration",
            "replacement",
            "verification",
        )},
        "planning_profile_id": uuid4(),
    }


def _manifest(tmp_path: Path, **changes: object) -> DemoManifest:
    payload = _payload(tmp_path)
    payload.update(changes)
    return DemoManifest.model_validate(payload)


def test_valid_manifest_has_frozen_absolute_resource_contract(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    assert manifest.mode == "interview-demo"
    assert "verification" in manifest.worker_ids
    assert manifest.run_root.is_absolute()
    assert manifest.repository.is_absolute()
    validate_manifest(manifest, tmp_path / "sidera")
    assert manifest.run_root == (
        tmp_path / "sidera" / ".sidera" / "interview-demo" / str(manifest.run_id)
    )
    assert tmp_path / "sidera" not in manifest.repository.parents
    with pytest.raises(ValidationError):
        manifest.run_id = uuid4()  # type: ignore[misc]


def test_schema_two_manifest_requires_implementation_identity(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload.update(
        {
            "schema_version": 2,
            "implementation_fingerprint": "a" * 64,
            "fingerprint_version": 2,
        }
    )
    manifest = DemoManifest.model_validate(payload)
    assert manifest.schema_version == 2

    payload.pop("implementation_fingerprint")
    with pytest.raises(ValidationError):
        DemoManifest.model_validate(payload)


def test_unknown_scenario_and_extra_fields_are_rejected(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["scenario"] = "not-a-scenario"
    with pytest.raises(ValidationError):
        DemoManifest.model_validate(payload)

    payload = _payload(tmp_path)
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        DemoManifest.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("control_plane", "http://localhost:15432"),
        ("knowledge", "http://127.0.0.1:15433/path"),
        ("main_agent", "http://user:pass@127.0.0.1:15434"),
        ("web", "https://127.0.0.1:15435"),
        ("web", "http://127.0.0.1:15435?debug=1"),
        ("web", "http://127.0.0.1:1023"),
    ],
)
def test_manifest_rejects_non_local_or_non_canonical_endpoints(
    tmp_path: Path, field: str, value: str
) -> None:
    endpoints = dict(_payload(tmp_path)["endpoints"])
    endpoints[field] = value
    manifest = _manifest(tmp_path, endpoints=endpoints)

    with pytest.raises(DemoConfigurationError):
        validate_manifest(manifest, tmp_path / "sidera")


def test_manifest_rejects_duplicate_or_out_of_range_ports(tmp_path: Path) -> None:
    endpoints = dict(_payload(tmp_path)["endpoints"])
    endpoints["web"] = "http://127.0.0.1:15434"
    with pytest.raises(DemoConfigurationError, match="duplicate_endpoint_port"):
        validate_manifest(_manifest(tmp_path, endpoints=endpoints), tmp_path / "sidera")

    endpoints = dict(_payload(tmp_path)["endpoints"])
    endpoints["web"] = "http://127.0.0.1:65536"
    with pytest.raises(DemoConfigurationError, match="invalid_endpoint"):
        validate_manifest(_manifest(tmp_path, endpoints=endpoints), tmp_path / "sidera")


def test_manifest_rejects_database_name_drift(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, database_name="sidera_interview_test_wrong")

    with pytest.raises(DemoConfigurationError, match="database_name"):
        validate_manifest(manifest, tmp_path / "sidera")


def test_manifest_rejects_unscoped_run_root_and_repository_symlink_escape(
    tmp_path: Path,
) -> None:
    sidera_root = tmp_path / "sidera"
    sidera_root.mkdir()
    inside = sidera_root / "run"
    inside.mkdir()
    with pytest.raises(DemoConfigurationError, match="run_root"):
        validate_manifest(_manifest(tmp_path, run_root=inside), sidera_root)

    link = tmp_path / "link-run"
    link.symlink_to(sidera_root, target_is_directory=True)
    with pytest.raises(DemoConfigurationError, match="path_inside_sidera"):
        validate_manifest(
            _manifest(tmp_path, repository=link / "repository"),
            sidera_root,
        )


def test_stable_id_is_repeatable_and_scoped_to_run() -> None:
    first_run = uuid4()
    second_run = uuid4()

    assert stable_id(first_run, "worker/validate") == stable_id(first_run, "worker/validate")
    assert stable_id(first_run, "worker/validate") != stable_id(
        second_run, "worker/validate"
    )
    with pytest.raises(ValueError, match="invalid_resource_key"):
        stable_id(first_run, " worker/validate")


def test_load_manifest_reads_json_and_keeps_unknown_fields_invalid(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    payload = _payload(tmp_path)
    path.write_text(json.dumps(payload, default=str), encoding="utf-8")

    loaded = load_manifest(path)

    assert loaded.run_id == UUID(str(payload["run_id"]))

    path.write_text(
        json.dumps({**payload, "credentials": "secret"}, default=str),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(path)
