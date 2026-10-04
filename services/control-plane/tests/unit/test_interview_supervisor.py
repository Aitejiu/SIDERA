from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

from sidera_control_plane.demo.environment import create_demo_tokens
from sidera_control_plane.demo.supervisor import (
    DemoSupervisor,
    ProcessIdentityError,
    ProcessSpec,
    SupervisorError,
    SupervisorStartError,
    _assert_port_available,
    build_demo_process_specs,
)
from sidera_control_plane.maintenance_lock import maintenance_lock
from tests.unit.interview_demo_support import manifest_for


def _sleep_spec(root: Path, name: str, *, port: int | None = None) -> ProcessSpec:
    return ProcessSpec(
        name=name,
        argv=(sys.executable, "-u", "-c", "import time;time.sleep(60)"),
        cwd=root,
        explicit_env={"PATH": os.environ.get("PATH", "/usr/bin")},
        port=port,
        startup_timeout_seconds=2,
    )


def _supervisor(root: Path, *specs: ProcessSpec, run_id=None) -> DemoSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    manifest = manifest_for(root.parent, run_id=run_id).model_copy(
        update={"run_root": root}
    )
    return DemoSupervisor(manifest, specs=specs)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _cleanup_test_process(pid: int) -> None:
    if not _pid_exists(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.02)


def test_supervisor_refuses_a_run_with_an_active_maintenance_lock(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(tmp_path / "run", _sleep_spec(tmp_path, "worker"))

    with maintenance_lock(tmp_path / "run", owner="test-holder"), pytest.raises(
        SupervisorError, match="maintenance_busy"
    ):
        supervisor.start()


def test_supervisor_refuses_a_port_owned_by_an_unregistered_process(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    supervisor = _supervisor(tmp_path / "run", _sleep_spec(tmp_path, "web", port=port))
    try:
        with pytest.raises(SupervisorStartError, match="port_in_use"):
            supervisor.start()
    finally:
        listener.close()


def test_port_probe_allows_rebind_after_a_listener_closes_an_http_connection() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    server, _ = listener.accept()

    server.close()
    client.close()
    listener.close()

    _assert_port_available(port)


def test_supervisor_rejects_a_reused_pid_without_signalling_it(tmp_path: Path) -> None:
    spec = _sleep_spec(tmp_path, "worker")
    supervisor = _supervisor(tmp_path / "run", spec)
    identity = supervisor.start()["worker"]
    identity_path = tmp_path / "run" / "processes" / "worker.json"
    payload = json.loads(identity_path.read_text(encoding="utf-8"))
    payload["started_at"] = "a different process start"
    identity_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        with pytest.raises(ProcessIdentityError, match="identity_mismatch"):
            supervisor.stop()
        assert _pid_exists(identity.pid)
    finally:
        _cleanup_test_process(identity.pid)


def test_supervisor_treats_a_terminated_zombie_as_stopped(tmp_path: Path) -> None:
    spec = _sleep_spec(tmp_path, "worker")
    supervisor = _supervisor(tmp_path / "run", spec)
    identity = supervisor.start()["worker"]

    os.killpg(identity.pid, signal.SIGTERM)
    stopped = supervisor.stop()

    assert stopped == ()
    assert not _pid_exists(identity.pid)


def test_supervisor_refuses_to_stop_a_process_owned_by_another_run(tmp_path: Path) -> None:
    spec = _sleep_spec(tmp_path, "worker")
    first_run = manifest_for(tmp_path / "run")
    first_run.run_root.mkdir(parents=True)
    owner = DemoSupervisor(first_run, specs=(spec,))
    identity = owner.start()["worker"]
    foreign_run = manifest_for(tmp_path / "run")
    foreign_owner = DemoSupervisor(foreign_run, specs=(spec,))

    try:
        with pytest.raises(ProcessIdentityError, match="run_id_mismatch"):
            foreign_owner.stop()
        assert _pid_exists(identity.pid)
    finally:
        owner.stop()


def test_partial_start_rolls_back_only_processes_started_by_this_attempt(
    tmp_path: Path,
) -> None:
    first = _sleep_spec(tmp_path, "first")
    missing = ProcessSpec(
        name="second",
        argv=(str(tmp_path / "missing-executable"),),
        cwd=tmp_path,
        explicit_env={},
        startup_timeout_seconds=1,
    )
    supervisor = _supervisor(tmp_path / "run", first, missing)

    with pytest.raises(SupervisorStartError, match="partial_start"):
        supervisor.start()

    identity_path = tmp_path / "run" / "processes" / "first.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert not _pid_exists(identity["pid"])
    assert (tmp_path / "run" / "logs" / "first.log").exists()


def test_start_and_stop_are_idempotent_for_an_owned_process(tmp_path: Path) -> None:
    supervisor = _supervisor(
        tmp_path / "run", _sleep_spec(tmp_path, "worker")
    )

    first = supervisor.start()["worker"]
    second = supervisor.start()["worker"]
    assert second.pid == first.pid

    supervisor.stop()
    supervisor.stop()

    assert not _pid_exists(first.pid)


def test_restart_replaces_only_the_named_owned_process(tmp_path: Path) -> None:
    supervisor = _supervisor(
        tmp_path / "run",
        _sleep_spec(tmp_path, "first"),
        _sleep_spec(tmp_path, "second"),
    )

    identities = supervisor.start()
    replacement = supervisor.restart("first")

    try:
        assert replacement.component == "first"
        assert replacement.pid != identities["first"].pid
        assert _pid_exists(replacement.pid)
        assert _pid_exists(identities["second"].pid)
    finally:
        supervisor.stop()


def test_stop_uses_one_bounded_time_budget_for_all_processes(tmp_path: Path) -> None:
    ignore_term = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    specs = tuple(
        ProcessSpec(
            name=f"worker-{index}",
            argv=(sys.executable, "-u", "-c", ignore_term),
            cwd=tmp_path,
            explicit_env={"PATH": os.environ.get("PATH", "/usr/bin")},
            startup_timeout_seconds=2,
        )
        for index in range(3)
    )
    supervisor = _supervisor(tmp_path / "run", *specs)
    identities = supervisor.start()

    started = time.monotonic()
    stopped = supervisor.stop(timeout_seconds=0.6)
    elapsed = time.monotonic() - started

    assert set(stopped) == set(identities)
    assert elapsed < 1.0
    assert all(not _pid_exists(identity.pid) for identity in identities.values())


def test_worker_without_a_ready_record_is_rolled_back(tmp_path: Path) -> None:
    spec = ProcessSpec(
        name="worker",
        argv=(sys.executable, "-u", "-c", "import time;time.sleep(60)"),
        cwd=tmp_path,
        explicit_env={"PATH": os.environ.get("PATH", "/usr/bin")},
        require_log_ready_status=True,
        startup_timeout_seconds=0.2,
    )
    supervisor = _supervisor(tmp_path / "run", spec)

    with pytest.raises(SupervisorStartError, match="component_not_ready:worker"):
        supervisor.start()

    stored = json.loads(
        (tmp_path / "run" / "processes" / "worker.json").read_text(
            encoding="utf-8"
        )
    )
    assert not _pid_exists(stored["pid"])


def test_identity_persists_run_pid_start_time_and_argument_hash(tmp_path: Path) -> None:
    supervisor = _supervisor(
        tmp_path / "run", _sleep_spec(tmp_path, "worker")
    )
    identity = supervisor.start()["worker"]

    try:
        stored = json.loads(
            (tmp_path / "run" / "processes" / "worker.json").read_text(
                encoding="utf-8"
            )
        )
        assert stored["run_id"] == str(supervisor.manifest.run_id)
        assert stored["pid"] == identity.pid
        assert stored["started_at"]
        assert len(stored["argv_hash"]) == 64
    finally:
        supervisor.stop()


def test_full_process_spec_uses_only_the_fixed_loopback_component_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from sidera_control_plane.demo import supervisor

    roots = [
        supervisor._CONTROL_PLANE_ROOT,
        supervisor._KNOWLEDGE_ROOT,
        supervisor._GATEWAY_ROOT,
    ]
    selected = []

    def interpreter(project_root):
        assert project_root in roots
        selected.append(project_root)
        return sys.executable

    monkeypatch.setattr(supervisor, "_python_executable", interpreter)
    node_selections = []

    def node_executable(command):
        assert command == "node"
        node_selections.append(command)
        return str(tmp_path / "node")

    monkeypatch.setattr(supervisor.shutil, "which", node_executable)
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir(parents=True)
    create_demo_tokens(manifest.run_root)

    specs = build_demo_process_specs(
        manifest,
        inherited_env={"PATH": "/usr/bin", "DEEPSEEK_API_KEY": "must-not-leak"},
    )
    assert selected == roots
    assert node_selections == ["node"]

    assert [spec.name for spec in specs] == [
        "control-plane",
        "knowledge-service",
        "demo-worker-heartbeat",
        "planning-worker",
        "context-package-worker",
        "knowledge-candidate-worker",
        "activation-worker",
        "scheduler-worker",
        "gateway-worker",
        "plan-transition-worker",
        "collaboration-worker",
        "notification-worker",
        "result-worker",
        "demo-notification-sink",
        "main-agent-gateway",
        "web",
    ]
    assert all("akashic" not in " ".join(spec.argv) for spec in specs)
    assert all("DEEPSEEK_API_KEY" not in spec.explicit_env for spec in specs)
    assert all(
        "127.0.0.1" in " ".join(spec.argv)
        for spec in (specs[0], specs[1], specs[-2])
    )
    assert specs[0].health_url == f"{manifest.endpoints.control_plane}/healthz"
    assert specs[-2].health_url == f"{manifest.endpoints.main_agent}/main-agent/v1/health"
    assert specs[-1].port == int(manifest.endpoints.web.rsplit(":", 1)[1])
    assert specs[-1].argv[0].endswith("/node")
    assert specs[-1].argv[1].endswith("/apps/web/node_modules/vite/bin/vite.js")


def test_restore_demo_specs_exclude_all_writers_and_provider_processes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from sidera_control_plane.demo import supervisor

    roots = [
        supervisor._CONTROL_PLANE_ROOT,
        supervisor._KNOWLEDGE_ROOT,
        supervisor._GATEWAY_ROOT,
    ]
    selected = []

    def interpreter(project_root):
        assert project_root in roots
        selected.append(project_root)
        return sys.executable

    monkeypatch.setattr(supervisor, "_python_executable", interpreter)
    node_selections = []

    def node_executable(command):
        assert command == "node"
        node_selections.append(command)
        return str(tmp_path / "node")

    monkeypatch.setattr(supervisor.shutil, "which", node_executable)
    manifest = manifest_for(tmp_path).model_copy(
        update={"restore_mode": "read_only"}
    )
    manifest.run_root.mkdir(parents=True)
    create_demo_tokens(manifest.run_root)

    specs = build_demo_process_specs(manifest, inherited_env={"PATH": "/usr/bin"})
    assert selected == roots
    assert node_selections == ["node"]

    assert [spec.name for spec in specs] == [
        "control-plane",
        "knowledge-service",
        "main-agent-gateway",
        "web",
    ]


def test_spawn_rejects_a_forbidden_explicit_environment_value(tmp_path: Path) -> None:
    spec = _sleep_spec(tmp_path, "worker")
    unsafe_spec = ProcessSpec(
        name=spec.name,
        argv=spec.argv,
        cwd=spec.cwd,
        explicit_env={"PATH": "/usr/bin", "DEEPSEEK_API_KEY": "fake-test-secret"},
    )
    supervisor = _supervisor(tmp_path / "run", unsafe_spec)

    with pytest.raises(ValueError, match="forbidden_environment:DEEPSEEK_API_KEY"):
        supervisor._spawn(unsafe_spec)
