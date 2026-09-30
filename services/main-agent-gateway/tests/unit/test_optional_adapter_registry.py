from __future__ import annotations

import asyncio
import builtins
import subprocess
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sidera_runtime_guard import RuntimeBudgetGuard

from sidera_main_agent_gateway.config import Settings

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")


def _akashic_settings() -> Settings:
    return Settings(
        akashic_endpoint="127.0.0.1:9010",
        sidera_workspace_id=WORKSPACE_ID,
    )


def _demo_settings() -> Settings:
    return Settings(
        adapter_kind="interview-demo",
        demo_manifest=Path("/tmp/unused-manifest.json"),
        sidera_workspace_id=WORKSPACE_ID,
    )


class _CloseTrackingAdapter:
    def __init__(self) -> None:
        self.close_calls = 0

    async def health(self) -> dict[str, str]:
        return {"status": "ok"}

    async def close(self) -> None:
        self.close_calls += 1


def test_api_root_import_does_not_import_optional_adapters() -> None:
    code = (
        "import importlib, sys; "
        "importlib.import_module('sidera_main_agent_gateway.api.app'); "
        "assert 'akashic_sdk' not in sys.modules; "
        "assert 'sidera_task_bridge' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_unconfigured_gateway_is_not_an_empty_successful_chat() -> None:
    from sidera_main_agent_gateway.api.app import create_app

    settings = Settings(
        adapter_kind="unconfigured",
        sidera_workspace_id=WORKSPACE_ID,
    )

    with TestClient(create_app(settings)) as client:
        health = client.get("/main-agent/v1/health")
        response = client.get("/main-agent/v1/threads")

    assert health.status_code == 200
    assert health.json()["provider"] == "unavailable"
    assert health.json()["status"] == "ok"
    assert health.json()["service"] == "sidera-main-agent-gateway"
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "main_agent_not_configured"
    assert response.json()["error"]["message"] == (
        "main agent adapter is unavailable"
    )
    assert response.json()["error"]["retryable"] is False


def test_unavailable_adapter_operations_raise_typed_errors() -> None:
    from sidera_main_agent_gateway.adapters.unavailable import (
        UnavailableMainAgentAdapter,
    )
    from sidera_main_agent_gateway.errors import AdapterConfigurationUnavailable

    adapter = UnavailableMainAgentAdapter("adapter_not_installed")

    async def exercise() -> None:
        operations = (
            adapter.health,
            lambda: adapter.list_threads(),
            lambda: adapter.create_thread(title="", context_refs=()),
            lambda: adapter.read_thread("thread-1"),
            lambda: adapter.start_turn("thread-1", "hello"),
            lambda: adapter.read_turn("thread-1", "turn-1"),
            lambda: adapter.interrupt_turn("thread-1", "turn-1"),
        )
        for operation in operations:
            with pytest.raises(AdapterConfigurationUnavailable) as raised:
                await operation()
            assert raised.value.code == "adapter_not_installed"
            assert str(raised.value) == "main agent adapter is unavailable"

        assert await adapter.capabilities() == {"reason": "adapter_not_installed"}
        await adapter.close()

    asyncio.run(exercise())


def test_adapter_registry_requires_a_matching_registration() -> None:
    from sidera_main_agent_gateway.adapter_registry import (
        MainAgentAdapterRegistration,
        select_main_agent_adapter,
    )
    from sidera_main_agent_gateway.errors import AdapterConfigurationUnavailable

    settings = _akashic_settings()
    with pytest.raises(AdapterConfigurationUnavailable) as missing:
        select_main_agent_adapter(settings, {})
    assert missing.value.code == "adapter_not_registered"

    def unused_factory(settings, acceptance_guard):
        pytest.fail("mismatched registration factory must not be called")

    with pytest.raises(AdapterConfigurationUnavailable) as mismatch:
        select_main_agent_adapter(
            settings,
            {
                "akashic": MainAgentAdapterRegistration(
                    "interview-demo", unused_factory
                )
            },
        )
    assert mismatch.value.code == "adapter_registration_mismatch"


def test_adapter_registry_passes_settings_and_guard_by_identity() -> None:
    from sidera_main_agent_gateway.adapter_registry import (
        MainAgentAdapterRegistration,
        select_main_agent_adapter,
    )
    from sidera_main_agent_gateway.contracts import MainAgentAdapter

    settings = _akashic_settings()
    guard = cast(RuntimeBudgetGuard, object())
    adapter = cast(MainAgentAdapter, object())
    received: list[tuple[Settings, RuntimeBudgetGuard | None]] = []

    def factory(
        actual_settings: Settings,
        actual_guard: RuntimeBudgetGuard | None,
    ) -> MainAgentAdapter:
        received.append((actual_settings, actual_guard))
        return adapter

    result = select_main_agent_adapter(
        settings,
        {"akashic": MainAgentAdapterRegistration("akashic", factory)},
        acceptance_guard=guard,
    )

    assert result is adapter
    assert received == [(settings, guard)]
    assert received[0][1] is guard


def test_missing_akashic_sdk_returns_adapter_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidera_main_agent_gateway import composition
    from sidera_main_agent_gateway.api.app import create_app

    find_spec = composition.find_spec

    def without_akashic_sdk(name: str) -> ModuleSpec | None:
        if name == "akashic_sdk":
            return None
        return find_spec(name)

    monkeypatch.setattr(composition, "find_spec", without_akashic_sdk)

    with TestClient(create_app(_akashic_settings())) as client:
        health = client.get("/main-agent/v1/health")
        response = client.get("/main-agent/v1/threads")

    assert health.status_code == 200
    assert health.json()["provider"] == "unavailable"
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "adapter_not_installed"
    assert response.json()["error"]["retryable"] is False


def test_missing_demo_bridge_returns_adapter_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidera_main_agent_gateway import composition
    from sidera_main_agent_gateway.api.app import create_app

    find_spec = composition.find_spec

    def without_bridge(name: str) -> ModuleSpec | None:
        if name == "sidera_task_bridge":
            return None
        return find_spec(name)

    monkeypatch.setattr(composition, "find_spec", without_bridge)

    with TestClient(create_app(_demo_settings())) as client:
        health = client.get("/main-agent/v1/health")
        response = client.get("/main-agent/v1/threads")

    assert health.status_code == 200
    assert health.json()["provider"] == "unavailable"
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "adapter_not_installed"
    assert response.json()["error"]["retryable"] is False


def test_demo_selection_does_not_import_akashic_sdk() -> None:
    code = """
import importlib, sys
from pathlib import Path
from uuid import UUID
from sidera_main_agent_gateway.adapters import demo
from sidera_main_agent_gateway import composition
from sidera_main_agent_gateway.config import Settings

class SelectedDemo:
    def __init__(self, settings, *, acceptance_guard=None):
        self.settings = settings
        self.acceptance_guard = acceptance_guard

demo.DemoMainAgentAdapter = SelectedDemo
settings = Settings(
    adapter_kind="interview-demo",
    demo_manifest=Path('/tmp/unused-manifest.json'),
    sidera_workspace_id=UUID('20000000-0000-0000-0000-000000000001'),
)
adapter = composition.build_main_agent_adapter(settings)
assert isinstance(adapter, SelectedDemo)
assert 'akashic_sdk' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_internal_akashic_import_failure_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidera_main_agent_gateway import composition

    real_import = builtins.__import__

    def broken_adapter_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sidera_main_agent_gateway.adapters.akashic":
            raise ModuleNotFoundError(
                "internal adapter dependency is broken", name="internal_dependency"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(composition, "find_spec", lambda _name: object())
    monkeypatch.setattr(builtins, "__import__", broken_adapter_import)

    with pytest.raises(ModuleNotFoundError, match="internal adapter dependency") as raised:
        composition.build_main_agent_adapter(_akashic_settings())

    assert raised.value.name == "internal_dependency"


def test_injected_adapter_bypasses_composition_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidera_main_agent_gateway import composition
    from sidera_main_agent_gateway.api.app import create_app
    from sidera_main_agent_gateway.contracts import MainAgentAdapter

    def unexpected_composition(*args, **kwargs):
        pytest.fail("injected adapter must bypass composition")

    monkeypatch.setattr(
        composition, "build_main_agent_adapter", unexpected_composition
    )
    adapter = _CloseTrackingAdapter()

    with TestClient(
        create_app(_akashic_settings(), adapter=cast(MainAgentAdapter, adapter))
    ) as client:
        assert client.get("/main-agent/v1/health").json()["provider"] == "available"

    assert adapter.close_calls == 1


def test_composition_receives_original_guard_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidera_main_agent_gateway import composition
    from sidera_main_agent_gateway.api.app import create_app
    from sidera_main_agent_gateway.contracts import MainAgentAdapter

    settings = _akashic_settings()
    guard = cast(RuntimeBudgetGuard, object())
    adapter = _CloseTrackingAdapter()
    received: list[tuple[Settings, RuntimeBudgetGuard | None]] = []

    def build(
        actual_settings: Settings,
        *,
        acceptance_guard: RuntimeBudgetGuard | None = None,
    ) -> MainAgentAdapter:
        received.append((actual_settings, acceptance_guard))
        return cast(MainAgentAdapter, adapter)

    monkeypatch.setattr(composition, "build_main_agent_adapter", build)

    with TestClient(create_app(settings, acceptance_guard=guard)) as client:
        assert client.get("/main-agent/v1/health").json()["provider"] == "available"

    assert received == [(settings, guard)]
    assert received[0][1] is guard
    assert adapter.close_calls == 1
