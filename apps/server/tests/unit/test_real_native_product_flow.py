import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Lock, Thread
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import SecretStr

from tests.support.product_targets import ProductTargetError, select_targets


def real_module():
    from tests.support import real_native_product_flow

    return real_native_product_flow


def inert_real_targets(tmp_path):
    allocation = "sidera-native-harness:product:n17:20261008:realunit"
    environ = {
        "SIDERA_PRODUCT_TEST_ALLOCATION_ID": allocation,
        "SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY": str(tmp_path / "no-db"),
        "SIDERA_PRODUCT_TEST_PORT": "5432",
    }
    for component, kind in (
        ("product", "product"),
        ("agents", "agents"),
        ("cp", "native_cp"),
        ("credentials", "cfg2"),
    ):
        from sqlalchemy.engine import URL

        database = f"sidera_{kind}_test_n17_realunit"
        owner = f"n17_{component}_realunit"
        prefix = "SIDERA_PRODUCT_TEST_" + component.upper() + "_"
        environ.update(
            {
                prefix + "DATABASE_URL": URL.create(
                    "postgresql+psycopg",
                    username=owner,
                    database=database,
                    query={
                        "host": environ["SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY"],
                        "port": "5432",
                    },
                ).render_as_string(hide_password=False),
                prefix + "EXPECTED_DATABASE": database,
                prefix + "EXPECTED_OWNER": owner,
                prefix + "EXPECTED_MARKER": allocation + ":" + component,
            }
        )
    return select_targets(environ)


@pytest.fixture
def real_case(tmp_path, monkeypatch, no_database_connections):
    targets = inert_real_targets(tmp_path)
    calls = []
    guards = []
    for target in targets:
        guard = SimpleNamespace(target=target, active=True, idle=0)
        guard.inspect = lambda guard=guard: {"idle": {"other_clients": guard.idle}}
        guards.append(guard)
    executable = tmp_path / "private-native-byte-fixture"
    executable.write_bytes(b"PRIVATE EXECUTABLE BYTES; NEVER INVOKE\n")
    executable.chmod(0o700)
    directory, evidence = tmp_path / "owned", tmp_path / "evidence"
    directory.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)

    def build(*, bad=None):
        module = real_module()
        entry = module.RealNativeEntry(
            executable=executable,
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            actual_runtime_version="1.18.32",
            verification_python=Path(sys.executable),
            selected_secret=SecretStr("REAL-TEST-SENTINEL-NOT-A-KEY"),
        )
        changes = {
            "relative_executable": {"executable": Path("relative-native")},
            "wrong_hash": {"executable_sha256": "f" * 64},
            "wrong_version": {"actual_runtime_version": "1.18.31"},
            "wrong_route": {"model_id": "opencode-go/other"},
            "empty_secret": {"selected_secret": SecretStr("")},
        }
        entry = replace(entry, **changes.get(bad, {}))
        if bad == "foreign_guard":
            guards[0].target = targets[1]
        elif bad == "nonidle_guard":
            guards[0].idle = 1
        elif bad == "nonempty_directory":
            (directory / "foreign.txt").write_text("existing input")

        def refuse(*args, **kwargs):
            calls.append("owned-side-effect")
            pytest.fail("Construction crossed an owned side-effect boundary")

        for name in (
            "local_git",
            "_bind_listener",
            "_reserve_port",
            "OwnedKnowledgeServer",
            "build_product_runtime",
            "start_owned_backend",
            "start_owned_vite",
        ):
            monkeypatch.setattr(module, name, refuse, raising=False)
        return module.build_real_native_product_flow_lab(
            targets, guards, directory, evidence, entry=entry, start_web=False
        )

    return SimpleNamespace(
        build=build,
        side_effects=calls,
        directory=directory,
        evidence=evidence,
        targets=targets,
        guards=guards,
        executable=executable,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "relative_executable",
        "wrong_hash",
        "wrong_version",
        "wrong_route",
        "empty_secret",
        "foreign_guard",
        "nonidle_guard",
        "nonempty_directory",
    ],
)
def test_real_entry_refused_before_owned_start(real_case, bad):
    with pytest.raises(ProductTargetError):
        real_case.build(bad=bad)
    assert real_case.side_effects == []


def start_boundaries(
    real_case, monkeypatch, *, fail_backend=False, fail_knowledge_start=False
):
    module = real_module()
    lab = real_case.build()
    calls = []
    source = real_case.evidence / "source"
    source.mkdir()
    for name in module.REAL_SOURCE_PATHS:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private source fixture\n")
    monkeypatch.setattr(module, "SOURCE_ROOT", source)
    monkeypatch.setattr(
        module,
        "local_git",
        lambda *args: calls.append(("git-boundary", args)) or "a" * 40,
    )
    listener = SimpleNamespace(closed=False)

    def close_listener():
        calls.append(("listener",))
        listener.closed = True

    listener.close = close_listener
    listener.fileno = lambda: -1 if listener.closed else 7
    monkeypatch.setattr(module, "_bind_listener", lambda *args: (listener, 43122))
    monkeypatch.setattr(module, "_reserve_port", lambda *args: 43121)

    class KnowledgeDouble:
        def __init__(self, workspace, *, include_page_list=False):
            self.workspace = workspace
            self.include_page_list = include_page_list
            self.url = "http://127.0.0.1:43123"
            self.token = SecretStr("KNOWLEDGE-TEST-SENTINEL-NOT-A-TOKEN")
            self.started = False
            calls.append(("knowledge-constructor", workspace, include_page_list))

        def start(self):
            assert lab.manifest_path.is_file()
            assert (lab.directory / "owner.json").is_file()
            self.started = True
            calls.append(("knowledge-start",))
            if fail_knowledge_start:
                raise RuntimeError(self.token.get_secret_value())

        def close(self):
            calls.append(("knowledge-close",))
            self.started = False
            return True

    monkeypatch.setattr(module, "OwnedKnowledgeServer", KnowledgeDouble, raising=False)
    runtime = SimpleNamespace(
        close=lambda **kwargs: calls.append(("runtime",)) or True,
        local_sessions=SimpleNamespace(create_bootstrap=lambda: object()),
    )

    def compose(settings, *, harnesses):
        from sidera_agents.harnesses.opencode import OpenCodeHarness

        assert type(harnesses[0]) is OpenCodeHarness
        assert settings is lab.settings
        assert settings.managed_credentials_enabled is False
        assert settings.selected_secrets == {
            "SIDERA_N23_OPENCODE_API_KEY": lab.entry.selected_secret
        }
        assert harnesses[0]._parent_environment == {}
        assert (
            harnesses[0]._request_timeout,
            harnesses[0]._prompt_timeout,
            harnesses[0]._cancel_timeout,
            harnesses[0]._close_timeout,
            harnesses[0]._stop_grace,
        ) == (120, 900, 120, 120, 10)
        knowledge = getattr(lab, "knowledge", None)
        runtime.knowledge_at_composition = (
            settings.knowledge_base_url,
            settings.knowledge_token,
            knowledge,
            getattr(knowledge, "started", False),
        )
        calls.append(("compose",))
        return runtime

    monkeypatch.setattr(module, "build_product_runtime", compose)
    monkeypatch.setattr(module, "create_app", lambda **kwargs: object())

    def backend(*args, resource_owner, **kwargs):
        assert resource_owner.manifest_path.is_file()
        assert Path(resource_owner.manifest["config_path"]).is_file()
        resource_owner.backend = SimpleNamespace(
            close=lambda: calls.append(("backend",)) or True
        )
        if fail_backend:
            raise RuntimeError("REAL-TEST-SENTINEL-NOT-A-KEY")

    monkeypatch.setattr(module, "start_owned_backend", backend)
    monkeypatch.setattr(
        module,
        "write_bootstrap_file",
        lambda issue, *, output: output.write_text("private bootstrap fixture"),
    )
    monkeypatch.setattr(
        module,
        "OwnedBootstrapExport",
        SimpleNamespace(
            capture=lambda path: SimpleNamespace(
                close=lambda: calls.append(("bootstrap",)) or True
            )
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("Native process allocated"),
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("Executable invoked")
    )
    return module, lab, calls


def test_real_composition_uses_actual_adapter_and_private_in_memory_secret(
    real_case, monkeypatch
):
    _, lab, calls = start_boundaries(real_case, monkeypatch)
    lab.start()
    assert ("compose",) in calls
    assert lab.manifest["mode"] == "real"
    assert lab.manifest["controlled_bridge"] is False
    assert lab.manifest["controller_url"] is None
    config = json.loads(Path(lab.manifest["config_path"]).read_text())
    transport = json.loads(Path(lab.manifest["transport_path"]).read_text())
    assert config["settings"]["max_sessions"] == 8
    assert config["verification"] == {
        "seed_sha256": hashlib.sha256(
            (lab.repository / "verify_demo.py").read_bytes()
        ).hexdigest(),
        "argv": [str(lab.entry.verification_python), "-B", "verify_demo.py"],
        "timeout_seconds": 60,
    }
    assert set(transport) == {"schema_version", "workspace_id", "bootstrap_file"}
    for value in (
        repr(lab),
        repr(lab.entry),
        repr(lab.settings),
        json.dumps(config),
        json.dumps(transport),
        json.dumps(lab.manifest),
    ):
        assert "REAL-TEST-SENTINEL-NOT-A-KEY" not in value
        assert "KNOWLEDGE-TEST-SENTINEL-NOT-A-TOKEN" not in value
    assert set(path.name for path in lab.repository.iterdir()) == {
        "README.md",
        "verify_demo.py",
        "inputs",
    }
    assert not any(
        (lab.repository / name).exists()
        for name in (
            "config_contract.json",
            "config_cli.py",
            "test_config_cli.py",
            "integration_report.md",
        )
    )
    assert lab.close() is True


def test_knowledge_prerequisite_uses_same_owned_catalog_before_composition(
    real_case, monkeypatch
):
    _, lab, calls = start_boundaries(real_case, monkeypatch)
    assert getattr(lab, "knowledge", None) is None
    try:
        lab.start()
        url, token, knowledge, started = lab.runtime.knowledge_at_composition
        assert url == "http://127.0.0.1:43123"
        assert knowledge is lab.knowledge
        assert knowledge.workspace == lab.workspace_id
        assert knowledge.include_page_list is True
        assert url == knowledge.url == lab.settings.knowledge_base_url
        assert token is knowledge.token is lab.settings.knowledge_token
        assert started is True
        assert (
            calls.index(("knowledge-constructor", lab.workspace_id, True))
            < calls.index(("knowledge-start",))
            < calls.index(("compose",))
        )
        assert lab.cleanup["knowledge_closed"] is False
        config = json.loads(Path(lab.manifest["config_path"]).read_text())
        assert config["knowledge_scope"] == "not_configured"
        assert lab.manifest["controller_url"] is None
        assert lab.manifest["controlled_bridge"] is False
        assert not hasattr(lab, "controller")
    finally:
        assert lab.close() is True
    assert lab.cleanup["knowledge_closed"] is True
    assert calls.index(("bootstrap",)) < calls.index(("knowledge-close",))


def test_knowledge_prerequisite_start_failure_retains_partial_owner_and_redacts_error(
    real_case, monkeypatch
):
    module, lab, calls = start_boundaries(
        real_case, monkeypatch, fail_knowledge_start=True
    )
    lab.owns_guards = True
    for guard in lab.guards:
        guard.inspect = lambda guard=guard: (
            calls.append(("inspect", guard.target.component))
            or {"idle": {"other_clients": guard.idle}}
        )
        guard.release = lambda guard=guard: (
            calls.append(("release", guard.target.component)) or True
        )
    with pytest.raises(
        ProductTargetError, match="^real_native_start_refused$"
    ) as error:
        lab.start()
    assert "KNOWLEDGE-TEST-SENTINEL-NOT-A-TOKEN" not in str(error.value)
    assert "KNOWLEDGE-TEST-SENTINEL-NOT-A-TOKEN" not in repr(error.value)
    assert error.value.__suppress_context__ is True
    assert isinstance(error.value.__context__, RuntimeError)
    assert str(error.value.__context__) == "KNOWLEDGE-TEST-SENTINEL-NOT-A-TOKEN"
    knowledge, listener = lab.knowledge, lab.listener
    assert knowledge.started is True
    assert listener.closed is False
    assert lab.runtime is None and lab.backend is None
    assert ("compose",) not in calls
    assert any(owner is lab for owner in module._retained_real_labs)
    assert lab.cleanup["knowledge_closed"] is False
    assert lab.cleanup["guards_inspected"] is None
    assert lab.cleanup["guards_released"] is None
    proofs = {path: path.read_bytes() for path in (lab.directory / "cleanup").iterdir()}
    assert len(proofs) == 1
    assert json.loads(next(iter(proofs.values())))["complete"] is False
    assert lab.close() is True
    assert lab.knowledge is knowledge
    assert knowledge.started is False
    assert listener.closed is True
    assert lab.cleanup["knowledge_closed"] is True
    assert calls.index(("listener",)) < calls.index(("knowledge-close",))
    assert calls.index(("knowledge-close",)) < calls.index(("inspect", "product"))
    assert calls.index(("inspect", "credentials")) < calls.index(
        ("release", "credentials")
    )
    assert all(path.read_bytes() == raw for path, raw in proofs.items())


def test_real_planning_budget_host_manifest_round_trip_preserves_complete_config(
    real_case, monkeypatch
):
    _, lab, _ = start_boundaries(real_case, monkeypatch)
    cli_path = Path(__file__).resolve().parents[4] / "scripts/native-harness-check.py"
    spec = importlib.util.spec_from_file_location("real_budget_cli_reader", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    lab.start()
    try:
        config_path = Path(lab.manifest["config_path"])
        config_bytes = config_path.read_bytes()
        manifest_bytes = lab.manifest_path.read_bytes()
        config = json.loads(config_bytes)
        source_bytes = {
            name: (Path(lab.manifest["source_root"]) / name).read_bytes()
            for name in lab.manifest["source_pins"]
        }
        refusal = None
        manifest = None
        try:
            manifest = cli.read_manifest(lab.manifest_path)
        except cli.ManifestError as error:
            refusal = str(error)
        assert refusal is None, f"Actual host-produced manifest refused: {refusal}"
        assert manifest == {**lab.manifest, "_source_changes": []}
        assert manifest["workspace_id"] == str(lab.workspace_id)
        assert config["settings"]["workspace_id"] == manifest["workspace_id"]
        assert manifest["config_hash"] == cli.fingerprint(config)
        assert config["planning_budget"] == {
            "max_context_bytes": 1048576,
            "max_tokens": 200000,
            "max_cost_microunits": 1000000,
            "max_runtime_seconds": 3600,
            "max_revisions": 16,
            "max_nodes": 64,
            "max_concurrency": 4,
        }
        assert cli.validate_real_config(config) == config["real"]
        assert config_path.read_bytes() == config_bytes
        assert lab.manifest_path.read_bytes() == manifest_bytes
        assert source_bytes == {
            name: (Path(manifest["source_root"]) / name).read_bytes()
            for name in manifest["source_pins"]
        }
    finally:
        assert lab.close() is True


def test_real_missing_selected_source_refused_before_owned_start(
    real_case, monkeypatch
):
    module, lab, calls = start_boundaries(real_case, monkeypatch)
    (module.SOURCE_ROOT / "apps/web/tests/e2e/native-harness-real.spec.ts").unlink()
    with pytest.raises(ProductTargetError):
        lab.start()
    assert calls == []
    assert not lab.repository.exists()


def test_partial_start_keeps_normal_owner_and_redacts_errors(real_case, monkeypatch):
    _, lab, calls = start_boundaries(real_case, monkeypatch, fail_backend=True)
    with pytest.raises(ProductTargetError) as error:
        lab.start()
    assert "REAL-TEST-SENTINEL-NOT-A-KEY" not in str(error.value)
    assert lab.runtime is not None and lab.backend is not None
    assert lab.close() is True
    assert calls.index(("backend",)) < calls.index(("runtime",))


def cleanup_case(real_case, *, owns_guards=True):
    module = real_module()
    lab = real_case.build()
    lab.owns_guards = owns_guards
    (lab.directory / "cleanup").mkdir(mode=0o700)
    lab.manifest = {
        "schema_version": 1,
        "owner_uid": os.getuid(),
        "allocation_id": lab.allocation_id,
        "workspace_id": str(lab.workspace_id),
        "namespace": str(lab.namespace),
        "config_hash": "a" * 64,
        "owner_receipt_path": str(lab.directory / "owner.json"),
        "cleanup_path": str(lab.directory / "cleanup"),
        "process_identities": {"owner_pid": os.getpid()},
    }
    calls = []
    lab.native = module.OpenCodeHarness(parent_environment={})
    lab.runtime = SimpleNamespace(
        close=lambda **kwargs: calls.append("runtime") or True
    )
    lab.backend = SimpleNamespace(close=lambda: calls.append("backend") or True)
    lab.bootstrap_export = SimpleNamespace(
        close=lambda: calls.append("bootstrap") or True
    )
    lab.knowledge = SimpleNamespace(close=lambda: calls.append("knowledge") or True)
    lab.cleanup["knowledge_closed"] = False
    for guard in lab.guards:
        guard.inspect = lambda guard=guard: (
            calls.append("inspect:" + guard.target.component)
            or {"idle": {"other_clients": guard.idle}}
        )
        guard.release = lambda guard=guard: (
            calls.append("release:" + guard.target.component) or True
        )
    reference = module._cleanup_reference(lab.manifest)
    return module, lab, calls, reference


def request_cleanup(lab, reference):
    path = lab.directory / "owner-cleanup-request.json"
    path.write_text(json.dumps(reference))
    path.chmod(0o600)
    return path


def test_real_close_drains_final_backend_read_before_actual_store_disposal(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import QueuePool

    from sidera_product.postgres.models import ProductDefaultsRow
    from sidera_product.postgres.store import PostgresProductStore

    module = real_module()
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "product.sqlite"),
        poolclass=QueuePool,
        execution_options={"schema_translate_map": {"product": None}},
    )
    store = PostgresProductStore.__new__(PostgresProductStore)
    store.engine = engine
    store._close_lock = Lock()
    store._closed = False
    calls = []
    targets = inert_real_targets(tmp_path)
    guards = []
    for target in targets:
        guard = SimpleNamespace(target=target, active=True)

        def inspect(guard=guard):
            calls.append("inspect:" + guard.target.component)
            return {
                "idle": {
                    "other_clients": engine.pool.checkedin() + engine.pool.checkedout()
                }
            }

        def release(guard=guard):
            assert guard.active is True
            assert store._closed is True
            assert engine.pool.checkedin() == engine.pool.checkedout() == 0
            calls.append("release:" + guard.target.component)
            guard.active = False
            return True

        guard.inspect, guard.release = inspect, release
        guards.append(guard)
    directory, evidence = tmp_path / "owned", tmp_path / "evidence"
    directory.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    lab = module.RealNativeProductFlowLab(
        targets=targets,
        guards=guards,
        directory=directory,
        evidence=evidence,
        entry=None,
        start_web=False,
        owns_guards=True,
    )

    def close_backend():
        calls.append("backend_last_inflight_read")
        assert store.get_defaults(uuid4()) is None
        return True

    def close_runtime(*, grace_seconds):
        assert grace_seconds == 120
        calls.append("runtime")
        store.close()
        return True

    lab.backend = SimpleNamespace(close=close_backend)
    lab.runtime = SimpleNamespace(close=close_runtime)
    try:
        ProductDefaultsRow.__table__.create(engine)
        assert engine.pool.checkedin() == 1 and engine.pool.checkedout() == 0
        assert lab.close() is True
        assert calls.index("backend_last_inflight_read") < calls.index("runtime")
        assert store._closed is True
        assert engine.pool.checkedin() == engine.pool.checkedout() == 0
        assert lab.cleanup["guards_inspected"] is True
        assert lab.cleanup["guards_released"] is True
        assert calls == [
            "backend_last_inflight_read",
            "runtime",
            "inspect:product",
            "inspect:agents",
            "inspect:cp",
            "inspect:credentials",
            "release:credentials",
            "release:cp",
            "release:agents",
            "release:product",
        ]
        assert all(guard.active is False for guard in guards)
    finally:
        store.close()
        engine.dispose()
    assert engine.pool.checkedin() == engine.pool.checkedout() == 0


@pytest.mark.parametrize("pending", ["vite", "backend"])
@pytest.mark.parametrize("failure", ["pending", "exception"])
def test_real_pending_transport_retains_owner_before_runtime_and_retries_only_pending(
    real_case, monkeypatch, pending, failure
):
    module, lab, calls, _ = cleanup_case(real_case)
    lab.vite = SimpleNamespace(poll=lambda: None)
    listener = SimpleNamespace(closed=False)

    def close_listener():
        calls.append("listener")
        listener.closed = True

    listener.close = close_listener
    listener.fileno = lambda: -1 if listener.closed else 7
    lab.listener = listener
    attempts = 0

    def close_transport(name):
        nonlocal attempts
        calls.append(name)
        if name == pending:
            attempts += 1
            if attempts == 1:
                if failure == "exception":
                    raise RuntimeError("uncertain transport close")
                return False
        return True

    monkeypatch.setattr(
        module, "_stop_owned_process", lambda value: close_transport("vite")
    )
    lab.backend.close = lambda: close_transport("backend")
    assert lab.close() is False
    stopped_at = ["vite"] if pending == "vite" else ["vite", "backend"]
    assert calls == stopped_at
    assert listener.closed is False
    assert lab.cleanup[pending + "_closed"] is False
    assert "runtime_closed" not in lab.cleanup
    assert "native_closed" not in lab.cleanup
    assert lab.cleanup["knowledge_closed"] is False
    assert lab.cleanup["guards_inspected"] is None
    assert lab.cleanup["guards_released"] is None
    assert lab._resource_closed == (set() if pending == "vite" else {"vite"})
    assert any(owner is lab for owner in module._retained_real_labs)
    proofs = {path: path.read_bytes() for path in (lab.directory / "cleanup").iterdir()}
    assert len(proofs) == 1
    incomplete = json.loads(next(iter(proofs.values())))
    assert incomplete["complete"] is False
    assert incomplete["cleanup"][pending + "_closed"] is False
    assert lab.close() is True
    assert calls == [
        *stopped_at,
        *(["vite"] if pending == "vite" else []),
        "backend",
        "listener",
        "runtime",
        "bootstrap",
        "knowledge",
        "inspect:product",
        "inspect:agents",
        "inspect:cp",
        "inspect:credentials",
        "release:credentials",
        "release:cp",
        "release:agents",
        "release:product",
    ]
    assert attempts == 2
    assert listener.closed is True
    assert lab.cleanup[pending + "_closed"] is True
    saved = list(calls)
    assert lab.close() is True
    assert calls == saved
    assert all(path.read_bytes() == raw for path, raw in proofs.items())


def test_real_partial_start_closes_untransferred_listener_before_runtime(real_case):
    _, lab, calls, _ = cleanup_case(real_case)
    lab.backend = None
    listener = SimpleNamespace(closed=False)

    def close_listener():
        calls.append("listener")
        listener.closed = True

    def close_runtime(*, grace_seconds):
        calls.append("runtime")
        assert grace_seconds == 120
        assert listener.closed is True
        return True

    listener.close = close_listener
    listener.fileno = lambda: -1 if listener.closed else 7
    lab.listener = listener
    lab.runtime.close = close_runtime
    assert lab.close() is True
    assert calls == [
        "listener",
        "runtime",
        "bootstrap",
        "knowledge",
        "inspect:product",
        "inspect:agents",
        "inspect:cp",
        "inspect:credentials",
        "release:credentials",
        "release:cp",
        "release:agents",
        "release:product",
    ]
    assert lab.cleanup["listener_closed"] is True
    assert lab.cleanup["runtime_closed"] is True


def test_knowledge_prerequisite_pending_close_retains_guards_and_retries_only_pending(
    real_case,
):
    module, lab, calls, _ = cleanup_case(real_case)
    results = iter((False, True))
    lab.knowledge.close = lambda: calls.append("knowledge") or next(results)
    assert lab.close() is False
    assert calls == ["backend", "runtime", "bootstrap", "knowledge"]
    assert lab.cleanup["knowledge_closed"] is False
    assert lab.cleanup["guards_inspected"] is None
    assert lab.cleanup["guards_released"] is None
    assert any(owner is lab for owner in module._retained_real_labs)
    proofs = {path: path.read_bytes() for path in (lab.directory / "cleanup").iterdir()}
    assert len(proofs) == 1
    incomplete = json.loads(next(iter(proofs.values())))
    assert incomplete["complete"] is False
    assert incomplete["cleanup"]["knowledge_closed"] is False
    assert lab.close() is True
    assert lab.cleanup["knowledge_closed"] is True
    assert calls == [
        "backend",
        "runtime",
        "bootstrap",
        "knowledge",
        "knowledge",
        "inspect:product",
        "inspect:agents",
        "inspect:cp",
        "inspect:credentials",
        "release:credentials",
        "release:cp",
        "release:agents",
        "release:product",
    ]
    saved = list(calls)
    assert lab.close() is True
    assert calls == saved
    assert all(path.read_bytes() == raw for path, raw in proofs.items())


@pytest.mark.parametrize("pending", ["runtime", "_sessions", "_orphaned"])
def test_knowledge_prerequisite_waits_for_runtime_and_native_custody(
    real_case, pending
):
    _, lab, calls, _ = cleanup_case(real_case)
    if pending == "runtime":
        lab.runtime.close = lambda **kwargs: calls.append("runtime") or False
    else:
        getattr(lab.native, pending)[uuid4()] = object()
    assert lab.close() is False
    assert calls == ["backend", "runtime"]
    assert lab.cleanup["backend_closed"] is True
    if pending == "runtime":
        assert "native_closed" not in lab.cleanup
    else:
        assert lab.cleanup["native_closed"] is False
    assert lab.cleanup["knowledge_closed"] is False
    assert lab.cleanup["guards_inspected"] is None
    assert lab.cleanup["guards_released"] is None
    if pending == "runtime":
        lab.runtime.close = lambda **kwargs: calls.append("runtime") or True
    else:
        getattr(lab.native, pending).clear()
    assert lab.close() is True
    assert calls.count("knowledge") == 1
    assert calls.index("bootstrap") < calls.index("knowledge")
    assert calls.index("knowledge") < calls.index("inspect:product")
    assert lab.cleanup["knowledge_closed"] is True


@pytest.mark.parametrize("failure", ["pending", "exception"])
def test_runtime_pending_retains_live_owner_and_all_later_resources(real_case, failure):
    module, lab, calls, reference = cleanup_case(real_case)

    def close_runtime(*, grace_seconds):
        assert grace_seconds == 120
        calls.append("runtime")
        if failure == "exception":
            raise RuntimeError("uncertain runtime close")
        return False

    lab.runtime.close = close_runtime
    request_cleanup(lab, reference)
    assert lab.service_cleanup_request() is False
    assert calls == ["backend", "runtime"]
    assert lab.cleanup["backend_closed"] is True
    assert lab.cleanup["runtime_closed"] is False
    proofs = {path: path.read_bytes() for path in (lab.directory / "cleanup").iterdir()}
    assert len(proofs) == 1
    assert json.loads(next(iter(proofs.values())))["complete"] is False
    assert any(owner is lab for owner in module._retained_real_labs)
    assert lab.service_cleanup_request() is False
    assert calls == ["backend", "runtime"]
    lab.runtime.close = lambda **kwargs: calls.append("runtime") or True
    assert lab.close() is True
    assert calls.count("backend") == 1
    assert calls.count("runtime") == 2
    assert all(path.read_bytes() == raw for path, raw in proofs.items())
    assert calls.index("backend") < calls.index("inspect:product")
    assert [name for name in calls if name.startswith("release:")] == [
        "release:credentials",
        "release:cp",
        "release:agents",
        "release:product",
    ]


@pytest.mark.parametrize("owned_set", ["_sessions", "_orphaned"])
def test_actual_adapter_custody_blocks_later_release(real_case, owned_set):
    module, lab, calls, _ = cleanup_case(real_case)
    getattr(lab.native, owned_set)[uuid4()] = object()
    assert lab.close() is False
    assert calls == ["backend", "runtime"]
    assert lab.cleanup["backend_closed"] is True
    assert lab.cleanup["native_closed"] is False
    assert lab.cleanup["knowledge_closed"] is False
    assert lab.cleanup["guards_inspected"] is None
    assert lab.cleanup["guards_released"] is None
    assert any(owner is lab for owner in module._retained_real_labs)
    assert getattr(lab.native, owned_set)
    getattr(lab.native, owned_set).clear()
    assert lab.close() is True
    assert calls.count("backend") == calls.count("runtime") == 1
    assert calls.index("bootstrap") < calls.index("knowledge")
    assert calls.index("knowledge") < calls.index("inspect:product")


@pytest.mark.parametrize(
    "case", ["foreign", "wrong_thread", "absent", "normal", "borrowed", "nonidle"]
)
def test_owner_protocol_is_scoped_and_idempotent(real_case, case):
    _, lab, calls, reference = cleanup_case(real_case, owns_guards=case != "borrowed")
    if case == "absent":
        assert lab.service_cleanup_request() is None
        assert calls == []
        return
    if case == "foreign":
        reference["workspace_id"] = str(uuid4())
    if case == "nonidle":
        lab.guards[0].idle = 1
    request_cleanup(lab, reference)
    if case == "wrong_thread":
        errors = []

        def foreign():
            try:
                lab.service_cleanup_request()
            except ProductTargetError as error:
                errors.append(str(error))

        thread = Thread(target=foreign)
        thread.start()
        thread.join(2)
        assert not thread.is_alive() and errors == [
            "real_native_cleanup_owner_thread_refused"
        ]
        assert calls == []
    elif case == "foreign":
        with pytest.raises(ProductTargetError):
            lab.service_cleanup_request()
        assert calls == []
    else:
        assert lab.service_cleanup_request() is (case != "nonidle")
        saved = list(calls)
        assert lab.service_cleanup_request() is (case != "nonidle")
        assert calls == saved
        if case == "borrowed":
            assert not any(name.startswith(("inspect:", "release:")) for name in calls)
        if case == "nonidle":
            assert not any(name.startswith("release:") for name in calls)


def test_bootstrap_inode_replacement_is_retained_by_real_owner(real_case):
    from tests.support.agent_management_desktop import OwnedBootstrapExport

    _, lab, calls, _ = cleanup_case(real_case)
    parent = lab.directory / "bootstrap"
    parent.mkdir(mode=0o700)
    path = parent / "session.json"
    path.write_text("original private export")
    path.chmod(0o600)
    directory_info, file_info = parent.stat(), path.stat()
    lab.bootstrap_export = OwnedBootstrapExport(
        path,
        directory_info.st_dev,
        directory_info.st_ino,
        file_info.st_dev,
        file_info.st_ino,
    )
    path.rename(parent / "original.json")
    path.write_text("replacement export")
    path.chmod(0o600)
    assert lab.close() is False
    assert path.read_text() == "replacement export"
    assert not any(name.startswith("release:") for name in calls)


@pytest.fixture
def verifier_case(tmp_path):
    module = real_module()
    root = tmp_path / "verifier-fixture"
    root.mkdir(mode=0o700)
    (root / "inputs").mkdir(mode=0o700)
    for name, value in module.INPUT_FIXTURES.items():
        (root / "inputs" / (name + ".json")).write_text(json.dumps(value))
    (root / "inputs" / "malformed.json").write_text("{")
    (root / "config_contract.json").write_text(
        json.dumps(
            {
                "exit_codes": {
                    "OK": 0,
                    "INVALID_CONFIGURATION": 2,
                    "INVALID_JSON": 3,
                    "INPUT_UNAVAILABLE": 4,
                }
            }
        )
    )
    (root / "integration_report.md").write_text(
        "Untrusted report text; the host executes its own checks.\n"
    )
    (root / "test_config_cli.py").write_text(
        "import unittest\nclass Smoke(unittest.TestCase):\n    def test_fixture(self):\n        self.assertTrue(True)\n"
    )
    (root / "config_cli.py").write_text("""import json
import sys

code = 0
if len(sys.argv) != 2:
    code = 4
else:
    try:
        with open(sys.argv[1]) as stream:
            value = json.load(stream)
    except OSError:
        code = 4
    except ValueError:
        code = 3
    else:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip() or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535 or type(value.get("enabled", True)) is not bool:
            code = 2
result = {"ok": False, "error": {2: "INVALID_CONFIGURATION", 3: "INVALID_JSON", 4: "INPUT_UNAVAILABLE"}[code]} if code else {"ok": True, "config": {"name": value["name"], "port": value["port"], "enabled": value.get("enabled", True)}}
print(json.dumps(result))
raise SystemExit(code)
""")
    namespace = {
        "__name__": "private_verifier_unit",
        "__file__": str(root / "verify_demo.py"),
    }
    exec(compile(module.VERIFY_DEMO, namespace["__file__"], "exec"), namespace)
    return root, namespace["main"]


@pytest.mark.parametrize("failing_test", [False, True], ids=["valid", "failing-tests"])
def test_independent_seed_verification_preserves_workspace_bytes(
    verifier_case, failing_test
):
    root, verify = verifier_case
    if failing_test:
        (root / "test_config_cli.py").write_text(
            "import unittest\nclass Smoke(unittest.TestCase):\n    def test_fixture(self):\n        self.assertTrue(False)\n"
        )

    def snapshot():
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    if failing_test:
        with pytest.raises(ValueError, match="independent_verification_failed"):
            verify()
    else:
        verify()
    assert snapshot() == before
    assert not any(
        path.name == "__pycache__" or path.suffix == ".pyc" for path in root.rglob("*")
    )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "changed_valid_input",
        "contract_bool",
        "no_tests",
        "missing_output",
        "stdout_noise",
        "argv_numeric_false",
    ],
)
def test_independent_seed_verifier_uses_literal_cases_and_real_unittest(
    verifier_case, bad
):
    root, verify = verifier_case
    if bad == "changed_valid_input":
        (root / "inputs" / "valid_default.json").write_text(
            json.dumps({"name": "altered", "port": 9})
        )
    elif bad == "contract_bool":
        contract = json.loads((root / "config_contract.json").read_text())
        contract["exit_codes"]["OK"] = False
        (root / "config_contract.json").write_text(json.dumps(contract))
    elif bad == "no_tests":
        (root / "test_config_cli.py").write_text("# No executable tests\n")
    elif bad == "missing_output":
        (root / "integration_report.md").unlink()
    elif bad == "stdout_noise":
        path = root / "config_cli.py"
        path.write_text('print("not JSON")\n' + path.read_text())
    elif bad == "argv_numeric_false":
        path = root / "config_cli.py"
        path.write_text(
            path.read_text().replace(
                "print(json.dumps(result))",
                'if len(sys.argv) != 2:\n    result["ok"] = 0\nprint(json.dumps(result))',
            )
        )
    if bad is None:
        verify()
    else:
        with pytest.raises(ValueError):
            verify()


@pytest.fixture
def real_web_entry(tmp_path, no_database_connections):
    module = real_module()
    root = tmp_path / "real-web"
    root.mkdir(mode=0o700)
    source = root / "source"
    source.mkdir(mode=0o700)
    for name in module.REAL_SOURCE_PATHS:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private pinned source; not executed\n")
    for name in ("repository", "checkpoints", "cleanup", "bootstrap"):
        (root / name).mkdir(mode=0o700)
    executable = root / "native-byte-fixture"
    executable.write_text("PRIVATE BYTES; NEVER EXECUTE\n")
    executable.chmod(0o700)
    workspace, namespace = str(uuid4()), str(uuid4())
    config = {
        "scenario": "main",
        "planning_budget": {
            "max_context_bytes": 1048576,
            "max_tokens": 200000,
            "max_cost_microunits": 1000000,
            "max_runtime_seconds": 3600,
            "max_revisions": 16,
            "max_nodes": 64,
            "max_concurrency": 4,
        },
        "settings": {
            "workspace_id": workspace,
            "principal_id": "private-human",
            "harness_id": "opencode",
            "executable": str(executable),
            "max_active": 4,
            "max_sessions": 8,
        },
        "real": {
            "schema_version": 1,
            "explicit_provider_run": True,
            "model_id": "opencode-go/deepseek-v4.1-flash",
            "provider_env": "OPENCODE_API_KEY",
            "credential_source_alias": "SIDERA_N23_OPENCODE_API_KEY",
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "actual_runtime_version": "1.18.32",
            "verification_python": sys.executable,
            "budget": {
                "total_seconds": 7200,
                "main_turns": 40,
                "planner_runs": 16,
                "worker_sessions": 64,
                "adapter_prompts": 200,
            },
        },
        "verification": {
            "seed_sha256": hashlib.sha256(module.VERIFY_DEMO.encode()).hexdigest(),
            "argv": [sys.executable, "-B", "verify_demo.py"],
            "timeout_seconds": 60,
        },
        "knowledge_scope": "not_configured",
    }
    manifest = {
        "schema_version": 1,
        "owner_uid": os.getuid(),
        "allocation_id": "sidera-native-harness:product:n17:20261008:webunit",
        "workspace_id": workspace,
        "namespace": namespace,
        "principal_id": "private-human",
        "repository_directory": str(root / "repository"),
        "repository_head": "a" * 40,
        "source_root": str(source),
        "source_pins": {
            name: {
                "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest(),
                "scope": "ui" if name.startswith("apps/web/") else "producer",
            }
            for name in module.REAL_SOURCE_PATHS
        },
        "config_path": str(root / "config.json"),
        "config_hash": "",
        "mode": "real",
        "controlled_bridge": False,
        "base_url": "http://127.0.0.1:43121",
        "backend_url": "http://127.0.0.1:43122",
        "controller_url": None,
        "transport_path": str(root / "transport.json"),
        "checkpoint_path": str(root / "checkpoints"),
        "cleanup_path": str(root / "cleanup"),
        "owner_receipt_path": str(root / "owner.json"),
        "guard_custody": "caller",
        "process_identities": {
            "owner_pid": os.getpid(),
            "backend_port": 43122,
            "web_port": 43121,
        },
        "target_facts": [],
    }
    transport = {
        "schema_version": 1,
        "workspace_id": workspace,
        "bootstrap_file": str(root / "bootstrap/session.json"),
    }

    def save():
        manifest["config_hash"] = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        owner = {
            key: manifest[key]
            for key in (
                "schema_version",
                "owner_uid",
                "allocation_id",
                "workspace_id",
                "namespace",
                "config_hash",
            )
        }
        owner.update(
            owner_pid=os.getpid(),
            operation_id=str(uuid5(UUID(namespace), "owned_cleanup")),
        )
        for name, value in (
            ("manifest.json", manifest),
            ("config.json", config),
            ("transport.json", transport),
            ("owner.json", owner),
            ("bootstrap/session.json", {"token": "private-bootstrap-sentinel"}),
        ):
            path = root / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    save()
    return SimpleNamespace(
        root=root,
        path=root / "manifest.json",
        manifest=manifest,
        config=config,
        transport=transport,
        save=save,
    )


def real_web_probe(entry, code, *, case=None, case_id=None, explicit=True):
    node = shutil.which("node")
    assert node is not None, "node24_required"
    source = Path(__file__).resolve().parents[4]
    program = r"""
const fs = require('node:fs'), path = require('node:path');
const web = process.cwd(), ts = require('typescript'), actual = require('@playwright/test');
let probeExpect = actual.expect;
const definitions = [], extensions = [], cache = new Map();
function testBoundary(fixtures = {}) {
  const test = (name, fn) => definitions.push({name, fn});
  test.extend = extra => { const next = {...fixtures, ...extra}; extensions.push(next); return testBoundary(next); };
  test.skip = () => {}; test.setTimeout = () => {};
  return test;
}
function load(name) {
  const file = path.resolve(web, name);
  if (!fs.existsSync(file)) return null;
  if (cache.has(file)) return cache.get(file).exports;
  const module = {exports: {}}; cache.set(file, module);
  const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
    reportDiagnostics: true,
  });
  if ((output.diagnostics || []).some(d => d.category === ts.DiagnosticCategory.Error)) throw Error('transpilation_failed');
  const requireFile = spec => spec === '@playwright/test'
    ? {...actual, test: testBoundary(), expect: probeExpect}
    : spec.endsWith('native-harness-fixtures.js') ? load('tests/e2e/native-harness-fixtures.ts') : require(spec);
  new Function('require', 'module', 'exports', '__filename', '__dirname', output.outputText)(requireFile, module, module.exports, file, path.dirname(file));
  return module.exports;
}
const fixture = load('tests/e2e/native-harness-fixtures.ts');
const manifestPath = process.argv[1];
(async () => {
"""
    result = subprocess.run(
        [
            node,
            "-e",
            program
            + code
            + "\n})().catch(e => {console.error(e.message); process.exitCode = 1;});",
            str(entry.path),
        ],
        cwd=source / "apps/web",
        env={
            "PATH": str(Path(node).parent) + ":/usr/bin:/bin",
            "HOME": str(entry.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            **({"SIDERA_NATIVE_MANIFEST": str(entry.path)} if explicit else {}),
            **({"SIDERA_NATIVE_REAL_CASE": case} if case else {}),
            **({"SIDERA_NATIVE_REAL_CASE_ID": case_id} if case_id else {}),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(result.stdout)


def real_config_reader_probe(entry):
    return real_web_probe(
        entry,
        """
const raw = JSON.parse(fs.readFileSync(manifestPath, 'utf8')), result = {};
try { result.config = fixture.loadRealConfig(raw); }
catch (error) { result.configError = error.message; }
try { result.manifest = fixture.loadOwnedManifest(manifestPath); }
catch (error) { result.manifestError = error.message; }
console.log(JSON.stringify(result));
""",
    )


@pytest.mark.parametrize("bound", [None, 1, 9007199254740991])
def test_real_planning_budget_web_readers_accept_complete_producer_shape(
    real_web_entry, bound
):
    entry = real_web_entry
    if bound is not None:
        entry.config["planning_budget"] = dict.fromkeys(
            entry.config["planning_budget"], bound
        )
        entry.save()
    config_bytes = Path(entry.manifest["config_path"]).read_bytes()
    manifest_bytes = entry.path.read_bytes()
    assert real_config_reader_probe(entry) == {
        "config": entry.config,
        "manifest": entry.manifest,
    }
    assert entry.config["planning_budget"]["max_tokens"] == (
        200000 if bound is None else bound
    )
    assert Path(entry.manifest["config_path"]).read_bytes() == config_bytes
    assert entry.path.read_bytes() == manifest_bytes


@pytest.mark.parametrize(
    "bad",
    [
        "missing_budget",
        "budget_null",
        "budget_bool",
        "budget_number",
        "budget_string",
        "budget_list",
        "missing_key",
        "extra_key",
    ],
)
def test_real_planning_budget_web_readers_refuse_incomplete_or_foreign_structure(
    real_web_entry, bad
):
    entry = real_web_entry
    if bad == "missing_budget":
        entry.config.pop("planning_budget")
    elif bad == "missing_key":
        entry.config["planning_budget"].pop("max_cost_microunits")
    elif bad == "extra_key":
        entry.config["planning_budget"]["max_subtasks"] = 8
    else:
        entry.config["planning_budget"] = {
            "budget_null": None,
            "budget_bool": True,
            "budget_number": 200000,
            "budget_string": "200000",
            "budget_list": [],
        }[bad]
    entry.save()
    assert real_config_reader_probe(entry) == {
        "configError": "native_real_config_refused",
        "manifestError": "native_manifest_refused",
    }


@pytest.mark.parametrize(
    "field",
    [
        "max_context_bytes",
        "max_tokens",
        "max_cost_microunits",
        "max_runtime_seconds",
        "max_revisions",
        "max_nodes",
        "max_concurrency",
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [
        True,
        1.5,
        "200000",
        None,
        {"api_key": "PRIVATE-SENTINEL-NOT-A-KEY"},
        [{"token": "PRIVATE-SENTINEL-NOT-A-KEY"}],
        0,
        -1,
        9007199254740992,
    ],
    ids=[
        "bool",
        "float",
        "string",
        "null",
        "dict",
        "list",
        "zero",
        "negative",
        "unsafe",
    ],
)
def test_real_planning_budget_web_readers_refuse_non_safe_positive_integer_values(
    real_web_entry, field, invalid
):
    entry = real_web_entry
    entry.config["planning_budget"][field] = invalid
    entry.save()
    assert real_config_reader_probe(entry) == {
        "configError": "native_real_config_refused",
        "manifestError": "native_manifest_refused",
    }


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("top", "max_tokens"),
        ("nested", "max_tokens"),
        ("nested_list", "max_tokens"),
        ("nested", "api_key"),
        ("budget", "api_key"),
        ("budget", "token"),
        ("budget", "cookie"),
        ("budget", "authorization"),
        ("budget", "bootstrap"),
        ("budget", "native_handle"),
        ("budget", "password"),
        ("budget", "secret"),
    ],
)
def test_real_planning_budget_web_readers_do_not_exempt_secret_or_foreign_token_fields(
    real_web_entry, location, field
):
    entry = real_web_entry
    item = 200000 if field == "max_tokens" else "PRIVATE-SENTINEL-NOT-A-KEY"
    if location == "top":
        entry.config[field] = item
    elif location == "nested":
        entry.config["settings"]["metadata"] = {field: item}
    elif location == "nested_list":
        entry.config["settings"]["metadata"] = [{field: item}]
    else:
        entry.config["planning_budget"][field] = item
    entry.save()
    assert real_config_reader_probe(entry) == {
        "configError": "native_real_config_refused",
        "manifestError": "native_manifest_refused",
    }


@pytest.mark.parametrize(
    "bad",
    ["symlink", "public_file", "public_directory", "relative", "workspace", "hash"],
)
def test_real_planning_budget_web_readers_refuse_unsafe_or_foreign_config(
    real_web_entry, bad
):
    entry = real_web_entry
    config_path = Path(entry.manifest["config_path"])
    if bad == "symlink":
        selected = entry.root / "config-link.json"
        selected.symlink_to(config_path)
        entry.manifest["config_path"] = str(selected)
    elif bad == "relative":
        entry.manifest["config_path"] = "config.json"
    elif bad == "workspace":
        entry.config["settings"]["workspace_id"] = str(uuid4())
    entry.save()
    if bad == "public_file":
        config_path.chmod(0o644)
    elif bad == "public_directory":
        entry.root.chmod(0o755)
    elif bad == "hash":
        entry.config["planning_budget"]["max_tokens"] = 200001
        config_path.write_text(json.dumps(entry.config))
    assert real_config_reader_probe(entry) == {
        "configError": "native_real_config_refused",
        "manifestError": "native_manifest_refused",
    }


def test_real_browser_entry_validates_and_selects_only_main(real_web_entry):
    result = real_web_probe(
        real_web_entry,
        """
const manifest = fixture.loadOwnedManifest(manifestPath);
console.log(JSON.stringify({mode: manifest.mode, realExport: typeof fixture.realTest,
  selected: fixture.realScenario ? fixture.realScenario(manifest) : null,
  fakeCount: fixture.NATIVE_SOURCE_PATHS.length, realCount: fixture.REAL_NATIVE_SOURCE_PATHS?.length}));
""",
    )
    assert result == {
        "mode": "real",
        "realExport": "function",
        "selected": "main",
        "fakeCount": 26,
        "realCount": 39,
    }


PROFILE_CAPTURE_SETUP = """
const manifest = fixture.loadOwnedManifest(manifestPath);
const root = `/agents/v1/workspaces/${manifest.workspace_id}`;
const refs = {
 primary: {workspace_id: manifest.workspace_id, profile_id: '11111111-1111-4111-8111-111111111111', revision: 1},
 target: {workspace_id: manifest.workspace_id, profile_id: '22222222-2222-4222-8222-222222222222', revision: 1},
 resource: {workspace_id: manifest.workspace_id, resource_id: '33333333-3333-4333-8333-333333333333', revision: 1},
};
const operations = {
 primaryCreate: '44444444-4444-4444-8444-444444444444', primaryBind: '55555555-5555-4555-8555-555555555555',
 targetCreate: '66666666-6666-4666-8666-666666666666', targetBind: '77777777-7777-4777-8777-777777777777',
 resource: '88888888-8888-4888-8888-888888888888',
};
const page = new Proxy({}, {get() {throw Error('Live page access forbidden');}});
function receipt(operationId, ref) {
 return {operationId, state: 'succeeded', target_ref: structuredClone(ref), result_ref: structuredClone(ref)};
}
function draft(resource_refs) {
 return {name: 'Owned capture profile', runtime_ref: {workspace_id: manifest.workspace_id, runtime_id: native.ids.runtime_id},
  model_id: 'opencode-go/deepseek-v4.1-flash', native_settings: {native_tools: 'write'},
  resource_refs, environment: {}, secret_refs: []};
}
async function create(prefix) {
 native.configurationCapture = prefix ? 'target' : 'primary';
 const operationId = operations[prefix ? 'targetCreate' : 'primaryCreate'];
 native.configurationIntent(root + '/profiles', {operationId, draft: draft([])});
 await native.capture(manifest.base_url + root + '/profiles', 200, receipt(operationId, refs[prefix ? 'target' : 'primary']));
}
function binding(prefix) {
 native.configurationCapture = prefix ? 'target' : 'primary';
 const created = refs[prefix ? 'target' : 'primary'], url = manifest.base_url + root + `/profiles/${created.profile_id}/revisions`;
 const operationId = operations[prefix ? 'targetBind' : 'primaryBind'];
 native.configurationIntent(new URL(url).pathname, {operationId, expected_ref: structuredClone(created), draft: draft([refs.resource])});
 return {url, body: receipt(operationId, {...created, revision: 2})};
}
async function bind(prefix) {
 const {url, body} = binding(prefix); await native.capture(url, 200, body);
}
async function resource() {
 native.ids.runtime_id = '99999999-9999-4999-8999-999999999999';
 native.ids.task_tool_connection_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
 native.configurationIntent(root + '/resources', {operationId: operations.resource,
  draft: {name: 'SIDERA Task Tools', kind: 'mcp', settings: {connection_id: native.ids.task_tool_connection_id}, secret_refs: []}});
 await native.capture(manifest.base_url + root + '/resources', 200, receipt(operations.resource, refs.resource));
}
"""


@pytest.mark.parametrize("mode", ["real", "fake"])
def test_profile_capture_distinguishes_creation_and_appointed_revision(
    real_web_entry, mode
):
    result = real_web_probe(
        real_web_entry,
        PROFILE_CAPTURE_SETUP
        + "manifest.mode = "
        + json.dumps(mode)
        + """;
const native = new fixture.NativeFixture(page, manifest);
await resource();
const states = [];
for (const prefix of ['', 'target_']) {
 await create(prefix);
 const early = structuredClone(native.ids);
 await bind(prefix);
 await create(prefix);
 await bind(prefix);
 const final = structuredClone(native.ids);
 states.push({creation: early[prefix + 'profile_create_ref'], profileId: early[prefix + 'profile_id'],
  earlyAppointed: early[prefix + 'profile_ref'] ?? null, appointed: final[prefix + 'profile_ref'],
  unchangedCreation: JSON.stringify(early[prefix + 'profile_create_ref']) === JSON.stringify(final[prefix + 'profile_create_ref'])});
}
const records = fs.readdirSync(manifest.checkpoint_path).map(name => ({name, body: JSON.parse(fs.readFileSync(path.join(manifest.checkpoint_path, name)))}));
console.log(JSON.stringify({states, count: records.length,
 privateRecords: records.every(r => (fs.statSync(path.join(manifest.checkpoint_path, r.name)).mode & 511) === 384)}));
""",
    )
    workspace = real_web_entry.manifest["workspace_id"]
    assert result == {
        "states": [
            {
                "creation": {
                    "workspace_id": workspace,
                    "profile_id": profile,
                    "revision": 1,
                },
                "profileId": profile,
                "earlyAppointed": None
                if mode == "real"
                else {"workspace_id": workspace, "profile_id": profile, "revision": 1},
                "appointed": {
                    "workspace_id": workspace,
                    "profile_id": profile,
                    "revision": 2,
                },
                "unchangedCreation": True,
            }
            for profile in (
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            )
        ],
        "count": 9,
        "privateRecords": True,
    }


@pytest.mark.parametrize("prefix", ["", "target_"])
def test_real_profile_bind_capture_accepts_unset_appointed_reference(
    real_web_entry, prefix
):
    result = real_web_probe(
        real_web_entry,
        PROFILE_CAPTURE_SETUP
        + "const prefix = "
        + json.dumps(prefix)
        + """;
const native = new fixture.NativeFixture(page, manifest);
await resource(); await create('');
if (prefix) {await bind(''); await create(prefix);}
delete native.ids[prefix + 'profile_ref'];
const {url, body} = binding(prefix);
let error = null;
try {await native.capture(url, 200, body);} catch (caught) {error = caught.message;}
console.log(JSON.stringify({error, appointed: native.ids[prefix + 'profile_ref'] ?? null}));
""",
    )
    assert result == {
        "error": None,
        "appointed": {
            "workspace_id": real_web_entry.manifest["workspace_id"],
            "profile_id": "22222222-2222-4222-8222-222222222222"
            if prefix
            else "11111111-1111-4111-8111-111111111111",
            "revision": 2,
        },
    }


@pytest.mark.parametrize("prefix", ["", "target_"])
@pytest.mark.parametrize("phase", ["create", "bind"])
@pytest.mark.parametrize(
    "bad",
    [
        "workspace",
        "profile",
        "revision",
        "boolean_revision",
        "operation",
        "invalid_operation",
        "target",
        "extra_field",
    ],
)
def test_real_profile_capture_refuses_invalid_receipt_identity(
    real_web_entry, prefix, phase, bad
):
    result = real_web_probe(
        real_web_entry,
        PROFILE_CAPTURE_SETUP
        + "const [prefix, phase, bad] = "
        + json.dumps([prefix, phase, bad])
        + """;
const native = new fixture.NativeFixture(page, manifest);
await resource(); await create('');
if (prefix) {await bind(''); await create(prefix);}
let url, body;
if (phase === 'bind') ({url, body} = binding(prefix));
else {url = manifest.base_url + root + '/profiles'; body = receipt(operations[prefix ? 'targetCreate' : 'primaryCreate'], refs[prefix ? 'target' : 'primary']);}
if (bad === 'operation') body.operationId = operations.resource;
else if (bad === 'invalid_operation') {body.operationId = 'invalid-operation'; native.ids[prefix + (phase === 'create' ? 'profile_create_operation_id' : 'configuration_operation_id')] = body.operationId;}
else if (bad === 'target') body.target_ref.revision = 9;
else {
 if (bad === 'workspace') body.result_ref.workspace_id = operations.resource;
 if (bad === 'profile') body.result_ref.profile_id = refs.resource.resource_id;
 if (bad === 'revision') body.result_ref.revision = 3;
 if (bad === 'boolean_revision') body.result_ref.revision = true;
 if (bad === 'extra_field') body.result_ref.extra = 'fabricated';
 body.target_ref = structuredClone(body.result_ref);
}
const before = JSON.stringify(native.ids), count = fs.readdirSync(manifest.checkpoint_path).length;
let refused = false;
try {await native.capture(url, 200, body);} catch {refused = true;}
console.log(JSON.stringify({refused, unchanged: JSON.stringify(native.ids) === before, countUnchanged: fs.readdirSync(manifest.checkpoint_path).length === count}));
""",
    )
    assert result == {"refused": True, "unchanged": True, "countUnchanged": True}


@pytest.mark.parametrize("prefix", ["", "target_"])
@pytest.mark.parametrize(
    "bad",
    [
        "created_workspace",
        "created_extra",
        "expected",
        "resource",
        "resource_revision",
        "resource_extra",
        "missing_create_operation",
        "reused_operation",
        "appointed_r1",
        "appointed_other",
    ],
)
def test_real_profile_bind_capture_requires_exact_original_creation_and_resource(
    real_web_entry, prefix, bad
):
    result = real_web_probe(
        real_web_entry,
        PROFILE_CAPTURE_SETUP
        + "const [prefix, bad] = "
        + json.dumps([prefix, bad])
        + """;
const native = new fixture.NativeFixture(page, manifest);
await resource(); await create('');
if (prefix) {await bind(''); await create(prefix);}
const {url, body} = binding(prefix);
if (bad === 'created_workspace') {native.ids[prefix + 'profile_create_ref'].workspace_id = operations.resource; native.ids[prefix + 'configuration_expected_ref'].workspace_id = operations.resource;}
if (bad === 'created_extra') {native.ids[prefix + 'profile_create_ref'].extra = 'fabricated'; native.ids[prefix + 'configuration_expected_ref'].extra = 'fabricated';}
if (bad === 'expected') native.ids[prefix + 'configuration_expected_ref'].revision = 2;
if (bad === 'resource') native.ids[prefix + 'configuration_resource_ref'].resource_id = operations.resource;
if (bad === 'resource_revision') {native.ids.task_tool_resource_ref.revision = 2; native.ids[prefix + 'configuration_resource_ref'].revision = 2;}
if (bad === 'resource_extra') {native.ids.task_tool_resource_ref.extra = 'fabricated'; native.ids[prefix + 'configuration_resource_ref'].extra = 'fabricated';}
if (bad === 'missing_create_operation') delete native.ids[prefix + 'profile_create_operation_id'];
if (bad === 'reused_operation') native.ids[prefix + 'profile_create_operation_id'] = body.operationId;
if (bad === 'appointed_r1') native.ids[prefix + 'profile_ref'] = structuredClone(refs[prefix ? 'target' : 'primary']);
if (bad === 'appointed_other') native.ids[prefix + 'profile_ref'] = {...refs[prefix ? 'target' : 'primary'], revision: 3};
const before = JSON.stringify(native.ids), count = fs.readdirSync(manifest.checkpoint_path).length;
let refused = false;
try {await native.capture(url, 200, body);} catch {refused = true;}
console.log(JSON.stringify({refused, unchanged: JSON.stringify(native.ids) === before, countUnchanged: fs.readdirSync(manifest.checkpoint_path).length === count}));
""",
    )
    assert result == {"refused": True, "unchanged": True, "countUnchanged": True}


def test_real_browser_repository_reference_uses_actual_python_producer(real_web_entry):
    result = real_web_probe(
        real_web_entry,
        """
const spec = load('tests/e2e/native-harness-real.spec.ts');
const directory = '/private/tmp/sidera-n23-repository-reference-fixture';
const manifest = {...JSON.parse(fs.readFileSync(manifestPath, 'utf8')),
  source_root: path.resolve(web, '../..'), repository_directory: directory};
const native = new fixture.NativeFixture({}, manifest, true);
const before = JSON.stringify(manifest);
const reference = await spec.realRepositoryReference(native);
console.log(JSON.stringify({reference, directory: native.manifest.repository_directory,
  distinct: reference !== directory, unchanged: JSON.stringify(manifest) === before}));
""",
        explicit=False,
    )
    assert result == {
        "reference": "git-repository://"
        "383e8d6ddccf1a38650a2bcf79e8009b037c04083dbbb40c6dafe657815ca209",
        "directory": "/private/tmp/sidera-n23-repository-reference-fixture",
        "distinct": True,
        "unchanged": True,
    }


def test_real_browser_repository_reference_refuses_fake_before_python(real_web_entry):
    result = real_web_probe(
        real_web_entry,
        """
const spec = load('tests/e2e/native-harness-real.spec.ts');
const manifest = {...JSON.parse(fs.readFileSync(manifestPath, 'utf8')), mode: 'fake'};
let calls = 0, error = null;
const native = {manifest, python() { calls++; throw Error('Unexpected Python bridge'); }};
try { await spec.realRepositoryReference(native); } catch (caught) { error = caught.message; }
console.log(JSON.stringify({calls, error}));
""",
        explicit=False,
    )
    assert result == {"calls": 0, "error": "native_real_repository_reference_required"}


@pytest.mark.parametrize("value", [None, 1, True, {}, []])
def test_real_browser_repository_reference_refuses_non_string_bridge_result(
    real_web_entry, value
):
    result = real_web_probe(
        real_web_entry,
        """
const spec = load('tests/e2e/native-harness-real.spec.ts');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
let calls = 0, error = null;
const native = {manifest, python() { calls++; return """
        + json.dumps(value)
        + """; }};
try { await spec.realRepositoryReference(native); } catch (caught) { error = caught.message; }
console.log(JSON.stringify({calls, error}));
""",
        explicit=False,
    )
    assert result == {"calls": 1, "error": "native_real_repository_reference_required"}


@pytest.mark.parametrize(
    "bad",
    [
        "missing_pin",
        "extra_pin",
        "producer_bytes",
        "ui_bytes",
        "scope",
        "owner",
        "controller",
        "transport_secret",
        "config_model",
        "config_budget",
        "config_limits",
        "executable_hash",
        "verification_argv",
    ],
)
def test_real_browser_entry_refuses_drift_before_resources(real_web_entry, bad):
    entry = real_web_entry
    if bad == "missing_pin":
        entry.manifest["source_pins"].pop(
            "apps/server/tests/support/real_native_product_flow.py"
        )
    elif bad == "extra_pin":
        entry.manifest["source_pins"]["unexpected.py"] = {
            "sha256": "a" * 64,
            "scope": "producer",
        }
    elif bad in {"producer_bytes", "ui_bytes"}:
        name = (
            "scripts/native-harness-check.py"
            if bad == "producer_bytes"
            else "apps/web/tests/e2e/native-harness-fixtures.ts"
        )
        (Path(entry.manifest["source_root"]) / name).write_text("changed bytes")
    elif bad == "scope":
        entry.manifest["source_pins"]["scripts/native-harness-check.py"]["scope"] = "ui"
    elif bad == "owner":
        entry.manifest["owner_uid"] += 1
    elif bad == "controller":
        entry.manifest["controller_url"] = "http://127.0.0.1:43200"
    elif bad == "transport_secret":
        entry.transport["provider_api_key"] = "PRIVATE-SENTINEL-NOT-A-KEY"
    elif bad == "config_model":
        entry.config["real"]["model_id"] = "unapproved/model"
    elif bad == "config_budget":
        entry.config["real"]["budget"]["total_seconds"] = 1
    elif bad == "config_limits":
        entry.config["settings"]["max_sessions"] = 4
    elif bad == "executable_hash":
        entry.config["real"]["executable_sha256"] = "0" * 64
    elif bad == "verification_argv":
        entry.config["verification"]["argv"] = [sys.executable, "changed.py"]
    entry.save()
    assert real_web_probe(
        entry,
        """
let refused = false; try {fixture.loadOwnedManifest(manifestPath, """
        + json.dumps(bad != "ui_bytes")
        + """);} catch {refused = true;}
console.log(JSON.stringify({refused}));
""",
    ) == {"refused": True}


def test_real_config_is_explicit_private_and_has_no_server(real_web_entry):
    result = real_web_probe(
        real_web_entry,
        """
const config = load('playwright.native-real.config.ts')?.default;
console.log(JSON.stringify(config ? {match: config.testMatch, workers: config.workers, retries: config.retries,
 timeout: config.timeout, project: config.projects[0].name, viewport: config.projects[0].use.viewport,
 trace: config.use.trace, video: config.use.video, screenshot: config.use.screenshot,
 server: config.webServer ?? null, output: config.outputDir.startsWith(path.dirname(manifestPath) + '/'),
 reporter: config.reporter.map(r => r[0])} : null));
""",
    )
    assert result == {
        "match": ["native-harness-real.spec.ts"],
        "workers": 1,
        "retries": 0,
        "timeout": 7200000,
        "project": "native-real-chromium",
        "viewport": {"width": 1440, "height": 960},
        "trace": "off",
        "video": "off",
        "screenshot": "off",
        "server": None,
        "output": True,
        "reporter": ["json"],
    }


@pytest.mark.parametrize("case", ["input", "handoff"])
def test_real_focused_case_requires_explicit_immutable_identity(real_web_entry, case):
    case_id = str(uuid4())
    result = real_web_probe(
        real_web_entry,
        """
const manifest = fixture.loadOwnedManifest(manifestPath);
console.log(JSON.stringify({selected: fixture.realScenario(manifest), identity: fixture.realCaseId(manifest)}));
""",
        case=case,
        case_id=case_id,
    )
    assert result == {"selected": case, "identity": case_id}
    assert real_web_probe(
        real_web_entry,
        """
let refused = false; try {fixture.realScenario(fixture.loadOwnedManifest(manifestPath));} catch {refused = true;}
console.log(JSON.stringify({refused}));
""",
        case=case,
    ) == {"refused": True}


def test_real_spec_is_not_implicitly_collected(real_web_entry):
    for case in (None, "input", "handoff"):
        result = real_web_probe(
            real_web_entry,
            """
const spec = load('tests/e2e/native-harness-real.spec.ts');
console.log(JSON.stringify({present: spec !== null, definitions: definitions.length}));
""",
            case=case,
            case_id=str(uuid4()),
            explicit=False,
        )
        assert result == {"present": True, "definitions": 0}


@pytest.mark.parametrize(
    "helper",
    [
        "nativeScenario",
        "facts",
        "bridge",
        "readSourceContext",
        "waitForSourceContext",
        "repositoryProof",
        "assertNativeNavigation",
        "fake_fixture",
    ],
)
def test_fake_browser_helpers_refuse_real_without_side_effects(real_web_entry, helper):
    result = real_web_probe(
        real_web_entry,
        """
const manifest = JSON.parse(fs.readFileSync(manifestPath));
let effects = 0;
const page = {getByRole: () => {effects++; throw Error('page_must_not_be_used');}};
const native = new fixture.NativeFixture(page, manifest);
native.ids.root_task_id = native.ids.source_worker_item_id = '12345678-1234-4234-8234-123456789abc';
native.python = () => {effects++; return {};}; native.get = async () => {effects++; return {};};
let refused = false;
try {
 const helper = """
        + json.dumps(helper)
        + """;
 if (helper === 'nativeScenario') fixture.nativeScenario(manifest);
 else if (helper === 'assertNativeNavigation') await fixture.assertNativeNavigation(native);
 else if (helper === 'fake_fixture') await extensions[0].native({page: {}}, async () => {effects++;}, {file: 'native-harness.spec.ts'});
 else await native[helper]();
} catch {refused = true;}
console.log(JSON.stringify({refused, effects}));
""",
    )
    assert result == {"refused": True, "effects": 0}


@pytest.mark.parametrize("bad", [None, "stalled", "foreign", "backwards"])
def test_real_trace_paginates_original_execution_and_refuses_cursor_drift(
    real_web_entry, bad
):
    result = real_web_probe(
        real_web_entry,
        """
const manifest = JSON.parse(fs.readFileSync(manifestPath)), native = new fixture.NativeFixture({}, manifest);
const id = '12345678-1234-4234-8234-123456789abc';
const pin = {workspace_id: manifest.workspace_id, worker_item_id: id, execution_id: id, run_id: id,
 profile_ref: {workspace_id: manifest.workspace_id, profile_id: id, revision: 2}};
const queries = [], bad = """
        + json.dumps(bad)
        + """;
native.get = async url => {queries.push(url); const after = Number(new URL('http://local' + url).searchParams.get('after_sequence'));
 const sequence = bad === 'backwards' && after ? 1 : after + 1;
 return {...pin, run_id: bad === 'foreign' ? manifest.workspace_id : id,
 events: bad === 'stalled' ? [] : [{run_id: id, sequence, kind: 'response.started'}],
 next_sequence: bad === 'stalled' ? after : sequence, has_more: after === 0};};
let trace = null, refused = false;
try {trace = await fixture.readRealNativeTrace(native, pin);} catch {refused = true;}
console.log(JSON.stringify({refused, count: trace?.events.length ?? null, cursor: trace?.next_sequence ?? null,
 after: queries.map(url => new URL('http://local' + url).searchParams.get('after_sequence'))}));
""",
    )
    if bad is None:
        assert result == {
            "refused": False,
            "count": 2,
            "cursor": 2,
            "after": ["0", "1"],
        }
    else:
        assert result["refused"] is True


@pytest.mark.parametrize("case", ["main", "input", "handoff"])
def test_explicit_real_collection_registers_one_selected_case(real_web_entry, case):
    result = real_web_probe(
        real_web_entry,
        """
load('playwright.native-real.config.ts');
load('tests/e2e/native-harness-real.spec.ts');
console.log(JSON.stringify({definitions: definitions.map(d => d.name)}));
""",
        case=case,
        case_id=str(uuid4()),
    )
    assert result == {"definitions": [f"real native {case}"]}


@pytest.mark.parametrize("bad", [None, "secret", "pin_drift", "replacement"])
def test_real_focused_receipts_are_private_immutable_and_separate(real_web_entry, bad):
    case_id = str(uuid4())
    result = real_web_probe(
        real_web_entry,
        """
const manifest = fixture.loadOwnedManifest(manifestPath), native = new fixture.NativeFixture({}, manifest);
const bad = """
        + json.dumps(bad)
        + """;
native.ids.turn_operation_id = '12345678-1234-4234-8234-123456789abc';
let refused = false, first = null;
try {
 first = native.recordFocusedReal('pending', bad === 'secret' ? {provider_api_key: 'PRIVATE-SENTINEL'} : {intent: true});
 if (bad === 'pin_drift') manifest.source_pins['scripts/native-harness-check.py'].sha256 = 'f'.repeat(64);
 if (bad === 'replacement') native.ids.turn_operation_id = manifest.workspace_id;
 native.recordFocusedReal('incomplete', {admission: 'UNKNOWN'});
 fixture.realFocusedRecords(manifest, fixture.realScenario(manifest), fixture.realCaseId(manifest));
} catch {refused = true;}
const receipts = fs.readdirSync(path.dirname(manifestPath)).filter(n => n.startsWith('focused-real-'));
console.log(JSON.stringify({refused, mainRecords: fs.readdirSync(manifest.checkpoint_path).length,
 count: receipts.length, modes: receipts.map(n => fs.statSync(path.join(path.dirname(manifestPath), n)).mode & 511),
 firstStatus: first ? JSON.parse(fs.readFileSync(first)).status : null}));
""",
        case="input",
        case_id=case_id,
    )
    if bad is None:
        assert result == {
            "refused": False,
            "mainRecords": 0,
            "count": 2,
            "modes": [384, 384],
            "firstStatus": "pending",
        }
    else:
        assert result["refused"] is True
        assert result["mainRecords"] == 0


def test_focused_original_admission_is_queried_and_never_resent(real_web_entry):
    result = real_web_probe(
        real_web_entry,
        """
const manifest = fixture.loadOwnedManifest(manifestPath), native = new fixture.NativeFixture({}, manifest);
const original = '12345678-1234-4234-8234-123456789abc';
native.ids.input_command_id = original; native.recordFocusedReal('incomplete', {admission: 'UNKNOWN'});
const queries = []; native.get = async url => {queries.push(url); return {command_id: original, status: 'pending'};};
let refused = false;
try {await native.requireFreshRealMutation('input_command_id', manifest.workspace_id);} catch {refused = true;}
console.log(JSON.stringify({refused, queries, original: native.ids.input_command_id}));
""",
        case="input",
        case_id=str(uuid4()),
    )
    assert result == {
        "refused": True,
        "queries": [
            f"/api/v1/workspaces/{real_web_entry.manifest['workspace_id']}/commands/12345678-1234-4234-8234-123456789abc"
        ],
        "original": "12345678-1234-4234-8234-123456789abc",
    }


def test_focused_mutation_queries_all_recorded_original_operations(real_web_entry):
    result = real_web_probe(
        real_web_entry,
        """
const manifest = fixture.loadOwnedManifest(manifestPath), native = new fixture.NativeFixture({}, manifest);
const original = '12345678-1234-4234-8234-123456789abc';
for (const key of ['goal_command_id', 'profile_create_operation_id', 'thread_id', 'turn_operation_id',
 'source_execution_id', 'source_session_id', 'input_operation_id', 'handoff_transition_id']) native.ids[key] = original;
native.recordFocusedReal('pending', {original_operations: true});
const queries = []; native.get = async url => {queries.push(url); return {};};
await native.requireFreshRealMutation('handoff_prepare_command_id', manifest.workspace_id);
console.log(JSON.stringify({queries}));
""",
        case="handoff",
        case_id=str(uuid4()),
    )
    workspace = real_web_entry.manifest["workspace_id"]
    original = "12345678-1234-4234-8234-123456789abc"
    assert set(result["queries"]) == {
        f"/api/v1/workspaces/{workspace}/commands/{original}",
        f"/product/v1/workspaces/{workspace}/configuration-requests?operationId={original}",
        f"/main-agent/v1/threads/{original}",
        f"/api/v1/gateway/inputs?workspace_id={workspace}&execution_id={original}&session_id={original}",
        f"/api/v1/workspaces/{workspace}/handoff-transitions/{original}",
    }


@pytest.mark.parametrize(
    "bad", [None, "schema", "worker_key", "dependency", "code_inputs"]
)
def test_real_proposal_requires_actual_four_keys_and_selected_inputs(
    real_web_entry, bad
):
    result = real_web_probe(
        real_web_entry,
        """
const spec = load('tests/e2e/native-harness-real.spec.ts');
const proposal = {proposal_schema_version: 6, display: {
 subtasks: [{key: 'actual-subtask', title: 'Actual subtask'}],
 worker_items: ['contract', 'cli', 'tests', 'integration'].map(key => ({key, title: key + ' actual', subtask_key: 'actual-subtask'})),
 worker_item_dependencies: [['contract', 'cli'], ['contract', 'tests'], ['cli', 'integration'], ['tests', 'integration']].map(([upstream_worker_item_key, downstream_worker_item_key]) => ({upstream_worker_item_key, downstream_worker_item_key, kind: 'hard'})),
 integration_spec: {integration_worker_item_key: 'integration'}},
 collaboration: {input_groups: [['cli', ['contract']], ['tests', ['contract']], ['integration', ['cli', 'tests']]].map(([consumer_worker_item_key, keys]) => ({consumer_worker_item_key, candidates: keys.map(worker_item_key => ({worker_item_key})), mode: 'all', requirements: {execution_status: 'succeeded'}}))}};
const bad = """
        + json.dumps(bad)
        + """;
if (bad === 'schema') proposal.proposal_schema_version = 2;
if (bad === 'worker_key') proposal.display.worker_items[1].key = 'replacement';
if (bad === 'dependency') proposal.display.worker_item_dependencies.pop();
if (bad === 'code_inputs') proposal.collaboration.input_groups[2].candidates.pop();
let refused = false, titles = null;
try {titles = spec.realProposalShape(proposal, 'main').workers.map(w => w.title);} catch {refused = true;}
console.log(JSON.stringify({refused, titles}));
""",
        explicit=False,
    )
    if bad is None:
        assert result == {
            "refused": False,
            "titles": [
                "contract actual",
                "cli actual",
                "tests actual",
                "integration actual",
            ],
        }
    else:
        assert result["refused"] is True


@pytest.mark.parametrize(
    ("selected", "invalid_revisions", "accepted_revision", "feedback_count"),
    [
        pytest.param("main", 0, 1, 0, id="valid-main"),
        pytest.param("main", 1, 2, 1, id="repaired-main"),
        pytest.param("input", 1, 2, 1, id="repaired-input"),
        pytest.param("handoff", 1, 2, 1, id="repaired-handoff"),
        pytest.param("main", 3, None, 2, id="exhausted-main"),
    ],
)
def test_real_proposal_ready_opens_feedback_and_preserves_original_identity(
    real_web_entry, selected, invalid_revisions, accepted_revision, feedback_count
):
    request_id = "12345678-1234-4234-8234-123456789abc"
    task_id = "22345678-1234-4234-8234-123456789abc"
    original_ids = {
        "planning_request_id": request_id,
        "root_task_id": task_id,
        "goal_command_id": "32345678-1234-4234-8234-123456789abc",
    }
    proposal_ids = [
        "42345678-1234-4234-8234-123456789abc",
        "52345678-1234-4234-8234-123456789abc",
        "62345678-1234-4234-8234-123456789abc",
    ]
    result = real_web_probe(
        real_web_entry,
        "const [selected, invalidRevisions, originalIds, proposalIds] = "
        + json.dumps([selected, invalid_revisions, original_ids, proposal_ids])
        + """;
probeExpect = value => actual.expect(value);
probeExpect.poll = callback => ({
  toBe: async expected => actual.expect(await callback()).toBe(expected),
  toBeGreaterThan: async expected => actual.expect(await callback()).toBeGreaterThan(expected),
});
// Rebind the driver's fixture import to this program's deterministic poll port.
cache.delete(path.resolve(web, 'tests/e2e/native-harness-fixtures.ts'));
const spec = load('tests/e2e/native-harness-real.spec.ts');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const requestId = originalIds.planning_request_id, taskId = originalIds.root_task_id;
const requestPath = `/api/v1/workspaces/${manifest.workspace_id}/planning-requests/${requestId}`;
const pagePath = `/workspaces/${manifest.workspace_id}/planning/${requestId}`;
const keys = selected === 'main' ? ['contract', 'cli', 'tests', 'integration'] : [`${selected}-work`];
const proposals = proposalIds.slice(0, invalidRevisions === 3 ? 3 : invalidRevisions + 1).map((id, index) => {
  const proposal = {
    id, planning_request_id: requestId, planning_run_id: '72345678-1234-4234-8234-123456789abc',
    revision: index + 1, content_hash: String(index + 1).repeat(64), proposal_schema_version: 6,
    brief_revision: 1, profile_version: 1, kind: 'create', created_at: '2026-10-09T00:00:00Z',
    derived_diff: {}, display: {
      subtasks: [{key: 'original-subtask', title: 'Original substantive workload'}],
      worker_items: keys.map(key => ({key, title: key + ' implementation', subtask_key: 'original-subtask'})),
      worker_item_dependencies: selected === 'main'
        ? [['contract', 'cli'], ['contract', 'tests'], ['cli', 'integration'], ['tests', 'integration']].map(
          ([upstream_worker_item_key, downstream_worker_item_key]) => ({upstream_worker_item_key, downstream_worker_item_key, kind: 'hard'}))
        : [],
      integration_spec: {integration_worker_item_key: selected === 'main' ? 'integration' : keys[0]},
    },
    collaboration: {
      mode: 'input_groups', provider: null, default_strategy: null,
      runtime_config_version: null, runtime_interface: null, subtask_overrides: [],
      input_groups: selected === 'main'
        ? [['cli', ['contract']], ['tests', ['contract']], ['integration', ['cli', 'tests']]].map(
          ([consumer_worker_item_key, sources]) => ({
            consumer_worker_item_key, group_key: `${consumer_worker_item_key}-code`, title: 'Required immutable code results',
            candidates: sources.map((worker_item_key, priority) => ({worker_item_key, priority})),
            mode: 'all', minimum_count: null, selection_strategy: 'priority',
            requirements: {execution_status: 'succeeded', evidence_status: 'not_required', required_artifact_refs: [], required_evidence_refs: []},
          }))
        : [],
    },
  };
  if (index < invalidRevisions) {
    if (selected === 'main') proposal.display.worker_item_dependencies.pop();
    else proposal.display.worker_items[0].key = 'wrong-work';
  }
  return proposal;
});
const planning = proposals.map(proposal => ({
  id: requestId, workspace_id: manifest.workspace_id, kind: 'create', status: 'proposal_ready',
  source_type: 'root_task', source_ref: taskId, current_brief_revision: 1,
  current_proposal_revision: proposal.revision, current_proposal_content_hash: proposal.content_hash,
  current_run_id: proposal.planning_run_id, active_run: null, pending_clarifications: [],
  planning_profile_id: '82345678-1234-4234-8234-123456789abc', planning_profile_version: 1,
  profile: null, profile_key: null, requires_attention: false, revision_base: null, revision_freshness: null,
  usage: {context_bytes: null, input_tokens: null, output_tokens: null, cost_microunits: null, runtime_seconds: null},
  version: 11 + proposal.revision, last_change_cursor: proposal.revision,
  created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z',
}));
const shapeErrors = proposals.map(proposal => {
  try { spec.realProposalShape(proposal, selected); return null; }
  catch (error) { return error.message; }
});
let current = 0, open = false, feedback = '';
const actions = [], feedbacks = [], records = [], queries = [], navigations = [];
const native = {
  ids: originalIds, versions: {planning_request: 11, untouched: 7},
  prefix: () => `/api/v1/workspaces/${manifest.workspace_id}/`,
  path: suffix => `/workspaces/${manifest.workspace_id}/${suffix}`,
  id: key => { actual.expect(typeof native.ids[key]).toBe('string'); return native.ids[key]; },
  get: async url => {
    queries.push(url);
    if (url === requestPath) return structuredClone(planning[current]);
    if (url === `${requestPath}/proposals/${proposals[current].revision}`) return structuredClone(proposals[current]);
    throw Error('unexpected_original_proposal_query');
  },
  record: (status, observations) => records.push({status, observations: structuredClone(observations)}),
  page: {
    goto: async url => { actual.expect(url).toBe(pagePath); navigations.push(url); open = false; feedback = ''; },
    getByRole: (role, options) => {
      actual.expect(options.exact).toBe(true);
      if (role === 'button' && options.name === '要求修改') return {click: async () => {
        actual.expect(open).toBe(false); actions.push('open'); open = true;
      }};
      if (role === 'textbox' && options.name === '修改意见') return {fill: async text => {
        actions.push('fill');
        if (!open) throw Error('proposal_feedback_composer_closed');
        feedback = text;
        feedbacks.push({text, request_id: planning[current].id, task_id: planning[current].source_ref, revision: proposals[current].revision});
      }};
      if (role === 'button' && options.name === '提交修改意见') return {click: async () => {
        actual.expect(open).toBe(true); actual.expect(feedback.trim().length).toBeGreaterThan(0);
        actions.push('submit');
        if (current + 1 >= proposals.length) throw Error('unexpected_third_feedback_or_new_proposal');
        current++;
      }};
      throw Error('unexpected_proposal_feedback_control');
    },
  },
};
let outcome = null, error = null;
try { outcome = await spec.proposalReady(native, selected); }
catch (caught) { error = caught.message; }
console.log(JSON.stringify({outcome, error, actions, feedbacks, records, queries, navigations, shapeErrors,
  ids: native.ids, sameIdsObject: native.ids === originalIds, versions: native.versions,
  currentRevision: proposals[current].revision}));
""",
        explicit=False,
    )
    expected_error = (
        "native_real_proposal_repair_exhausted" if accepted_revision is None else None
    )
    assert result["error"] == expected_error
    assert result["actions"] == ["open", "fill", "submit"] * feedback_count
    assert result["sameIdsObject"] is True
    proposal_count = accepted_revision or 3
    pins = [
        {
            "id": proposal_ids[index],
            "revision": index + 1,
            "content_hash": str(index + 1) * 64,
        }
        for index in range(proposal_count)
    ]
    assert result["records"] == [
        {"status": "pending", "observations": {"original_proposals": pins[:count]}}
        for count in range(1, proposal_count + 1)
    ]
    page_path = (
        f"/workspaces/{real_web_entry.manifest['workspace_id']}/planning/{request_id}"
    )
    assert result["navigations"] == [page_path] * proposal_count
    request_path = (
        f"/api/v1/workspaces/{real_web_entry.manifest['workspace_id']}"
        f"/planning-requests/{request_id}"
    )
    assert set(result["queries"]) == {
        request_path,
        *(f"{request_path}/proposals/{pin['revision']}" for pin in pins),
    }
    shape_error = (
        "native_real_dependencies_required"
        if selected == "main"
        else "native_real_worker_keys_required"
    )
    assert result["shapeErrors"] == [
        shape_error if index < invalid_revisions else None
        for index in range(proposal_count)
    ]
    feedback = (
        "Keep this original Task and repository contract. Repair schema 6 to exactly "
        "contract, cli, tests, integration; hard edges contract->cli, contract->tests, "
        "cli->integration, tests->integration. Use one all-required immutable "
        "code-result input group for each consumer: cli=[contract], tests=[contract], "
        "integration=[cli,tests]. Keep explicit confirmation and configured owned-check."
        if selected == "main"
        else "Keep this original Task and substantive workload. Repair schema 6 to "
        f"exactly one WorkerItem key {selected}-work; keep explicit confirmation "
        "and the existing repository contract."
    )
    assert result["feedbacks"] == [
        {
            "text": feedback,
            "request_id": request_id,
            "task_id": task_id,
            "revision": revision,
        }
        for revision in range(1, feedback_count + 1)
    ]
    assert result["currentRevision"] == proposal_count
    if accepted_revision is None:
        assert result["outcome"] is None
        assert result["ids"] == original_ids
        assert result["versions"] == {"planning_request": 11, "untouched": 7}
    else:
        accepted = pins[-1]
        assert result["outcome"] == {
            "actual_proposal_schema": 6,
            "preserved_proposals": pins,
            "current_proposal_revision": accepted_revision,
        }
        assert result["ids"] == {
            **original_ids,
            "proposal_id": accepted["id"],
            "proposal_revision": accepted_revision,
            "proposal_content_hash": accepted["content_hash"],
        }
        assert result["versions"] == {
            "planning_request": 11 + accepted_revision,
            "untouched": 7,
        }


@pytest.mark.parametrize("case", ["overlap", "sequential", "missing_run_events"])
def test_real_parallel_observation_requires_actual_run_lifetimes(real_web_entry, case):
    result = real_web_probe(
        real_web_entry,
        """
const spec = load('tests/e2e/native-harness-real.spec.ts'), selected = """
        + json.dumps(case)
        + """;
const events = (start, end) => [{kind: 'run.started', occurred_at: '2026-10-09T00:00:' + start + 'Z'}, {kind: 'run.closed', occurred_at: '2026-10-09T00:00:' + end + 'Z'}];
const left = {events: events('01', '04')}, right = {events: events(selected === 'sequential' ? '05' : '02', '06')};
if (selected === 'missing_run_events') right.events = [{kind: 'response.started', occurred_at: '2026-10-09T00:00:02Z'}];
console.log(JSON.stringify({overlap: spec.realRunOverlap(left, right)}));
""",
        explicit=False,
    )
    assert result == {"overlap": "OBSERVED" if case == "overlap" else "NOT_OBSERVED"}


@pytest.mark.parametrize(
    "case",
    [
        "original",
        "foreign_operation",
        "foreign_run",
        "missing_acceptance",
        "missing_response",
        "response_mismatch",
    ],
)
def test_real_input_requires_original_run_and_operation_trace(real_web_entry, case):
    result = real_web_probe(
        real_web_entry,
        """
const spec = load('tests/e2e/native-harness-real.spec.ts'), selected = """
        + json.dumps(case)
        + """;
const run = '12345678-1234-4234-8234-123456789abc', operation = '23456789-1234-4234-8234-123456789abc';
const trace = {run_id: run, events: ['input.accepted', 'response.started'].map((kind, index) => ({kind, sequence: index + 1,
 run_id: selected === 'foreign_run' ? operation : run, operation_id: selected === 'foreign_operation' ? run : operation,
 response_id: selected === 'response_mismatch' && index === 1 ? operation : run}))};
if (selected === 'missing_acceptance') trace.events.shift();
if (selected === 'missing_response') trace.events.pop();
console.log(JSON.stringify({admitted: spec.realInputAdmitted(trace, operation)}));
""",
        explicit=False,
    )
    assert result == {"admitted": case == "original"}


def test_fake_config_and_scenario_keep_their_original_entry(real_web_entry):
    entry = real_web_entry
    entry.manifest.update(
        mode="fake", controlled_bridge=True, controller_url="http://127.0.0.1:43123"
    )
    from tests.support.native_product_flow import SOURCE_PATHS

    entry.manifest["source_pins"] = {
        name: pin
        for name, pin in entry.manifest["source_pins"].items()
        if name in SOURCE_PATHS
    }
    entry.save()
    result = real_web_probe(
        entry,
        """
const manifest = fixture.loadOwnedManifest(manifestPath), config = load('playwright.native.config.ts').default;
console.log(JSON.stringify({scenario: fixture.nativeScenario(manifest), mode: manifest.mode, count: Object.keys(manifest.source_pins).length,
 match: config.testMatch, project: config.projects[0].name, timeout: config.timeout}));
""",
    )
    assert result == {
        "scenario": "main",
        "mode": "fake",
        "count": 26,
        "match": ["native-harness.spec.ts", "native-harness-resume.spec.ts"],
        "project": "native-chromium",
        "timeout": 3600000,
    }


@pytest.mark.parametrize("bad", ["config_secret", "owner_operation", "owner_extra"])
def test_real_entry_refuses_secret_config_and_foreign_owner_reference(
    real_web_entry, bad
):
    entry = real_web_entry
    if bad == "config_secret":
        entry.config["settings"]["api_key"] = "PRIVATE-SENTINEL-NOT-A-KEY"
        entry.save()
    else:
        owner_path = entry.root / "owner.json"
        owner = json.loads(owner_path.read_text())
        if bad == "owner_operation":
            owner["operation_id"] = str(uuid4())
        else:
            owner["extra"] = "foreign"
        owner_path.write_text(json.dumps(owner))
    assert real_web_probe(
        entry,
        """
let refused = false; try {fixture.loadOwnedManifest(manifestPath);} catch {refused = true;}
console.log(JSON.stringify({refused}));
""",
    ) == {"refused": True}


@pytest.mark.parametrize(
    "changed,allow_ui,admitted",
    [
        (["ui"], False, False),
        (["ui"], True, True),
        (["producer"], True, False),
        (["ui", "producer"], True, False),
    ],
)
def test_real_navigation_repair_manifest_waives_only_ui(
    real_web_entry, changed, allow_ui, admitted
):
    entry = real_web_entry
    names = {
        "ui": "apps/web/tests/e2e/native-harness-real.spec.ts",
        "producer": "scripts/native-harness-check.py",
    }
    originals = {
        Path(entry.manifest["source_root"]) / names[kind]: (
            Path(entry.manifest["source_root"]) / names[kind]
        ).read_bytes()
        for kind in changed
    }
    manifest_bytes = entry.path.read_bytes()
    try:
        for source in originals:
            source.write_text("private repaired source bytes")
        result = real_web_probe(
            entry,
            """
let admitted = false;
try {fixture.loadOwnedManifest(manifestPath, """
            + json.dumps(allow_ui)
            + """); admitted = true;} catch {}
console.log(JSON.stringify({admitted}));
""",
        )
    finally:
        for source, original in originals.items():
            source.write_bytes(original)
    assert entry.path.read_bytes() == manifest_bytes
    assert result == {"admitted": admitted}


@pytest.mark.parametrize("reader", ["config", "fixture", "spec"])
@pytest.mark.parametrize(
    "resume,stage,ui_admitted",
    [
        ("1", "navigation_history", True),
        (None, "navigation_history", False),
        ("0", "navigation_history", False),
        ("1", "result_verified", False),
    ],
)
def test_real_navigation_repair_readers_require_explicit_selected_recovery(
    real_web_entry, reader, resume, stage, ui_admitted
):
    result = real_web_probe(
        real_web_entry,
        """
const reader = """
        + json.dumps(reader)
        + ", resume = "
        + json.dumps(resume)
        + ", stage = "
        + json.dumps(stage)
        + """;
process.env.SIDERA_NATIVE_REAL_ENTRY = '1';
process.env.SIDERA_NATIVE_STAGE = stage;
process.env.SIDERA_NATIVE_READ_ONLY = '1';
if (resume === null) delete process.env.SIDERA_NATIVE_RESUME; else process.env.SIDERA_NATIVE_RESUME = resume;
const manifestBytes = fs.readFileSync(manifestPath), manifest = JSON.parse(manifestBytes);
const original = '12345678-1234-4234-8234-123456789abc';
const seed = new fixture.NativeFixture({}, manifest);
seed.ids.root_task_id = seed.ids.source_worker_item_id = original;
seed.stage = 'navigation_history'; seed.record('failed', {original_navigation_negative: true});
const journal = fs.readdirSync(manifest.checkpoint_path).map(name => ({name, bytes: fs.readFileSync(path.join(manifest.checkpoint_path, name))}));
const ui = path.join(manifest.source_root, 'apps/web/tests/e2e/native-harness-real.spec.ts');
const producer = path.join(manifest.source_root, 'scripts/native-harness-check.py');
const originals = new Map([[ui, fs.readFileSync(ui)], [producer, fs.readFileSync(producer)]]);
let publicWrites = 0, publicReads = 0;
const page = {route: async () => {}, on: () => {}, context: () => ({storageState: async () => ({cookies: [], origins: []})}),
 request: {get: () => {publicReads++; throw Error('no_live_read');}, post: () => {publicWrites++; throw Error('no_live_write');}}};
const results = [];
for (const drift of ['ui', 'producer', 'combined']) {
 let admitted = false, originalIds = null;
 try {
  for (const [file, bytes] of originals) fs.writeFileSync(file, bytes);
  if (drift !== 'producer') fs.writeFileSync(ui, 'private UI repair');
  if (drift !== 'ui') fs.writeFileSync(producer, 'private producer drift');
  if (reader === 'config') {
   cache.delete(path.resolve(web, 'playwright.native-real.config.ts'));
   admitted = load('playwright.native-real.config.ts').default.projects[0].name === 'native-real-chromium';
  } else if (reader === 'spec') {
   cache.delete(path.resolve(web, 'tests/e2e/native-harness-real.spec.ts')); definitions.length = 0;
   load('tests/e2e/native-harness-real.spec.ts');
   admitted = definitions.length === 1 && definitions[0].name === 'real native main';
  } else {
   await extensions[1].native({page}, async native => {
    originalIds = {root: native.id('root_task_id'), worker: native.id('source_worker_item_id'), readOnly: native.readOnly};
   });
   admitted = true;
  }
 } catch {} finally {for (const [file, bytes] of originals) fs.writeFileSync(file, bytes);}
 results.push({drift, admitted, originalIds});
}
console.log(JSON.stringify({results, publicWrites, publicReads,
 manifestUnchanged: manifestBytes.equals(fs.readFileSync(manifestPath)),
 originalJournalUnchanged: journal.every(item => item.bytes.equals(fs.readFileSync(path.join(manifest.checkpoint_path, item.name))))}));
""",
    )
    expected_ids = (
        {
            "root": "12345678-1234-4234-8234-123456789abc",
            "worker": "12345678-1234-4234-8234-123456789abc",
            "readOnly": True,
        }
        if reader == "fixture" and ui_admitted
        else None
    )
    assert result == {
        "results": [
            {"drift": "ui", "admitted": ui_admitted, "originalIds": expected_ids},
            {"drift": "producer", "admitted": False, "originalIds": None},
            {"drift": "combined", "admitted": False, "originalIds": None},
        ],
        "publicWrites": 0,
        "publicReads": 0,
        "manifestUnchanged": True,
        "originalJournalUnchanged": True,
    }


def test_real_goal_repair_submits_nonblank_original_name_rule(real_web_entry):
    result = real_web_probe(
        real_web_entry,
        """
process.env.SIDERA_NATIVE_REAL_ENTRY = '1'; process.env.SIDERA_NATIVE_STAGE = 'goal_submitted';
load('tests/e2e/native-harness-real.spec.ts');
let goal = null;
const page = {goto: async () => {}, getByRole: (role, options) => {
 if (role === 'textbox' && options.name === '给 Main Agent 的消息') return {fill: async text => {goal = text;}};
 if (role === 'button' && options.name === '发送消息') return {click: async () => {throw Error('stop_before_provider_boundary');}};
 throw Error('unexpected_goal_selector');
}};
const native = new fixture.NativeFixture(page, fixture.loadOwnedManifest(manifestPath));
native.ids.thread_id = '12345678-1234-4234-8234-123456789abc'; native.authorize = async () => {};
try {await definitions[0].fn({native});} catch {}
console.log(JSON.stringify({goal, publicWrites: native.mutations.length}));
""",
    )
    assert result["publicWrites"] == 0
    assert "name.strip() must be nonempty" in result["goal"]
    assert "reject empty and whitespace-only names" in result["goal"]
    assert "preserve the original valid name unchanged" in result["goal"]


@pytest.mark.parametrize(
    "scenario,want",
    [
        ("absent_then_applied", "applied"),
        ("pending_then_applied", "applied"),
        ("applied", "applied"),
        ("rejected", "rejected"),
        ("foreign", "UNKNOWN"),
        ("permanent_unavailable", "UNKNOWN"),
        ("permanent_pending", "UNKNOWN"),
    ],
)
def test_real_original_command_repair_queries_only_same_terminal_id(
    real_web_entry, scenario, want
):
    result = real_web_probe(
        real_web_entry,
        """
const spec = load('tests/e2e/native-harness-real.spec.ts'), scenario = """
        + json.dumps(scenario)
        + """;
const native = new fixture.NativeFixture({}, fixture.loadOwnedManifest(manifestPath));
const original = '12345678-1234-4234-8234-123456789abc', queries = [];
native.ids.input_command_id = original;
let writes = 0;
native.page.request = {post: () => {writes++; throw Error('no_write');}, put: () => {writes++; throw Error('no_write');}};
native.get = async url => {
 queries.push(url);
 if (scenario === 'permanent_unavailable' || (scenario === 'absent_then_applied' && queries.length === 1)) throw Error('temporarily_absent');
 return {command_id: scenario === 'foreign' ? native.manifest.workspace_id : original,
  status: scenario === 'rejected' ? 'rejected' : scenario === 'permanent_pending' || (scenario === 'pending_then_applied' && queries.length === 1) ? 'pending' : 'applied'};
};
let outcome = 'UNKNOWN';
try {outcome = (await spec.originalCommand(native, 'input_command_id', scenario.endsWith('then_applied') ? 2000 : 25)).status;} catch {}
console.log(JSON.stringify({outcome, queries, writes, original: native.ids.input_command_id}));
""",
        explicit=False,
    )
    assert result["outcome"] == want
    assert result["writes"] == 0
    assert result["original"] == "12345678-1234-4234-8234-123456789abc"
    assert result["queries"]
    assert set(result["queries"]) == {
        f"/api/v1/workspaces/{real_web_entry.manifest['workspace_id']}/commands/12345678-1234-4234-8234-123456789abc"
    }
    if scenario.endswith("then_applied"):
        assert len(result["queries"]) >= 2
