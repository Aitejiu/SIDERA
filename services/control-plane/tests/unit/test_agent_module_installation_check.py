from __future__ import annotations

import ast
import base64
import csv
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

CHECKER_PATH = Path(__file__).resolve().parents[4] / "scripts" / (
    "check-agent-module-installation.py"
)
COMBINATION_NAMES = (
    "agents-core",
    "control-plane-core",
    "control-plane-agents",
    "public-client",
    "gateway-core",
    "bridge-client",
    "gateway-demo",
)
EXPECTED_PROJECTS = {
    "sidera-agents": "packages/agents",
    "sidera-runtime-guard": "packages/runtime-guard",
    "sidera-control-plane-client": "packages/control-plane-client",
    "sidera-control-plane": "services/control-plane",
    "sidera-akashic-task-bridge": "integrations/akashic",
    "sidera-main-agent-gateway": "services/main-agent-gateway",
}
EXPECTED_REQUIREMENTS = {
    "agents-core": "sidera-agents==0.1.0",
    "control-plane-core": "sidera-control-plane==0.1.0",
    "control-plane-agents": "sidera-control-plane==0.1.0",
    "public-client": "sidera-control-plane-client==0.1.0",
    "gateway-core": "sidera-main-agent-gateway==0.1.0",
    "bridge-client": "sidera-akashic-task-bridge==0.1.0",
    "gateway-demo": "sidera-main-agent-gateway[demo]==0.1.0",
}
EXPECTED_COMBINATION_PROJECTS = {
    "agents-core": ("sidera-agents",),
    "control-plane-core": (
        "sidera-control-plane",
        "sidera-agents",
        "sidera-runtime-guard",
    ),
    "control-plane-agents": (
        "sidera-control-plane",
        "sidera-agents",
        "sidera-runtime-guard",
    ),
    "public-client": ("sidera-control-plane-client",),
    "gateway-core": (
        "sidera-main-agent-gateway",
        "sidera-runtime-guard",
    ),
    "bridge-client": (
        "sidera-akashic-task-bridge",
        "sidera-control-plane-client",
    ),
    "gateway-demo": (
        "sidera-main-agent-gateway",
        "sidera-akashic-task-bridge",
        "sidera-control-plane-client",
        "sidera-runtime-guard",
    ),
}
EXPECTED_SOURCE_ROOTS = {
    "sidera-agents": "packages/agents/src",
    "sidera-runtime-guard": "packages/runtime-guard/src",
    "sidera-control-plane-client": "packages/control-plane-client/src",
    "sidera-control-plane": "services/control-plane/src",
    "sidera-akashic-task-bridge": "integrations/akashic/sidera_task_bridge",
    "sidera-main-agent-gateway": "services/main-agent-gateway/src",
}


@pytest.fixture
def install_checker():
    spec = importlib.util.spec_from_file_location("task6_install_checker", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _make_repository(tmp_path: Path, checker) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    for package, relative in checker.PROJECTS.items():
        project = repository / relative
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\n', encoding="ascii"
        )
    return repository


def _make_wheels(tmp_path: Path, checker) -> dict[str, Path]:
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir(exist_ok=True)
    wheels = {}
    for package in checker.PROJECTS:
        path = wheel_dir / f"{package.replace('-', '_')}-0.1.0-py3-none-any.whl"
        path.write_bytes(f"wheel:{package}".encode("ascii"))
        wheels[package] = path
    return wheels


def _probe_summary(name: str, wheels: dict[str, Path]) -> str:
    packages = {
        package: {
            "version": "0.1.0",
            "wheel_sha256": hashlib.sha256(wheels[package].read_bytes()).hexdigest(),
            "record_match": True,
        }
        for package in EXPECTED_COMBINATION_PROJECTS[name]
    }
    return json.dumps({"status": "PASS", "packages": packages})


def test_installation_checker_script_is_present() -> None:
    assert CHECKER_PATH.is_file()


def test_repository_check_requires_all_declared_pyprojects(tmp_path, install_checker):
    repository = _make_repository(tmp_path, install_checker)

    assert install_checker.check_repository(repository) == repository.resolve()

    missing = repository / EXPECTED_PROJECTS["sidera-main-agent-gateway"] / "pyproject.toml"
    missing.unlink()
    with pytest.raises(ValueError, match="invalid_repository"):
        install_checker.check_repository(repository)


def test_output_check_never_overwrites_a_previous_run(tmp_path, install_checker):
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "evidence"
    output.mkdir()
    marker = output / "previous.json"
    marker.write_text('{"keep": true}', encoding="ascii")

    with pytest.raises(ValueError, match="output_root_refused"):
        install_checker.check_output_root(output, repository)

    assert marker.read_text(encoding="ascii") == '{"keep": true}'


def test_output_check_refuses_repository_paths_and_symlinks(tmp_path, install_checker):
    repository = tmp_path / "repository"
    repository.mkdir()
    inside = repository / "evidence"
    inside.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="output_root_refused"):
        install_checker.check_output_root(inside, repository)
    with pytest.raises(ValueError, match="output_root_refused"):
        install_checker.check_output_root(alias, repository)


def test_project_and_combination_tables_are_fixed(install_checker):
    assert install_checker.PROJECTS == EXPECTED_PROJECTS
    assert install_checker.REQUIREMENTS == EXPECTED_REQUIREMENTS
    assert install_checker.COMBINATION_PROJECTS == EXPECTED_COMBINATION_PROJECTS
    assert install_checker.SOURCE_ROOTS == EXPECTED_SOURCE_ROOTS


def test_fixed_build_venv_and_install_argv(install_checker, tmp_path):
    repository = tmp_path / "repository"
    output = tmp_path / "evidence"
    expected_build = {
        package: [
            "uv",
            "build",
            str(repository / relative),
            "--wheel",
            "--out-dir",
            str(output / "wheels"),
            "--no-create-gitignore",
        ]
        for package, relative in EXPECTED_PROJECTS.items()
    }
    expected_build["sidera-agents"].append("--offline")

    for package in EXPECTED_PROJECTS:
        assert install_checker.build_argv(repository, package, output) == expected_build[
            package
        ]
    for name in COMBINATION_NAMES:
        assert install_checker.venv_argv(name, output) == [
            "uv",
            "venv",
            str(output / f"{name}-venv"),
            "--python",
            "3.12",
        ]
        expected_install = [
            "uv",
            "pip",
            "install",
            "--python",
            str(output / f"{name}-venv" / "bin" / "python"),
            "--find-links",
            str(output / "wheels"),
        ]
        if name == "agents-core":
            expected_install.append("--offline")
        expected_install.append(EXPECTED_REQUIREMENTS[name])
        if name in {"control-plane-core", "control-plane-agents", "gateway-core"}:
            expected_install.append("httpx>=0.28,<1")
        assert install_checker.install_argv(name, output) == expected_install


def test_build_wheels_runs_only_the_fixed_build_commands(
    tmp_path, monkeypatch, install_checker
):
    repository = _make_repository(tmp_path, install_checker)
    output = tmp_path / "evidence"
    output.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        project_path = Path(argv[2])
        package = next(
            name
            for name, relative in EXPECTED_PROJECTS.items()
            if project_path == repository / relative
        )
        wheel_dir = output / "wheels"
        wheel_dir.mkdir(exist_ok=True)
        wheel = wheel_dir / f"{package.replace('-', '_')}-0.1.0-py3-none-any.whl"
        wheel.write_bytes(f"built:{package}".encode("ascii"))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)

    wheels = install_checker.build_wheels(repository, output)

    assert set(wheels) == set(EXPECTED_PROJECTS)
    assert [call[0] for call in calls] == [
        install_checker.build_argv(repository, package, output)
        for package in EXPECTED_PROJECTS
    ]
    for argv, kwargs in calls:
        assert kwargs["cwd"] == output
        assert kwargs["timeout"] == 300
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert "PYTHONPATH" not in kwargs["env"]
        assert "VIRTUAL_ENV" not in kwargs["env"]
        assert kwargs["env"]["UV_NO_CONFIG"] == "1"
        if argv[2] == str(repository / "packages/agents"):
            assert "UV_CACHE_DIR" not in kwargs["env"]
        else:
            assert Path(kwargs["env"]["UV_CACHE_DIR"]) == output / "uv-cache"


def test_build_wheels_rejects_missing_or_duplicate_artifacts(
    tmp_path, monkeypatch, install_checker
):
    repository = _make_repository(tmp_path, install_checker)
    output = tmp_path / "evidence"
    output.mkdir()
    mode = "missing"

    def fake_run(argv, **_kwargs):
        nonlocal mode
        package = next(
            name
            for name, relative in EXPECTED_PROJECTS.items()
            if Path(argv[2]) == repository / relative
        )
        wheel_dir = output / "wheels"
        wheel_dir.mkdir(exist_ok=True)
        if mode != "missing":
            (wheel_dir / f"{package.replace('-', '_')}-0.1.0-py3-none-any.whl").write_bytes(
                b"wheel"
            )
        if mode == "duplicate" and package == "sidera-runtime-guard":
            (wheel_dir / "sidera_runtime_guard-0.1.0-py2-none-any.whl").write_bytes(
                b"duplicate"
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)
    with pytest.raises(install_checker.CheckFailure) as missing:
        install_checker.build_wheels(repository, output)
    assert missing.value.code == "build_failed"

    mode = "duplicate"
    with pytest.raises(install_checker.CheckFailure) as duplicate:
        install_checker.build_wheels(repository, output)
    assert duplicate.value.code == "build_failed"


@pytest.mark.parametrize("name", COMBINATION_NAMES)
def test_combination_uses_fixed_install_clean_environment_and_isolated_probe(
    name, tmp_path, monkeypatch, install_checker
):
    output = tmp_path / "evidence"
    output.mkdir()
    wheels = _make_wheels(tmp_path, install_checker)
    repository = _make_repository(tmp_path, install_checker)
    if name == "agents-core":
        example = repository / "packages/agents/examples/standalone.py"
        example.parent.mkdir()
        example.write_text("# Controlled public example fixture\n", encoding="ascii")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/source")
    monkeypatch.setenv("VIRTUAL_ENV", "/untrusted/venv")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-cfg1-fake-not-a-credential")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "uv":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout=_probe_summary(name, wheels), stderr=""
        )

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)

    result = install_checker.check_combination(name, wheels, output, repository)

    assert result["status"] == "PASS"
    assert calls[0][0] == install_checker.venv_argv(name, output)
    assert calls[1][0] == install_checker.install_argv(name, output)
    probe_argv, probe_kwargs = calls[2]
    assert Path(probe_argv[0]) == output / f"{name}-venv" / "bin" / "python"
    assert probe_argv[1:4] == ["-I", "-c", install_checker.PROBE_SOURCE]
    assert probe_argv[4] == name
    assert json.loads(probe_argv[5]) == {
        package: str(wheels[package]) for package in EXPECTED_COMBINATION_PROJECTS[name]
    }
    if name == "agents-core":
        assert probe_argv[6] == "# Controlled public example fixture\n"
    else:
        assert len(probe_argv) == 6
    assert len(calls) == 3
    for index, (_argv, kwargs) in enumerate(calls):
        assert kwargs["cwd"] == output
        assert kwargs["timeout"] == (30 if index == 2 else 300)
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert "PYTHONPATH" not in kwargs["env"]
        assert "VIRTUAL_ENV" not in kwargs["env"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "UV_PROJECT_ENVIRONMENT" not in kwargs["env"]
        assert kwargs["env"]["UV_NO_CONFIG"] == "1"
        if name == "agents-core":
            assert "UV_CACHE_DIR" not in kwargs["env"]
        else:
            assert Path(kwargs["env"]["UV_CACHE_DIR"]) == output / "uv-cache"
    assert probe_kwargs["timeout"] == 30


@pytest.mark.parametrize(
    ("failed_call", "expected_code", "expected_timeout"),
    [
        (0, "install_failed", 300),
        (1, "install_failed", 300),
        (2, "probe_failed", 30),
    ],
)
def test_combination_timeout_maps_to_safe_code(
    failed_call, expected_code, expected_timeout, tmp_path, monkeypatch, install_checker
):
    output = tmp_path / "evidence"
    output.mkdir()
    wheels = _make_wheels(tmp_path, install_checker)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) - 1 == failed_call:
            raise subprocess.TimeoutExpired(
                argv,
                kwargs["timeout"],
                output="sk-cfg1-fake-not-a-credential",
                stderr="sk-cfg1-fake-not-a-credential",
            )
        if argv[0] == "uv":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_probe_summary("public-client", wheels),
            stderr="",
        )

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)

    with pytest.raises(install_checker.CheckFailure) as failure:
        install_checker.check_combination("public-client", wheels, output)

    assert failure.value.code == expected_code
    assert "sk-cfg1-fake-not-a-credential" not in str(failure.value)
    assert calls[failed_call][1]["timeout"] == expected_timeout


def test_public_client_probe_uses_task_active_plan_and_unknown_with_mock_transport(
    tmp_path, install_checker
):
    probe_tree = ast.parse(install_checker.PROBE_SOURCE)
    probe_error = next(
        node
        for node in probe_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProbeFailure"
    )
    public_probe = next(
        node
        for node in probe_tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_probe_public_client"
    )
    public_probe.body.append(
        ast.Return(value=ast.Name(id="seen", ctx=ast.Load()))
    )
    client_source = CHECKER_PATH.parents[1] / "packages/control-plane-client/src"
    harness = "\n\n".join(
        (
            "import asyncio",
            "import json",
            "import sys",
            f"sys.path.insert(0, {str(client_source)!r})",
            ast.unparse(probe_error),
            ast.unparse(public_probe),
            "print(json.dumps(asyncio.run(_probe_public_client())))",
        )
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", harness],
        cwd=tmp_path,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    workspace_id = "10000000-0000-0000-0000-000000000002"
    task_id = "10000000-0000-0000-0000-000000000005"
    assert json.loads(result.stdout) == [
        [
            "GET",
            f"/api/v1/tasks/{task_id}",
            {"workspace_id": workspace_id},
        ],
        [
            "GET",
            f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/active-plan",
            {},
        ],
        ["POST", "/api/v1/commands", {}],
    ]


@pytest.mark.parametrize(
    "missing_path",
    [None, "/healthz", "/api/v1/tasks", "/api/v1/commands"],
)
def test_control_plane_probe_uses_public_openapi_paths(
    missing_path, tmp_path, install_checker
):
    probe_tree = ast.parse(install_checker.PROBE_SOURCE)
    probe_error = next(
        node
        for node in probe_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProbeFailure"
    )
    control_plane_probe = next(
        node
        for node in probe_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_probe_control_plane_core"
    )
    source = CHECKER_PATH.parents[1] / "services/control-plane/src"
    required_paths = ("/healthz", "/api/v1/tasks", "/api/v1/commands")
    paths = tuple(path for path in required_paths if path != missing_path)
    harness = "\n\n".join(
        (
            "import importlib",
            "import sys",
            f"sys.path.insert(0, {str(source)!r})",
            ast.unparse(probe_error),
            f"ROUTES = {paths!r}",
            "class _ProbeApp:",
            "    routes = [object()]",
            "    def openapi(self):",
            "        return {'paths': {path: {} for path in ROUTES}}",
            "from sidera_control_plane.api import app as app_module",
            "app_module.create_app = lambda _settings: _ProbeApp()",
            ast.unparse(control_plane_probe),
            "try:",
            "    _probe_control_plane_core()",
            "except ProbeFailure as failure:",
            "    print(failure.code)",
            "else:",
            "    print('PASS')",
        )
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", harness],
        cwd=tmp_path,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    expected = "probe_failed" if missing_path is not None else "PASS"
    assert result.stdout.strip() == expected


def test_failed_probe_keeps_observed_non_expected_version(
    tmp_path, monkeypatch, install_checker
):
    output = tmp_path / "evidence"
    output.mkdir()
    wheels = _make_wheels(tmp_path, install_checker)
    package = "sidera-control-plane-client"
    payload = {
        "status": "FAIL",
        "code": "dependency_boundary_failed",
        "packages": {package: {"version": "0.2.0", "record_match": True}},
    }

    def fake_run(argv, **_kwargs):
        if argv[0] == "uv":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv, 1, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)

    with pytest.raises(install_checker.CheckFailure) as failure:
        install_checker.check_combination("public-client", wheels, output)

    assert failure.value.code == "dependency_boundary_failed"
    assert failure.value.packages[package]["version"] == "0.2.0"
    assert failure.value.packages[package]["record_match"] is True


def test_forged_probe_pass_with_wrong_version_is_rejected(
    tmp_path, monkeypatch, install_checker
):
    output = tmp_path / "evidence"
    output.mkdir()
    wheels = _make_wheels(tmp_path, install_checker)
    package = "sidera-control-plane-client"
    payload = {
        "status": "PASS",
        "packages": {package: {"version": "0.2.0", "record_match": True}},
    }

    def fake_run(argv, **_kwargs):
        if argv[0] == "uv":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)

    with pytest.raises(install_checker.CheckFailure) as failure:
        install_checker.check_combination("public-client", wheels, output)

    assert failure.value.code == "dependency_boundary_failed"
    assert failure.value.packages[package]["version"] == "0.2.0"


def test_invalid_probe_version_is_redacted_from_cli_and_evidence(
    tmp_path, monkeypatch, capsys, install_checker
):
    repository = _make_repository(tmp_path, install_checker)
    output = tmp_path / "evidence"
    package = "sidera-control-plane-client"
    secret_marker = "sk-cfg1-fake-not-a-credential"
    payload = {
        "status": "FAIL",
        "code": "dependency_boundary_failed",
        "packages": {
            package: {"version": secret_marker, "record_match": False}
        },
    }

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["uv", "build"]:
            wheel_dir = Path(argv[argv.index("--out-dir") + 1])
            project = Path(argv[2])
            built_package = next(
                name
                for name, relative in EXPECTED_PROJECTS.items()
                if project == repository / relative
            )
            wheel_dir.mkdir(parents=True, exist_ok=True)
            wheel = wheel_dir / (
                f"{built_package.replace('-', '_')}-0.1.0-py3-none-any.whl"
            )
            wheel.write_bytes(f"wheel:{built_package}".encode("ascii"))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "uv":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv, 1, stdout=json.dumps(payload), stderr=secret_marker
        )

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)

    exit_code = install_checker.main(
        [
            "--repository",
            str(repository),
            "--output-root",
            str(output),
            "--combination",
            "public-client",
        ]
    )

    rendered = capsys.readouterr().out
    evidence = json.loads((output / "evidence.json").read_text(encoding="ascii"))
    assert exit_code == 1
    assert secret_marker not in rendered
    assert secret_marker not in (output / "evidence.json").read_text(encoding="ascii")
    assert (
        evidence["combinations"]["public-client"]["packages"][package]["version"]
        == "invalid"
    )


def test_source_fingerprints_read_only_pyprojects_and_named_python_roots(
    tmp_path, install_checker
):
    repository = _make_repository(tmp_path, install_checker)
    for package, relative in EXPECTED_SOURCE_ROOTS.items():
        source_root = repository / relative
        source_root.mkdir(parents=True, exist_ok=True)
        (source_root / "__init__.py").write_text(f"PACKAGE = {package!r}\n", encoding="ascii")
        project = repository / EXPECTED_PROJECTS[package]
        (project / ".env").write_text("sk-cfg1-fake-marker\n", encoding="ascii")
        (project / ".venv").mkdir()
        (project / ".venv" / "ignored.py").write_text("ignored = True\n", encoding="ascii")

    before = install_checker.source_fingerprints(repository)
    (repository / "README.md").write_text("ignored metadata\n", encoding="ascii")
    (repository / ".superpowers").mkdir()
    (repository / ".superpowers" / "runtime.json").write_text(
        "ignored runtime\n", encoding="ascii"
    )
    after_unrelated = install_checker.source_fingerprints(repository)
    assert after_unrelated == before

    changed_package = "sidera-control-plane-client"
    source = repository / EXPECTED_SOURCE_ROOTS[changed_package] / "__init__.py"
    source.write_text("PACKAGE = 'changed'\n", encoding="ascii")
    after_source_change = install_checker.source_fingerprints(repository)
    assert after_source_change[changed_package] != before[changed_package]
    assert {
        package: digest
        for package, digest in after_source_change.items()
        if package != changed_package
    } == {
        package: digest
        for package, digest in before.items()
        if package != changed_package
    }


def _wheel_digest(content: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(
        b"="
    ).decode("ascii")


def test_record_hash_mismatch_detects_same_name_version_fake_wheel(
    tmp_path, install_checker
):
    wheel = tmp_path / "sidera_control_plane_client-0.1.0-py3-none-any.whl"
    installed_root = tmp_path / "site-packages"
    source_name = "sidera_control_plane_client/__init__.py"
    metadata_name = "sidera_control_plane_client-0.1.0.dist-info/METADATA"
    record_name = "sidera_control_plane_client-0.1.0.dist-info/RECORD"
    source = b"VALUE = 'built wheel'\n"
    metadata = (
        b"Metadata-Version: 2.3\nName: sidera-control-plane-client\n"
        b"Version: 0.1.0\n\n"
    )
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    writer.writerow((source_name, _wheel_digest(source), len(source)))
    writer.writerow((metadata_name, _wheel_digest(metadata), len(metadata)))
    writer.writerow((record_name, "", ""))
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(source_name, source)
        archive.writestr(metadata_name, metadata)
        archive.writestr(record_name, record.getvalue())

    installed_source = installed_root / source_name
    installed_metadata = installed_root / metadata_name
    installed_source.parent.mkdir(parents=True)
    installed_metadata.parent.mkdir(parents=True)
    installed_source.write_bytes(b"VALUE = 'same version, different content'\n")
    installed_metadata.write_bytes(metadata)

    helper_check = install_checker.PROVENANCE_HELPER_SOURCE + r'''
import json
import sys
print(json.dumps(wheel_record_matches(sys.argv[1], sys.argv[2])))
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", helper_check, str(wheel), str(installed_root)],
        cwd=tmp_path,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "false"


def test_main_single_combination_keeps_other_results_not_run(
    tmp_path, monkeypatch, capsys, install_checker
):
    repository = _make_repository(tmp_path, install_checker)
    output = tmp_path / "evidence"
    wheels = _make_wheels(tmp_path, install_checker)
    calls = []

    def fake_build(actual_repository, actual_output, projects):
        assert actual_repository == repository.resolve()
        assert actual_output == output.resolve()
        assert projects == ("sidera-control-plane-client",)
        return {"sidera-control-plane-client": wheels["sidera-control-plane-client"]}

    def fake_check(name, selected_wheels, actual_output, actual_repository):
        assert set(selected_wheels) == {"sidera-control-plane-client"}
        assert actual_output == output.resolve()
        assert actual_repository == repository.resolve()
        calls.append(name)
        return {"status": "PASS", "packages": {}}

    monkeypatch.setattr(install_checker, "build_wheels", fake_build)
    monkeypatch.setattr(install_checker, "check_combination", fake_check)

    exit_code = install_checker.main(
        [
            "--repository",
            str(repository),
            "--output-root",
            str(output),
            "--combination",
            "public-client",
        ]
    )

    rendered = capsys.readouterr().out
    result = json.loads(rendered)
    assert rendered.count("\n") == 1
    assert exit_code == 0
    assert result["status"] == "PASS"
    assert result["provider_calls"] == 0
    assert result["combinations"]["public-client"]["status"] == "PASS"
    assert {
        name: item["status"]
        for name, item in result["combinations"].items()
        if name != "public-client"
    } == {name: "NOT_RUN" for name in COMBINATION_NAMES if name != "public-client"}
    assert calls == ["public-client"]
    assert (output / "evidence.json").is_file()


def test_main_failure_exit_and_json_do_not_echo_subprocess_output(
    tmp_path, monkeypatch, capsys, install_checker
):
    repository = _make_repository(tmp_path, install_checker)
    output = tmp_path / "evidence"
    secret_marker = "sk-cfg1-fake-not-a-credential"

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout=secret_marker, stderr=f"failed: {secret_marker}"
        )

    monkeypatch.setattr(install_checker.subprocess, "run", fake_run)

    exit_code = install_checker.main(
        ["--repository", str(repository), "--output-root", str(output)]
    )

    rendered = capsys.readouterr().out
    result = json.loads(rendered)
    assert rendered.count("\n") == 1
    assert secret_marker not in rendered
    assert exit_code == 1
    assert result["status"] == "FAIL"
    assert result["code"] == "build_failed"
    assert result["provider_calls"] == 0
    assert all(
        item["status"] == "NOT_RUN" for item in result["combinations"].values()
    )
    assert (output / "evidence.json").is_file()


def test_main_combination_failure_exits_one_and_preserves_not_run(
    tmp_path, monkeypatch, capsys, install_checker
):
    repository = _make_repository(tmp_path, install_checker)
    output = tmp_path / "evidence"
    wheels = _make_wheels(tmp_path, install_checker)

    monkeypatch.setattr(install_checker, "build_wheels", lambda *_args: wheels)

    def fail_combination(name, _wheels, actual_output, actual_repository):
        assert actual_output == output.resolve()
        assert actual_repository == repository.resolve()
        if name == "gateway-core":
            raise install_checker.CheckFailure("dependency_boundary_failed", name)
        pytest.fail(f"unexpected combination: {name}")

    monkeypatch.setattr(install_checker, "check_combination", fail_combination)

    exit_code = install_checker.main(
        [
            "--repository",
            str(repository),
            "--output-root",
            str(output),
            "--combination",
            "gateway-core",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["status"] == "FAIL"
    assert result["combinations"]["gateway-core"]["status"] == "FAIL"
    assert result["combinations"]["gateway-core"]["code"] == (
        "dependency_boundary_failed"
    )
    assert all(
        result["combinations"][name]["status"] == "NOT_RUN"
        for name in COMBINATION_NAMES
        if name != "gateway-core"
    )
