from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace

import pytest

from sidera_control_plane.demo import cli
from sidera_control_plane.demo.cli import (
    build_parser,
    continue_run,
    open_run,
    reset_run,
    restart_run,
)
from sidera_control_plane.demo.environment import create_demo_tokens
from sidera_control_plane.demo.state import DemoStateStore
from sidera_control_plane.demo.supervisor import DemoSupervisor
from tests.unit.interview_demo_support import manifest_for


def test_cli_accepts_the_documented_prepare_and_check_arguments() -> None:
    prepare = build_parser().parse_args(
        ["prepare", "--scenario", "mainline", "--print-manifest-path"]
    )
    check = build_parser().parse_args(
        [
            "check",
            "--suite",
            "integration",
            "--manifest",
            "/tmp/run/manifest.json",
            "--pg-port",
            "55433",
        ]
    )

    assert prepare.command == "prepare"
    assert prepare.scenario == "mainline"
    assert prepare.print_manifest_path is True
    assert check.command == "check"
    assert check.suite == "integration"
    assert check.pg_port == 55433


def test_cli_accepts_a_component_restart() -> None:
    restart = build_parser().parse_args(
        ["restart", "--manifest", "/tmp/run/manifest.json", "--component", "gateway-worker"]
    )

    assert restart.command == "restart"
    assert restart.component == "gateway-worker"


def test_restart_run_delegates_to_the_named_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    calls: list[str] = []

    class FakeSupervisor:
        def restart(self, component: str, *, timeout_seconds: float):
            assert timeout_seconds == 20
            calls.append(component)
            return object()

    monkeypatch.setattr(cli, "build_demo_process_specs", lambda *args, **kwargs: ())
    restart_run(manifest, "gateway-worker", supervisor=FakeSupervisor())

    assert calls == ["gateway-worker"]


def test_check_routes_pg_port_to_the_isolated_integration_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    calls: list[tuple[object, int | None]] = []

    def run_integration(target, *, pg_port=None):
        calls.append((target, pg_port))
        return {"status": "passed", "suite": "integration"}

    monkeypatch.setattr(cli, "_run_integration_suite", run_integration)

    result = cli.run_check_suite("integration", manifest=manifest, pg_port=55433)

    assert result == {"status": "passed", "suite": "integration"}
    assert calls == [(manifest, 55433)]


def test_cli_rejects_an_arbitrary_test_suite() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["check", "--suite", "run-this-shell"])


def test_continue_releases_only_the_named_simulation_gate(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    store = DemoStateStore(manifest.run_root / "state", manifest.run_id)

    continue_run(manifest, "parallel")

    assert store.read_gate("parallel") is True
    assert store.read_gate("before-artifact") is False
    assert store.read_gate("handoff") is False


def test_continue_can_release_the_governance_simulation_gate(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    store = DemoStateStore(manifest.run_root / "state", manifest.run_id)

    continue_run(manifest, "governance")

    assert store.read_gate("governance") is True


def test_reset_stops_processes_but_retains_run_evidence(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir(parents=True)
    evidence = manifest.run_root / "evidence.json"
    evidence.write_text("{\"retained\":true}", encoding="utf-8")
    supervisor = DemoSupervisor(manifest, specs=())

    reset_run(manifest, supervisor=supervisor)

    assert evidence.read_text(encoding="utf-8") == "{\"retained\":true}"


def test_prepare_print_manifest_path_writes_nothing_else_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = manifest_for(tmp_path)
    monkeypatch.setattr(cli, "prepare_demo_run", lambda _scenario: manifest)

    exit_code = cli.main(
        ["prepare", "--scenario", "mainline", "--print-manifest-path"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"{manifest.run_root / 'manifest.json'}\n"
    assert captured.err == ""


def test_open_uses_the_manifest_web_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    opened: list[tuple[str, int]] = []
    monkeypatch.setattr(
        cli.webbrowser,
        "open",
        lambda url, new=0: opened.append((url, new)) or True,
    )

    open_run(manifest)

    assert opened == [(manifest.endpoints.web, 2)]


def test_prepare_creates_private_runtime_directories(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir(parents=True)

    cli._prepare_run_directories(manifest)

    for name in (
        "workspaces",
        "artifacts",
        "results",
        "planning",
        "candidate-spool",
        "knowledge-spool",
    ):
        directory = manifest.run_root / name
        assert directory.is_dir()
        assert S_IMODE(directory.stat().st_mode) == 0o700


def test_notification_baseline_uses_the_demo_environment_and_private_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir(parents=True)
    tokens = create_demo_tokens(manifest.run_root)
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        cli,
        "component_environment",
        lambda *args, **kwargs: calls.append({"args": args, "kwargs": kwargs})
        or {"PATH": "/usr/bin"},
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append(
            {"command": command, "run": kwargs}
        )
        or SimpleNamespace(returncode=0),
    )

    cli._run_notification_baseline(manifest, tokens, pgpassfile=None)

    invocation = calls[1]
    command = invocation["command"]
    assert isinstance(command, tuple)
    assert command[-4:] == (
        "--workspace-id",
        str(manifest.workspace_id),
        "--owner-id",
        f"interview-demo-baseline:{manifest.run_id}",
    )
    run_options = invocation["run"]
    assert isinstance(run_options, dict)
    assert run_options["env"] == {"PATH": "/usr/bin"}
    log_path = manifest.run_root / "logs" / "notification-baseline.log"
    assert log_path.is_file()
    assert S_IMODE(log_path.stat().st_mode) == 0o600
