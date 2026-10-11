import ast
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_agents import bootstrap, ports
from sidera_agents.errors import AgentError


@pytest.fixture
def lab(admission_lab):
    return admission_lab


def _standalone():
    path = Path(__file__).resolve().parents[2] / "examples" / "standalone.py"
    assert path.is_file(), "The public standalone consumer is missing"
    spec = spec_from_file_location("agents_standalone_contract", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, ast.parse(path.read_text())


def test_second_harness_runs_through_only_public_consumer_contracts():
    example, tree = _standalone()
    allowed = {
        "sidera_agents.api",
        "sidera_agents.bootstrap",
        "sidera_agents.contracts",
        "sidera_agents.errors",
        "sidera_agents.ports",
    }
    imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("sidera_agents")
    ]
    assert imports and set(imports) <= allowed
    assert not any(
        isinstance(node, ast.Import)
        and any(alias.name.startswith("sidera_agents") for alias in node.names)
        for node in ast.walk(tree)
    )
    consumer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    vendor_branches = [
        node
        for node in ast.walk(consumer)
        if isinstance(node, (ast.If, ast.Match))
        and any(
            isinstance(child, ast.Attribute)
            and child.attr in {"harness_id", "session_id", "process_identity"}
            for child in ast.walk(node)
        )
    ]
    assert vendor_branches == []
    assert example.main() == {
        "harness_id": "fixture-harness",
        "state": "completed",
        "messages": ["Fixture response."],
        "closed": True,
        "profile_status": {"enabled": False, "version": 2},
        "resource_status": {"enabled": False, "version": 2},
    }


def test_public_registry_selects_exact_adapters_and_empty_does_not_default(lab):
    create = getattr(bootstrap, "create_harness_registry", None)
    assert callable(create), "Public registry composition is missing"
    assert create((lab.native,)).all() == (lab.native,)
    assert create(iter((lab.native,))).get("controlled") is lab.native
    empty = create(())
    assert empty.all() == ()
    with pytest.raises(AgentError) as unsupported:
        empty.get("opencode")
    assert unsupported.value.code == "unsupported_harness"


@pytest.mark.parametrize(
    "method",
    [
        "describe",
        "probe",
        "validate",
        "open",
        "prompt",
        "read_events",
        "cancel",
        "inspect",
        "close",
    ],
)
def test_public_registry_rejects_noncallable_adapter_methods(lab, method):
    create = getattr(bootstrap, "create_harness_registry", None)
    assert callable(create), "Public registry composition is missing"
    members = {
        name: getattr(lab.native, name)
        for name in (
            "describe",
            "probe",
            "validate",
            "open",
            "prompt",
            "read_events",
            "cancel",
            "inspect",
            "close",
        )
    }
    members[method] = None
    adapter = SimpleNamespace(
        harness_id="fixture-harness", executable_name="fixture", **members
    )
    with pytest.raises(ValueError):
        create((adapter,))
    assert lab.native.open_calls == [] and lab.native.prompt_calls == []


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("harness_id", ""),
        ("harness_id", " "),
        ("harness_id", None),
        ("harness_id", 1),
        ("executable_name", ""),
        ("executable_name", None),
    ],
)
def test_public_registry_rejects_invalid_identity_before_execution(
    lab, attribute, value
):
    create = getattr(bootstrap, "create_harness_registry", None)
    assert callable(create), "Public registry composition is missing"
    members = dict(vars(lab.native))
    members.update(
        {
            name: getattr(lab.native, name)
            for name in (
                "harness_id",
                "executable_name",
                "describe",
                "probe",
                "validate",
                "open",
                "prompt",
                "read_events",
                "cancel",
                "inspect",
                "close",
            )
        }
    )
    members[attribute] = value
    with pytest.raises(ValueError):
        create((SimpleNamespace(**members),))


def test_public_registry_rejects_duplicates_and_metadata_only_descriptors(lab):
    create = getattr(bootstrap, "create_harness_registry", None)
    assert callable(create), "Public registry composition is missing"
    with pytest.raises(ValueError):
        create((lab.native, lab.native))
    metadata = SimpleNamespace(
        harness_id="fixture-harness",
        executable_name="fixture",
        describe=lab.native.describe,
        probe=lab.native.probe,
        validate=lab.native.validate,
    )
    with pytest.raises(ValueError):
        create((metadata,))


def test_public_composition_is_lazy_and_cannot_spawn_or_read_native_auth():
    code = """
import pathlib
import subprocess
import sys
from datetime import datetime
from typing import get_type_hints
from sidera_agents.bootstrap import create_harness_registry, open_memory_store
from sidera_agents.ports import HarnessAdapter

assert not any(
    name.startswith(('sqlalchemy', 'alembic', 'psycopg', 'sidera_agents.postgres',
                     'sidera_agents.harnesses.opencode', 'sidera_agents.harnesses.acp_transport'))
    for name in sys.modules
)
def audit(event, args):
    if event == 'subprocess.Popen':
        raise AssertionError('Composition must not spawn')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        name = pathlib.Path(args[0]).name if isinstance(args[0], str) else ''
        if name in {'auth.json', 'opencode.json', 'opencode.jsonc'}:
            raise AssertionError('Composition must not read native auth/config')
sys.addaudithook(audit)
first = open_memory_store()
second = open_memory_store()
assert first is not second
assert create_harness_registry(()).all() == ()
assert [adapter.harness_id for adapter in create_harness_registry().all()] == ['opencode']
assert get_type_hints(HarnessAdapter) == {'harness_id': str, 'executable_name': str}
assert get_type_hints(HarnessAdapter.probe)['checked_at'] is datetime
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _forbid_readiness_mutation(lab, scoped):
    attempts = []

    def unexpected(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Readiness performed execution or ownership mutation")

    scoped.setattr(lab.store, "acquire_execution_owner", unexpected)
    scoped.setattr(lab.store, "release_execution_owner", unexpected)
    scoped.setattr(lab.service, "poll_once", unexpected)
    scoped.setattr(lab.execution, "poll_once", unexpected)
    for method in (
        "probe",
        "open",
        "prompt",
        "read_events",
        "cancel",
        "inspect",
        "close",
    ):
        scoped.setattr(lab.native, method, unexpected)
    return attempts


def test_owner_strict_readiness_is_false_before_start_without_acquiring(
    lab, monkeypatch
):
    fresh = bootstrap.create_agent_service(
        lab.store,
        lab.registry,
        lab.authorizer,
        ports.DenyContextSourceReader(),
        ports.DenySecretReader(),
        lab.clock,
    )
    try:
        installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
        with monkeypatch.context() as scoped:
            attempts = _forbid_readiness_mutation(lab, scoped)
            assert fresh.readiness() is True
            assert fresh.readiness(require_execution_owner=True) is False
            assert attempts == []
        assert lab.store.get_runtime_entry(lab.runtime.ref).installation == installation
        assert lab.native.open_calls == [] and lab.native.prompt_calls == []
    finally:
        fresh.close()


def test_owner_strict_manual_readiness_preserves_run_evidence_and_native_counts(
    lab, monkeypatch
):
    receipt = lab.execution.start(lab.request())
    lab.pump()
    snapshot = lab.execution.get_run(
        lab.workspace_id, receipt.run_id, grant_id=lab.grant_id
    )
    page = lab.service.observations.read_trace(
        lab.workspace_id, receipt.run_id, 0, grant_id=lab.grant_id
    )
    installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
    native_counts = (len(lab.native.open_calls), len(lab.native.prompt_calls))
    calls = tuple(lab.authorizer.calls)
    with monkeypatch.context() as scoped:
        attempts = _forbid_readiness_mutation(lab, scoped)
        for _ in range(3):
            assert lab.service.readiness() is True
            assert lab.service.readiness(require_execution_owner=True) is True
        assert attempts == []
    assert tuple(lab.authorizer.calls) == calls
    assert (
        lab.execution.get_run(lab.workspace_id, receipt.run_id, grant_id=lab.grant_id)
        == snapshot
    )
    assert (
        lab.service.observations.read_trace(
            lab.workspace_id, receipt.run_id, 0, grant_id=lab.grant_id
        )
        == page
    )
    assert lab.store.get_runtime_entry(lab.runtime.ref).installation == installation
    assert (
        (len(lab.native.open_calls), len(lab.native.prompt_calls))
        == native_counts
        == (1, 1)
    )


@pytest.mark.parametrize("answer", [False, None, 1, "error"])
def test_owner_strict_readiness_fails_closed_on_unconfirmed_query(
    lab, monkeypatch, answer
):
    original = lab.store.execution_owner_is_held

    def query(owner_id):
        assert owner_id == lab.service._owner_id
        if answer == "error":
            raise RuntimeError("PRIVATE_DRIVER_DETAIL_MUST_NOT_ESCAPE")
        return answer

    with monkeypatch.context() as scoped:
        attempts = _forbid_readiness_mutation(lab, scoped)
        scoped.setattr(lab.store, "execution_owner_is_held", query)
        assert lab.service.readiness() is True
        assert lab.service.readiness(require_execution_owner=True) is False
        assert attempts == []
    assert original(lab.service._owner_id) is True
    assert lab.native.open_calls == [] and lab.native.prompt_calls == []


@pytest.mark.parametrize("failed", [False, True])
def test_owner_strict_readiness_requires_storage_without_an_owner_query(
    lab, monkeypatch, failed
):
    def storage_ready():
        if failed:
            raise RuntimeError("PRIVATE_STORAGE_DETAIL_MUST_NOT_ESCAPE")
        return False

    def unexpected_query(owner_id):
        raise AssertionError("Unready storage must not query execution ownership")

    with monkeypatch.context() as scoped:
        attempts = _forbid_readiness_mutation(lab, scoped)
        scoped.setattr(lab.store, "readiness", storage_ready, raising=False)
        scoped.setattr(lab.store, "execution_owner_is_held", unexpected_query)
        assert lab.service.readiness(require_execution_owner=True) is False
        if failed:
            with pytest.raises(RuntimeError, match="PRIVATE_STORAGE_DETAIL"):
                lab.service.readiness()
        else:
            assert lab.service.readiness() is False
        assert attempts == []


def test_owner_strict_readiness_rejects_transferred_owner_without_reacquiring(
    lab, monkeypatch
):
    replacement_owner = uuid4()
    lab.store.release_execution_owner(lab.service._owner_id)
    assert lab.store.acquire_execution_owner(replacement_owner) is True
    try:
        with monkeypatch.context() as scoped:
            attempts = _forbid_readiness_mutation(lab, scoped)
            assert lab.service.readiness() is True
            assert lab.service.readiness(require_execution_owner=True) is False
            assert attempts == []
        assert lab.store.execution_owner_is_held(replacement_owner) is True
    finally:
        lab.store.release_execution_owner(replacement_owner)


def test_owner_strict_background_readiness_does_not_poll_while_tick_is_blocked(
    lab, monkeypatch
):
    entered, release = Event(), Event()
    actual_poll = lab.execution.poll_once

    def blocked_tick(**kwargs):
        result = actual_poll(**kwargs)
        entered.set()
        assert release.wait(timeout=3)
        return result

    monkeypatch.setattr(lab.execution, "poll_once", blocked_tick)
    lab.service.start_background(interval_seconds=0.01)
    try:
        assert entered.wait(timeout=1)
        installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
        with monkeypatch.context() as scoped:
            attempts = _forbid_readiness_mutation(lab, scoped)
            assert lab.service.readiness(require_execution_owner=True) is True
            assert attempts == []
        assert lab.store.get_runtime_entry(lab.runtime.ref).installation == installation
        assert lab.native.open_calls == [] and lab.native.prompt_calls == []
    finally:
        release.set()
        lab.service.close()


def test_owner_strict_readiness_rejects_a_stopped_background_thread(lab, monkeypatch):
    def stopped_tick(**kwargs):
        raise AgentError("execution_owner_unavailable", "Controlled tick exit.")

    monkeypatch.setattr(lab.execution, "poll_once", stopped_tick)
    lab.service.start_background(interval_seconds=0.01)
    thread = lab.service._thread
    assert thread is not None
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert lab.store.execution_owner_is_held(lab.service._owner_id) is True
    with monkeypatch.context() as scoped:
        attempts = _forbid_readiness_mutation(lab, scoped)
        assert lab.service.readiness() is True
        assert lab.service.readiness(require_execution_owner=True) is False
        assert attempts == []


def test_owner_strict_readiness_rejects_closing_with_a_still_held_manual_owner(
    lab, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    entered, release = Event(), Event()
    actual_prompt = lab.native.prompt

    def blocked_prompt(*args):
        entered.set()
        assert release.wait(timeout=3)
        return actual_prompt(*args)

    monkeypatch.setattr(lab.native, "prompt", blocked_prompt)
    lab.execution.start(lab.request())
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(lab.pump)
        try:
            assert entered.wait(timeout=1)
            with pytest.raises(AgentError) as pending:
                lab.service.close(timeout=0.01)
            assert pending.value.code == "shutdown_pending"
            assert lab.store.execution_owner_is_held(lab.service._owner_id) is True
            with monkeypatch.context() as scoped:
                attempts = _forbid_readiness_mutation(lab, scoped)
                assert lab.service.readiness(require_execution_owner=True) is False
                assert attempts == []
        finally:
            release.set()
            worker.result(timeout=2)
