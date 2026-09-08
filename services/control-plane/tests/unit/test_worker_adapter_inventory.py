from __future__ import annotations

from pathlib import Path
from sys import executable
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from sidera_control_plane import gateway_worker
from sidera_control_plane.adapters import opencode_acp
from sidera_control_plane.adapters.dsh_acp import DshACPAdapter
from sidera_control_plane.adapters.local_verification import LocalVerificationAdapter
from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.domain.modules import (
    LOCAL_VERIFICATION_PROVIDER,
    ModuleInstance,
    ModuleLifecycle,
)


def _settings(tmp_path: Path) -> Any:
    return SimpleNamespace(
        workspace_root=tmp_path / "workspaces",
        git_executable="git",
        opencode_executable="opencode",
        opencode_request_timeout_seconds=30.0,
        opencode_handoff_request_timeout_seconds=40.0,
        opencode_stop_grace_seconds=2.0,
    )


def _instance(*, config: dict[str, object] | None = None) -> ModuleInstance:
    return ModuleInstance(
        id=uuid4(),
        workspace_id=uuid4(),
        definition_id=uuid4(),
        instance_name="worker",
        config_version=1,
        config_namespace="worker",
        config=config or {},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def test_inventory_and_factories_share_one_registration_source(
    tmp_path: Path,
) -> None:
    registrations = gateway_worker.worker_adapter_inventory()
    factories = gateway_worker.build_worker_adapter_factories(
        _settings(tmp_path),
        workspace_manager=cast(Any, SimpleNamespace()),
    )

    assert gateway_worker.registered_worker_adapter_types() == tuple(
        item.adapter_type for item in registrations
    )
    assert tuple(factories) == gateway_worker.registered_worker_adapter_types()
    assert {item.adapter_type for item in registrations} == {
        "fake",
        "opencode",
        "opencode-acp",
        LOCAL_VERIFICATION_PROVIDER,
        "dsh-acp",
    }
    assert all(item.factory_key for item in registrations)


def test_reading_inventory_does_not_construct_or_start_an_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gateway_worker,
        "OpenCodeACPAdapter",
        lambda **_: pytest.fail("inventory must not construct OpenCode"),
    )

    registrations = gateway_worker.worker_adapter_inventory()

    assert registrations
    assert all(item.adapter_type != "fake" or item.is_fake for item in registrations)


def test_factory_constructs_real_opencode_with_bound_v3_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeTransport:
        def request(
            self,
            method: str,
            params: dict[str, object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            calls.append(
                (
                    method,
                    {"params": params, "timeout": timeout},
                )
            )
            if method == "initialize":
                return {
                    "protocolVersion": 1,
                    "agentCapabilities": {"loadSession": False},
                }
            if method == "session/new":
                return {"sessionId": "inventory-test-session"}
            raise AssertionError(f"unexpected ACP request: {method}")

        def terminate(self, *, grace_seconds: float) -> None:
            calls.append(("terminate", {"grace_seconds": grace_seconds}))

        def close(self) -> None:
            calls.append(("close", {}))

    def spawn(
        executable: str,
        *,
        cwd: Path,
        stderr_limit_bytes: int,
        env: dict[str, str],
    ) -> FakeTransport:
        calls.append(
            (
                "spawn",
                {
                    "executable": executable,
                    "cwd": cwd,
                    "stderr_limit_bytes": stderr_limit_bytes,
                    "env": env,
                },
            )
        )
        return FakeTransport()

    monkeypatch.setattr(opencode_acp.ACPProcessTransport, "spawn", staticmethod(spawn))
    settings = _settings(tmp_path)
    instance = _instance(
        config={
            "worker.executable": "configured-opencode",
            "worker.model": "opencode-go/MiMo-V2.5",
            "worker.context_package_versions": [1, 2, 3],
            "worker.request_timeout_seconds": 7.0,
            "worker.handoff_request_timeout_seconds": 8.0,
            "worker.stop_grace_seconds": 0.5,
        }
    )
    factories = gateway_worker.build_worker_adapter_factories(
        settings,
        workspace_manager=cast(Any, SimpleNamespace()),
    )

    adapter = factories["opencode"](instance)
    assert isinstance(adapter, OpenCodeACPAdapter)
    assert adapter._model == "opencode-go/MiMo-V2.5"
    assert adapter._workspace_root == settings.workspace_root.resolve()
    assert adapter._request_timeout_seconds == 7.0
    assert adapter._handoff_request_timeout_seconds == 8.0
    assert adapter._stop_grace_seconds == 0.5
    assert adapter._profile.context_package_versions == (1, 2, 3)

    profile = adapter.probe({})

    assert profile.context_package_versions == (1, 2, 3)
    spawn_call = next(payload for method, payload in calls if method == "spawn")
    assert spawn_call["executable"] == "configured-opencode"
    environment = cast(dict[str, str], spawn_call["env"])
    assert '"model":"opencode-go/MiMo-V2.5"' in environment["OPENCODE_CONFIG_CONTENT"]
    request_calls = [payload for method, payload in calls if method in {"initialize", "session/new"}]
    assert [payload["timeout"] for payload in request_calls] == [7.0, 7.0]


def test_default_opencode_module_keeps_legacy_context_contract(
    tmp_path: Path,
) -> None:
    factories = gateway_worker.build_worker_adapter_factories(
        _settings(tmp_path),
        workspace_manager=cast(Any, SimpleNamespace()),
    )

    adapter = factories["opencode"](_instance())

    assert isinstance(adapter, OpenCodeACPAdapter)
    assert adapter._profile.context_package_versions == (1, 2)


def test_factory_constructs_dsh_target_from_explicit_argv_and_environment(
    tmp_path: Path,
) -> None:
    instance = _instance(
        config={
            "worker.argv": [executable, "-c", "print('acp host')"],
            "worker.process_env": {"PATH": "/usr/bin", "DSH_MODEL": "mock"},
            "worker.context_package_versions": [1, 2],
            "worker.request_timeout_seconds": 3.0,
            "worker.stop_grace_seconds": 0.5,
        }
    )
    factories = gateway_worker.build_worker_adapter_factories(
        _settings(tmp_path),
        workspace_manager=cast(Any, SimpleNamespace()),
    )

    adapter = factories["dsh-acp"](instance)

    assert isinstance(adapter, DshACPAdapter)
    assert adapter._argv == (executable, "-c", "print('acp host')")
    assert adapter._process_env == {"PATH": "/usr/bin", "DSH_MODEL": "mock"}
    assert adapter._handoff_target_enabled is True
    assert adapter._profile.handoff_checkpoint_versions == ()
    assert adapter._profile.handoff_session_context_versions == (1,)
    assert adapter._profile.handoff_writer_gate_versions == (1,)


def test_local_verification_is_explicitly_not_a_second_agent(
    tmp_path: Path,
) -> None:
    registrations = {
        item.adapter_type: item for item in gateway_worker.worker_adapter_inventory()
    }
    factories = gateway_worker.build_worker_adapter_factories(
        _settings(tmp_path),
        workspace_manager=cast(Any, SimpleNamespace()),
    )

    adapter = factories[LOCAL_VERIFICATION_PROVIDER](_instance())

    assert isinstance(adapter, LocalVerificationAdapter)
    assert registrations[LOCAL_VERIFICATION_PROVIDER].is_local_verification is True
    assert registrations[LOCAL_VERIFICATION_PROVIDER].is_fake is False
    assert registrations[LOCAL_VERIFICATION_PROVIDER].delegates_to_opencode is False
