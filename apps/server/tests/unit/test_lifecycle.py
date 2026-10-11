import importlib.util
import sys
from datetime import UTC, datetime
from threading import Event, Thread
from uuid import uuid4

import httpx
import pytest
from sidera_agents.contracts import RuntimeRef, RuntimeSnapshot
from sidera_agents.errors import AgentError


def lifecycle_module():
    assert importlib.util.find_spec("sidera_product.lifecycle") is not None, (
        "N17 must provide a bounded product owner lifecycle"
    )
    from sidera_product import lifecycle

    return lifecycle


def test_inert_close_is_repeatable_and_fences_restart(
    settings_values, no_database_connections
):
    lifecycle_module()
    from sidera_product.composition import build_product_runtime
    from sidera_product.config import ProductSettings

    runtime = build_product_runtime(ProductSettings.model_validate(settings_values))
    assert runtime.status()["execution"]["ready"] is False
    assert runtime.close(grace_seconds=0) is True
    assert runtime.close(grace_seconds=0) is True
    assert runtime.status()["lifecycle"] == "closed"
    with pytest.raises(RuntimeError, match="product_closed"):
        runtime.start()


@pytest.mark.anyio
async def test_product_lifespan_calls_public_owner_once_not_child_lifespan(
    settings_values, no_database_connections, monkeypatch
):
    lifecycle_module()
    from sidera_product.app import create_app
    from sidera_product.config import ProductSettings

    app = create_app(ProductSettings.model_validate(settings_values))
    runtime = app.state.product_runtime
    calls = []

    # Lifecycle wiring only; real ownership/Run drain is exercised in guarded PG tests.
    def start():
        calls.append("start")

    real_close = runtime.close

    def close(*, grace_seconds=None):
        calls.append("close")
        return real_close(grace_seconds=grace_seconds)

    monkeypatch.setattr(runtime, "start", start)
    monkeypatch.setattr(runtime, "close", close)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18080"
        ) as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
    assert calls == ["start", "close"]


@pytest.fixture
def controlled_owner_runtime(settings_values, no_database_connections, monkeypatch):
    from sidera_product import lifecycle
    from sidera_product.composition import build_product_runtime
    from sidera_product.config import ProductSettings

    runtime = build_product_runtime(ProductSettings.model_validate(settings_values))
    calls = []
    monkeypatch.setattr(runtime.product_store, "readiness", lambda: True)
    monkeypatch.setattr(runtime.agents, "readiness", lambda **kwargs: True)
    monkeypatch.setattr(runtime.control_plane, "readiness", lambda: True)
    monkeypatch.setattr(lifecycle, "credential_schema_ready", lambda engine: True)

    def empty_conversations(*, workspace_id, principal_id):
        assert workspace_id == runtime.settings.workspace_id
        assert principal_id == runtime.settings.local_principal_id
        return ()

    monkeypatch.setattr(runtime.chat.store, "list_conversations", empty_conversations)
    snapshot = RuntimeSnapshot(
        ref=RuntimeRef(workspace_id=runtime.settings.workspace_id, runtime_id=uuid4()),
        harness_id="controlled-owner-start",
        executable=sys.executable,
        actual_version="controlled-1",
        installed=True,
        enabled=True,
        version=1,
        protocol_state="ready",
        auth_state="ready",
        capabilities={},
        checked_at=datetime.now(UTC),
        limits={"max_active": 1, "max_sessions": 1},
    )
    monkeypatch.setattr(
        runtime.agents.runtimes, "list_runtimes", lambda *args, **kwargs: (snapshot,)
    )

    def start_agents(**kwargs):
        calls.append("agents.start")

    def start_cp(*, background_identity):
        assert background_identity is runtime.background_identity
        calls.append("cp.start")

    monkeypatch.setattr(runtime.agents, "start_background", start_agents)
    monkeypatch.setattr(runtime.control_plane, "start_background", start_cp)

    def record_close(owner, method, label):
        original = getattr(owner, method)

        def close(*args, **kwargs):
            calls.append(label)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, method, close)

    record_close(runtime.control_plane, "close", "cp.close")
    record_close(runtime.chat, "close", "chat.close")
    record_close(runtime.agents, "close", "agents.close")
    record_close(runtime.agent_store, "close", "agents.store.close")
    record_close(runtime.credential_engine, "dispose", "credentials.dispose")
    record_close(runtime.product_store, "close", "product.store.close")
    try:
        yield runtime, calls
    finally:
        assert runtime.close(grace_seconds=0) is True


def test_cold_main_chat_status_is_unavailable_and_repeated_reads_are_inert(
    settings_values, no_database_connections
):
    from sidera_product.composition import build_product_runtime
    from sidera_product.config import ProductSettings

    runtime = build_product_runtime(ProductSettings.model_validate(settings_values))
    try:
        first, second = runtime.status(), runtime.status()
        assert first == second
        assert first["execution"]["ready"] is False
        assert first["main_agent"] == {
            "available": False,
            "code": "product_unavailable",
        }
    finally:
        assert runtime.close(grace_seconds=0) is True


def test_main_chat_status_matches_actual_ready_and_cleanup_fence_without_owner_calls(
    controlled_owner_runtime, monkeypatch
):
    runtime, calls = controlled_owner_runtime
    runtime.start()
    before = list(calls)
    for _ in range(2):
        status = runtime.status()
        assert status["execution"]["ready"] is True
        assert status["main_agent"] == {"available": True, "code": None}
    assert calls == before
    original_close = runtime.control_plane.close
    monkeypatch.setattr(runtime.control_plane, "close", lambda **kwargs: False)
    try:
        assert runtime.close(grace_seconds=0) is False
        before = list(calls)
        for _ in range(2):
            status = runtime.status()
            assert status["lifecycle"] == "cleanup_incomplete"
            assert status["execution"]["ready"] is False
            assert status["main_agent"] == {
                "available": False,
                "code": "product_unavailable",
            }
        assert calls == before
    finally:
        monkeypatch.setattr(runtime.control_plane, "close", original_close)
    assert runtime.close(grace_seconds=0) is True


@pytest.mark.parametrize("owner_name", ["agents", "control_plane"])
def test_close_during_owner_start_is_a_monotonic_admission_fence(
    controlled_owner_runtime, monkeypatch, owner_name
):
    runtime, calls = controlled_owner_runtime
    entered, release = Event(), Event()
    owner = getattr(runtime, owner_name)
    original_start = owner.start_background
    errors = []

    def held_start(**kwargs):
        original_start(**kwargs)
        entered.set()
        assert release.wait(3)

    def start():
        try:
            runtime.start()
        except RuntimeError as error:
            errors.append(str(error))

    monkeypatch.setattr(owner, "start_background", held_start)
    thread = Thread(target=start, name="n17-owned-start-close-test")
    thread.start()
    try:
        assert entered.wait(3)
        assert runtime.close(grace_seconds=0) is False
        with pytest.raises(AgentError) as denied:
            runtime.require_write_admission()
        assert denied.value.code == "product_unavailable"
        assert runtime.status()["execution"]["ready"] is False
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    with pytest.raises(AgentError) as denied:
        runtime.require_write_admission()
    assert denied.value.code == "product_unavailable"
    assert runtime.status()["execution"]["ready"] is False
    assert runtime.status()["lifecycle"] == "closed"
    assert errors == ["product_startup_failed"]
    starts = ["agents.start"]
    if owner_name == "control_plane":
        starts.append("cp.start")
    assert calls == starts + [
        "cp.close",
        "chat.close",
        "agents.close",
        "agents.store.close",
        "credentials.dispose",
        "product.store.close",
    ]
    assert runtime.close(grace_seconds=0) is True
    with pytest.raises(RuntimeError, match="product_closed"):
        runtime.start()


def test_close_before_start_never_starts_an_owner(controlled_owner_runtime):
    runtime, calls = controlled_owner_runtime
    assert runtime.close(grace_seconds=0) is True
    with pytest.raises(RuntimeError, match="product_closed"):
        runtime.start()
    with pytest.raises(AgentError) as denied:
        runtime.require_write_admission()
    assert denied.value.code == "product_unavailable"
    assert calls == [
        "cp.close",
        "chat.close",
        "agents.close",
        "agents.store.close",
        "credentials.dispose",
        "product.store.close",
    ]


def test_ready_close_retains_original_owners_until_confirmed(
    controlled_owner_runtime, monkeypatch
):
    runtime, calls = controlled_owner_runtime
    runtime.start()
    assert runtime.status()["execution"]["ready"] is True
    runtime.require_write_admission()
    original_close = runtime.control_plane.close

    def pending_close(**kwargs):
        calls.append("cp.pending")
        return False

    monkeypatch.setattr(runtime.control_plane, "close", pending_close)
    try:
        assert runtime.close(grace_seconds=0) is False
        assert runtime.status()["lifecycle"] == "cleanup_incomplete"
        assert runtime.status()["execution"]["ready"] is False
        with pytest.raises(AgentError) as denied:
            runtime.require_write_admission()
        assert denied.value.code == "product_unavailable"
        assert calls == ["agents.start", "cp.start", "cp.pending"]
    finally:
        monkeypatch.setattr(runtime.control_plane, "close", original_close)
    assert runtime.close(grace_seconds=0) is True
    assert calls == [
        "agents.start",
        "cp.start",
        "cp.pending",
        "cp.close",
        "chat.close",
        "agents.close",
        "agents.store.close",
        "credentials.dispose",
        "product.store.close",
    ]
    assert runtime.close(grace_seconds=0) is True
    assert calls.count("cp.close") == calls.count("agents.close") == 1
