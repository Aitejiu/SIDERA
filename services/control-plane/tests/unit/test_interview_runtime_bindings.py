from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.api import dependencies
from sidera_control_plane.api.schemas import CommandRequest
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import ValidationError
from tests.unit.interview_demo_support import manifest_for


def _settings(manifest_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test@127.0.0.1/test",
        interview_demo_manifest=manifest_path,
    )


def _request(manifest, command_type: str, *, payload: dict[str, object]) -> CommandRequest:
    target_type = {
        "ConfigureWorkerRuntime": "worker_runtime_configuration",
        "PinExecutionWorker": "execution",
        "ClearExecutionWorkerPin": "execution",
    }[command_type]
    target_id = (
        manifest.worker_ids["validation"]
        if command_type == "ConfigureWorkerRuntime"
        else uuid4()
    )
    return CommandRequest(
        command_id=uuid4(),
        workspace_id=manifest.workspace_id,
        command_type=command_type,
        target_type=target_type,
        actor={"type": "local", "id": "aitejiu"},
        target_id=target_id,
        expected_version=None if command_type == "ConfigureWorkerRuntime" else 1,
        idempotency_key=f"test:{uuid4()}",
        correlation_id=uuid4(),
        payload=payload,
    )


def _install_manifest(monkeypatch, manifest) -> Path:
    path = manifest.run_root / "manifest.json"
    monkeypatch.setattr(dependencies, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(dependencies, "validate_manifest", lambda *_args: None)
    return path


def test_demo_runtime_configuration_accepts_only_manifest_binding(monkeypatch, tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir()
    path = _install_manifest(monkeypatch, manifest)
    request = _request(
        manifest,
        "ConfigureWorkerRuntime",
        payload={
            "adapter_module_instance_id": str(manifest.module_ids["worker_primary"]),
            "adapter_module_config_version": 1,
            "workspace_module_instance_id": str(manifest.module_ids["workspace"]),
            "workspace_module_config_version": 1,
        },
    )

    dependencies.require_demo_runtime_binding(request, _settings(path))


@pytest.mark.parametrize(
    "payload_field,payload_value,reason",
    [
        ("adapter_module_instance_id", str(uuid4()), "adapter"),
        ("workspace_module_instance_id", str(uuid4()), "workspace"),
        ("adapter_module_config_version", 2, "version"),
        ("workspace_module_config_version", 2, "version"),
    ],
)
def test_demo_runtime_configuration_rejects_outside_or_unknown_module(
    monkeypatch, tmp_path: Path, payload_field: str, payload_value: object, reason: str
) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir()
    path = _install_manifest(monkeypatch, manifest)
    payload: dict[str, object] = {
        "adapter_module_instance_id": str(manifest.module_ids["worker_primary"]),
        "adapter_module_config_version": 1,
        "workspace_module_instance_id": str(manifest.module_ids["workspace"]),
        "workspace_module_config_version": 1,
    }
    payload[payload_field] = payload_value

    with pytest.raises(ValidationError) as error:
        dependencies.require_demo_runtime_binding(
            _request(manifest, "ConfigureWorkerRuntime", payload=payload),
            _settings(path),
        )
    assert error.value.code == "demo_runtime_binding_forbidden"
    assert reason in str(error.value).lower() or error.value.details


def test_demo_pin_rejects_worker_from_another_run(monkeypatch, tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir()
    path = _install_manifest(monkeypatch, manifest)
    with pytest.raises(ValidationError) as error:
        dependencies.require_demo_runtime_binding(
            _request(manifest, "PinExecutionWorker", payload={"worker_id": str(uuid4())}),
            _settings(path),
        )
    assert error.value.code == "demo_runtime_binding_forbidden"


def test_runtime_clear_with_empty_payload_is_allowed_for_the_active_run(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir()
    path = _install_manifest(monkeypatch, manifest)
    request = _request(
        manifest,
        "ClearExecutionWorkerPin",
        payload={},
    )
    dependencies.require_demo_runtime_binding(request, _settings(path))
