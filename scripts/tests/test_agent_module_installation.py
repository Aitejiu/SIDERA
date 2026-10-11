import ast
import json
import shutil
import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_PATHS = {
    "sidera-agents": "packages/agents",
    "sidera-runtime-guard": "packages/runtime-guard",
    "sidera-control-plane-client": "packages/control-plane-client",
    "sidera-control-plane": "services/control-plane",
    "sidera-akashic-task-bridge": "integrations/akashic",
    "sidera-main-agent-gateway": "services/main-agent-gateway",
    "sidera-product": "apps/server",
    "sidera-model-connections": "packages/model-connections",
}
CLOSURES = {
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
    "gateway-core": ("sidera-main-agent-gateway", "sidera-runtime-guard"),
    "bridge-client": ("sidera-akashic-task-bridge", "sidera-control-plane-client"),
    "gateway-demo": (
        "sidera-main-agent-gateway",
        "sidera-akashic-task-bridge",
        "sidera-control-plane-client",
        "sidera-runtime-guard",
    ),
    "native-product": (
        "sidera-product",
        "sidera-agents",
        "sidera-control-plane",
        "sidera-control-plane-client",
        "sidera-model-connections",
        "sidera-runtime-guard",
    ),
}


@pytest.fixture
def script():
    path = Path(__file__).resolve().parents[1] / "check-agent-module-installation.py"
    spec = spec_from_file_location("agent_module_installation_contract", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repository(tmp_path, projects):
    repository = tmp_path / "repository"
    for project in projects:
        path = repository / PROJECT_PATHS[project]
        path.mkdir(parents=True)
        (path / "pyproject.toml").write_text(
            f'[project]\nname = "{project}"\nversion = "0.1.0"\n'
        )
        source = path / "src" / project.replace("-", "_")
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("VALUE = 1\n")
        if project == "sidera-agents":
            example = path / "examples"
            example.mkdir()
            (example / "standalone.py").write_text("def main(): return {}\n")
    return repository


def _process_boundary(script, monkeypatch, repository, *, failed=False):
    commands = []

    def run(argv, output_root, timeout, code, combination=None):
        commands.append(list(argv))
        if argv[:2] == ["uv", "build"]:
            project = next(
                name
                for name, relative in PROJECT_PATHS.items()
                if Path(argv[2]) == repository / relative
            )
            wheel_directory = output_root / "wheels"
            wheel_directory.mkdir(exist_ok=True)
            artifact = (
                wheel_directory / f"{project.replace('-', '_')}-0.1.0-py3-none-any.whl"
            )
            artifact.write_bytes(b"unit-process-boundary-artifact")
        if "-I" in argv:
            name = argv[argv.index("-c") + 2]
            payload = {
                "status": "FAIL" if failed else "PASS",
                "code": "dependency_boundary_failed" if failed else None,
                "packages": {
                    package: {"version": "0.1.0", "record_match": not failed}
                    for package in CLOSURES[name]
                },
            }
            return subprocess.CompletedProcess(
                argv, int(failed), json.dumps(payload), ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(script, "_run", run)
    return commands


@pytest.mark.parametrize(
    "combination,project",
    [
        ("agents-core", "sidera-agents"),
        ("public-client", "sidera-control-plane-client"),
    ],
)
@pytest.mark.parametrize("failed", [False, True])
def test_selection_precedes_validation_fingerprint_build_and_aggregation(
    script, tmp_path, monkeypatch, capsys, combination, project, failed
):
    repository = _repository(tmp_path, (project,))
    output = tmp_path / "evidence"
    commands = _process_boundary(script, monkeypatch, repository, failed=failed)
    result = script.main(
        [
            "--repository",
            str(repository),
            "--output-root",
            str(output),
            "--combination",
            combination,
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == int(failed)
    assert report["status"] == ("FAIL" if failed else "PASS")
    assert report["provider_calls"] == 0
    assert report["combinations"][combination]["status"] == (
        "FAIL" if failed else "PASS"
    )
    assert set(report["combinations"][combination]["source_fingerprint"]) == {project}
    assert set(report["combinations"][combination]["packages"]) == {project}
    assert all(
        item == {"status": "NOT_RUN"}
        for name, item in report["combinations"].items()
        if name != combination
    )
    assert [
        Path(argv[2]).relative_to(repository).as_posix()
        for argv in commands
        if argv[:2] == ["uv", "build"]
    ] == [PROJECT_PATHS[project]]
    assert len([argv for argv in commands if "-I" in argv]) == 1
    assert [
        argv[-1]
        for argv in commands
        if argv[:3] == ["uv", "pip", "install"] and combination != "agents-core"
    ] == (
        ["sidera-control-plane-client==0.1.0"]
        if project == "sidera-control-plane-client"
        else []
    )
    assert json.loads((output / "evidence.json").read_text()) == report


@pytest.mark.parametrize("failed", [False, True])
def test_native_product_selection_has_exact_closure_without_legacy_projects(
    script, tmp_path, monkeypatch, capsys, failed
):
    expected = (
        "sidera-product",
        "sidera-agents",
        "sidera-control-plane",
        "sidera-control-plane-client",
        "sidera-model-connections",
        "sidera-runtime-guard",
    )
    repository = _repository(tmp_path, expected)
    output = tmp_path / "evidence"
    commands = _process_boundary(script, monkeypatch, repository, failed=failed)
    assert script.main(
        [
            "--repository",
            str(repository),
            "--output-root",
            str(output),
            "--combination",
            "native-product",
        ]
    ) == int(failed)
    report = json.loads(capsys.readouterr().out)
    selected = report["combinations"]["native-product"]
    assert report["status"] == selected["status"] == ("FAIL" if failed else "PASS")
    assert set(selected["source_fingerprint"]) == set(expected)
    assert set(selected["packages"]) == set(expected)
    assert all(
        row == {"status": "NOT_RUN"}
        for name, row in report["combinations"].items()
        if name != "native-product"
    )
    assert [
        Path(argv[2]).relative_to(repository).as_posix()
        for argv in commands
        if argv[:2] == ["uv", "build"]
    ] == [PROJECT_PATHS[package] for package in expected]
    probes = [argv for argv in commands if "-I" in argv]
    assert len(probes) == 1 and set(json.loads(probes[0][5])) == set(expected)
    assert not any("--editable" in argv for argv in commands)
    assert [argv[-1] for argv in commands if argv[:3] == ["uv", "pip", "install"]] == [
        "sidera-product==0.1.0"
    ]
    assert report["provider_calls"] == 0
    assert json.loads((output / "evidence.json").read_text()) == report


def test_omitted_selection_retains_all_combinations(
    script, tmp_path, monkeypatch, capsys
):
    repository = _repository(tmp_path, PROJECT_PATHS)
    commands = _process_boundary(script, monkeypatch, repository)
    assert (
        script.main(
            [
                "--repository",
                str(repository),
                "--output-root",
                str(tmp_path / "evidence"),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "PASS"
    assert set(report["combinations"]) == set(CLOSURES)
    assert all(item["status"] == "PASS" for item in report["combinations"].values())
    assert len([argv for argv in commands if argv[:2] == ["uv", "build"]]) == 8
    assert {argv[argv.index("-c") + 2] for argv in commands if "-I" in argv} == set(
        CLOSURES
    )


@pytest.mark.parametrize("unsafe", ["inside_repository", "nonempty", "symlink"])
def test_selected_installation_retains_output_root_refusal(
    script, tmp_path, monkeypatch, capsys, unsafe
):
    repository = _repository(tmp_path, PROJECT_PATHS)
    output = tmp_path / "evidence"
    if unsafe == "inside_repository":
        output = repository / "output"
    elif unsafe == "nonempty":
        output.mkdir()
        (output / "retained").write_text("existing evidence")
    else:
        target = tmp_path / "target"
        target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    commands = _process_boundary(script, monkeypatch, repository)
    assert (
        script.main(
            [
                "--repository",
                str(repository),
                "--output-root",
                str(output),
                "--combination",
                "public-client",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["code"] == "output_root_refused"
    assert commands == []
    if unsafe == "nonempty":
        assert (output / "retained").read_text() == "existing evidence"


def _probe_namespace(script):
    tree = ast.parse(script.PROBE_SOURCE)
    tree.body = [
        node
        for node in tree.body
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Assign,
                ast.ClassDef,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
            ),
        )
    ]
    namespace = {}
    exec(compile(tree, "owned-installation-probe", "exec"), namespace)  # noqa: S102 - owned script definitions only
    return namespace


@pytest.mark.parametrize(
    "kind,name",
    [
        ("module", "sidera_control_plane"),
        ("module", "sidera_product"),
        ("module", "sidera_main_agent_gateway"),
        ("module", "akashic_sdk"),
        ("module", "sqlalchemy"),
        ("module", "alembic"),
        ("module", "psycopg"),
        ("module", "openai"),
        ("module", "anthropic"),
        ("distribution", "sidera-control-plane"),
        ("distribution", "sidera-product"),
        ("distribution", "sidera-main-agent-gateway"),
        ("distribution", "akashic-agent-sdk"),
        ("distribution", "sqlalchemy"),
        ("distribution", "alembic"),
        ("distribution", "psycopg"),
        ("distribution", "openai"),
        ("distribution", "anthropic"),
    ],
)
def test_agents_probe_rejects_forbidden_optional_or_vendor_dependencies(
    script, tmp_path, monkeypatch, kind, name
):
    namespace = _probe_namespace(script)
    artifact = tmp_path / "sidera_agents-0.1.0-py3-none-any.whl"
    artifact.write_bytes(b"unit-artifact")
    monkeypatch.setattr(
        namespace["sys"],
        "argv",
        [
            "probe",
            "agents-core",
            json.dumps({"sidera-agents": str(artifact)}),
            "",
        ],
    )
    namespace["_module_available"] = lambda module: False
    namespace["wheel_record_matches"] = lambda wheel, root: True
    namespace["_probe_agents_core"] = lambda: None
    monkeypatch.setattr(namespace["metadata"], "distributions", list)
    monkeypatch.setattr(
        namespace["metadata"],
        "distribution",
        lambda package: SimpleNamespace(
            version="0.1.0",
            locate_file=lambda relative: tmp_path,
        ),
    )
    assert set(namespace["_run_probe"]()) == {"sidera-agents"}
    if kind == "module":
        namespace["_module_available"] = lambda module: module == name
    else:
        monkeypatch.setattr(
            namespace["metadata"],
            "distributions",
            lambda: [
                SimpleNamespace(metadata={"Name": name}),
            ],
        )
    with pytest.raises(namespace["ProbeFailure"]):
        namespace["_run_probe"]()


def test_agents_commands_use_only_core_wheel_offline_and_isolated_imports(
    script, tmp_path
):
    build = script.build_argv(
        tmp_path / "repository", "sidera-agents", tmp_path / "evidence"
    )
    install = script.install_argv("agents-core", tmp_path / "evidence")
    assert "--offline" in build and "--offline" in install
    assert install[-1] == "sidera-agents==0.1.0"
    assert "--editable" not in install
    environment = script.subprocess_environment(tmp_path / "evidence")
    assert "PYTHONPATH" not in environment
    assert "DATABASE_URL" not in environment
    assert environment["UV_NO_CONFIG"] == "1"


def test_agents_offline_subprocess_uses_existing_cache_without_ambient_configuration(
    script, tmp_path, monkeypatch
):
    environments = []

    def run(argv, **kwargs):
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setenv("UV_CACHE_DIR", "/not-an-authorized-cache")
    monkeypatch.setenv("PYTHONPATH", "/not-an-authorized-source")
    monkeypatch.setenv("DATABASE_URL", "not-an-authorized-database")
    output = tmp_path / "evidence"
    script._run(["uv", "build"], output, 30, "build_failed", "agents-core")
    script._run(["uv", "build"], output, 30, "build_failed", "public-client")
    assert "UV_CACHE_DIR" not in environments[0]
    assert environments[1]["UV_CACHE_DIR"] == str(output / "uv-cache")
    for environment in environments:
        assert environment["UV_NO_CONFIG"] == "1"
        assert "PYTHONPATH" not in environment and "DATABASE_URL" not in environment


def _agents_consumer_probe(script, tmp_path, monkeypatch):
    namespace = _probe_namespace(script)
    installed = tmp_path / "venv" / "lib" / "site-packages"
    package = installed / "sidera_agents"
    package.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "packages" / "agents"
    shutil.copyfile(source / "alembic.ini", package / "alembic.ini")
    shutil.copytree(source / "migrations", package / "migrations")
    module_paths = {
        name: SimpleNamespace(__file__=str(package / f"{name.rsplit('.', 1)[-1]}.py"))
        for name in (
            "sidera_agents",
            "sidera_agents.api",
            "sidera_agents.bootstrap",
            "sidera_agents.contracts",
            "sidera_agents.errors",
            "sidera_agents.ports",
        )
    }
    monkeypatch.setattr(
        namespace["importlib"], "import_module", module_paths.__getitem__
    )
    monkeypatch.setattr(
        namespace["metadata"],
        "distribution",
        lambda name: SimpleNamespace(
            locate_file=lambda relative: installed,
        ),
    )
    summary = {
        "harness_id": "fixture-harness",
        "state": "completed",
        "messages": ["Fixture response."],
        "closed": True,
        "profile_status": {"enabled": False, "version": 2},
        "resource_status": {"enabled": False, "version": 2},
    }
    namespace["sys"] = SimpleNamespace(
        prefix=str(tmp_path / "venv"),
        modules={},
        argv=["probe", "agents-core", "{}", f"def main(): return {summary!r}\n"],
    )
    namespace["files"] = lambda name: package
    probe = namespace.get("_probe_agents_core")
    assert callable(probe), "The installed public-only agents consumer is missing"
    return namespace, probe, package, module_paths, summary


def test_agents_consumer_probe_accepts_owned_installed_resources_and_receipts(
    script, tmp_path, monkeypatch
):
    _, probe, _, _, _ = _agents_consumer_probe(script, tmp_path, monkeypatch)
    probe()


@pytest.mark.parametrize(
    "invalid",
    [
        "outside_module",
        "outside_resources",
        "outside_script_location",
        "missing_migration",
        "loaded_postgres",
        "loaded_native_adapter",
        "loaded_native_config",
        "loaded_native_descriptor",
        "loaded_native_transport",
        "incomplete_run",
        "unconfirmed_close",
        "missing_message",
        "stale_status",
    ],
)
def test_agents_consumer_probe_rejects_invalid_installation_or_public_evidence(
    script, tmp_path, monkeypatch, invalid
):
    namespace, probe, package, modules, summary = _agents_consumer_probe(
        script, tmp_path, monkeypatch
    )
    if invalid == "outside_module":
        modules["sidera_agents.api"].__file__ = str(tmp_path / "source" / "api.py")
    elif invalid == "outside_resources":
        namespace["files"] = lambda name: tmp_path / "source"
    elif invalid == "outside_script_location":
        (package / "alembic.ini").write_text(
            "[alembic]\nscript_location = %(here)s/../../source/migrations\n"
        )
    elif invalid == "missing_migration":
        (package / "migrations" / "versions" / "0004_inputs_events.py").unlink()
    elif invalid == "loaded_postgres":
        namespace["sys"].modules["sidera_agents.postgres.store"] = object()
    elif invalid == "loaded_native_adapter":
        namespace["sys"].modules["sidera_agents.harnesses.opencode"] = object()
    elif invalid == "loaded_native_config":
        namespace["sys"].modules["sidera_agents.harnesses.opencode_config"] = object()
    elif invalid == "loaded_native_descriptor":
        namespace["sys"].modules["sidera_agents.harnesses.opencode_descriptor"] = (
            object()
        )
    elif invalid == "loaded_native_transport":
        namespace["sys"].modules["sidera_agents.harnesses.acp_transport"] = object()
    else:
        if invalid == "incomplete_run":
            summary["state"] = "running"
        elif invalid == "unconfirmed_close":
            summary["closed"] = False
        elif invalid == "missing_message":
            summary["messages"] = []
        else:
            summary["profile_status"] = {"enabled": True, "version": 1}
        namespace["sys"].argv[3] = f"def main(): return {summary!r}\n"
    with pytest.raises(namespace["ProbeFailure"]):
        probe()


@pytest.mark.parametrize("combination", ("control-plane-core", "control-plane-agents"))
@pytest.mark.parametrize("failed", (False, True))
def test_native_cp_selection_has_exact_required_closure_without_other_projects(
    script,
    tmp_path,
    monkeypatch,
    capsys,
    combination,
    failed,
):
    expected = ("sidera-control-plane", "sidera-agents", "sidera-runtime-guard")
    repository = _repository(tmp_path, expected)
    output = tmp_path / "evidence"
    calls = []

    def run(argv, output_root, timeout, code, name=None):
        calls.append(list(argv))
        if argv[:2] == ["uv", "build"]:
            project = next(
                package
                for package in expected
                if Path(argv[2]) == repository / PROJECT_PATHS[package]
            )
            wheel_directory = output_root / "wheels"
            wheel_directory.mkdir(exist_ok=True)
            (
                wheel_directory / f"{project.replace('-', '_')}-0.1.0-py3-none-any.whl"
            ).write_bytes(b"controlled wheel boundary")
        if "-I" in argv:
            return subprocess.CompletedProcess(
                argv,
                int(failed),
                json.dumps(
                    {
                        "status": "FAIL" if failed else "PASS",
                        "code": "dependency_boundary_failed" if failed else None,
                        "packages": {
                            package: {"version": "0.1.0", "record_match": not failed}
                            for package in expected
                        },
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(script, "_run", run)
    assert script.main(
        [
            "--repository",
            str(repository),
            "--output-root",
            str(output),
            "--combination",
            combination,
        ]
    ) == int(failed)
    report = json.loads(capsys.readouterr().out)
    selected = report["combinations"][combination]
    assert selected["status"] == ("FAIL" if failed else "PASS")
    assert (
        set(selected["source_fingerprint"])
        == set(selected["packages"])
        == set(expected)
    )
    assert all(
        item == {"status": "NOT_RUN"}
        for name, item in report["combinations"].items()
        if name != combination
    )
    assert [
        Path(argv[2]).relative_to(repository).as_posix()
        for argv in calls
        if argv[:2] == ["uv", "build"]
    ] == [PROJECT_PATHS[package] for package in expected]
    probes = [argv for argv in calls if "-I" in argv]
    assert len(probes) == 1 and set(json.loads(probes[0][5])) == set(expected)
    assert not any("--editable" in argv for argv in calls)
    assert report["provider_calls"] == 0


def test_native_cp_probe_uses_public_inert_composition_and_packaged_resources(script):
    namespace = _probe_namespace(script)
    probe = namespace.get("_probe_control_plane_agents")
    assert callable(probe), "Native CP public installed probe is missing"
    project_map = namespace["PROJECTS"]
    expected = ("sidera-control-plane", "sidera-agents", "sidera-runtime-guard")
    assert (
        project_map["control-plane-core"]
        == project_map["control-plane-agents"]
        == expected
    )
    tree = ast.parse(script.PROBE_SOURCE)
    function = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_probe_control_plane_agents"
    )
    imported = {
        item.module for item in ast.walk(function) if isinstance(item, ast.ImportFrom)
    }
    assert "sidera_control_plane.composition.native_runtime" in imported
    assert not any(
        name and (".persistence" in name or ".harnesses" in name or ".adapters" in name)
        for name in imported
    )


@pytest.mark.parametrize("combination", CLOSURES)
def test_probe_argv_supplies_public_consumer_only_for_agents_and_native_product(
    script, tmp_path, combination
):
    consumes_example = combination in {"agents-core", "native-product"}
    repository = _repository(tmp_path, CLOSURES[combination])
    wheels = {package: tmp_path / f"{package}.whl" for package in PROJECT_PATHS}
    argv = script._probe_argv(
        combination,
        wheels,
        tmp_path / "evidence",
        repository if consumes_example else None,
    )
    assert argv[1:3] == ["-I", "-c"]
    assert argv[4] == combination
    assert json.loads(argv[5]) == {
        package: str(wheels[package]) for package in CLOSURES[combination]
    }
    assert len(argv) == (7 if consumes_example else 6)
    if consumes_example:
        assert (
            argv[6]
            == (repository / "packages/agents/examples/standalone.py").read_text()
        )


@pytest.mark.parametrize("combination", ("agents-core", "native-product"))
def test_consumer_probe_argv_requires_explicit_repository(
    script, tmp_path, combination
):
    wheels = {package: tmp_path / f"{package}.whl" for package in PROJECT_PATHS}
    with pytest.raises(script.CheckFailure, match="invalid_repository"):
        script._probe_argv(combination, wheels, tmp_path / "evidence")


def _native_product_dispatch(script, tmp_path, monkeypatch):
    namespace = _probe_namespace(script)
    wheels = {}
    for package in CLOSURES["native-product"]:
        artifact = tmp_path / f"{package}-0.1.0.whl"
        artifact.write_bytes(b"controlled native-product wheel boundary")
        wheels[package] = str(artifact)
    namespace["sys"] = SimpleNamespace(
        argv=["probe", "native-product", json.dumps(wheels), "public consumer source"]
    )
    namespace["_module_available"] = lambda module: False
    namespace["wheel_record_matches"] = lambda wheel, root: True
    monkeypatch.setattr(namespace["metadata"], "distributions", list)
    monkeypatch.setattr(
        namespace["metadata"],
        "distribution",
        lambda package: SimpleNamespace(
            version="0.1.0", locate_file=lambda relative: tmp_path
        ),
    )
    called = []

    async def smoke():
        called.append("native-product")

    namespace["_probe_native_product"] = smoke
    return namespace, called


def test_native_product_dispatch_verifies_all_records_then_runs_async_smoke(
    script, tmp_path, monkeypatch
):
    namespace, called = _native_product_dispatch(script, tmp_path, monkeypatch)
    packages = namespace["_run_probe"]()
    assert called == ["native-product"]
    assert set(packages) == set(CLOSURES["native-product"])
    assert all(
        row["version"] == "0.1.0" and row["record_match"] is True
        for row in packages.values()
    )


@pytest.mark.parametrize("package", CLOSURES["native-product"])
def test_native_product_dispatch_rejects_any_failed_wheel_record_before_smoke(
    script, tmp_path, monkeypatch, package
):
    namespace, called = _native_product_dispatch(script, tmp_path, monkeypatch)
    namespace["wheel_record_matches"] = lambda wheel, root: (
        Path(wheel).name != f"{package}-0.1.0.whl"
    )
    with pytest.raises(namespace["ProbeFailure"]) as failure:
        namespace["_run_probe"]()
    assert failure.value.code == "dependency_boundary_failed"
    assert failure.value.packages[package]["record_match"] is False
    assert called == []


@pytest.mark.parametrize(
    "kind,name",
    [
        ("module", "akashic_sdk"),
        ("module", "sidera_task_bridge"),
        ("module", "sidera_main_agent_gateway"),
        ("distribution", "akashic-agent-sdk"),
        ("distribution", "sidera-akashic-task-bridge"),
        ("distribution", "sidera-main-agent-gateway"),
    ],
)
def test_native_product_dispatch_rejects_legacy_modules_and_distributions(
    script, tmp_path, monkeypatch, kind, name
):
    namespace, called = _native_product_dispatch(script, tmp_path, monkeypatch)
    if kind == "module":
        namespace["_module_available"] = lambda module: module == name
    else:
        monkeypatch.setattr(
            namespace["metadata"],
            "distributions",
            lambda: [SimpleNamespace(metadata={"Name": name.replace("-", "_")})],
        )
    with pytest.raises(namespace["ProbeFailure"]):
        namespace["_run_probe"]()
    assert called == []


def test_native_product_probe_uses_installed_resources_and_actual_public_interfaces(
    script,
):
    namespace = _probe_namespace(script)
    assert callable(namespace.get("_probe_native_product")), (
        "Native product smoke is missing"
    )
    assert namespace["PROJECTS"] == CLOSURES == script.COMBINATION_PROJECTS
    assert script.PROJECTS == PROJECT_PATHS
    assert (
        namespace["ABSENT_MODULES"]["native-product"]
        == script.ABSENT_MODULES["native-product"]
        == ("akashic_sdk", "sidera_task_bridge", "sidera_main_agent_gateway")
    )
    assert (
        namespace["ABSENT_DISTRIBUTIONS"]["native-product"]
        == script.ABSENT_DISTRIBUTIONS["native-product"]
        == (
            "akashic-agent-sdk",
            "sidera-akashic-task-bridge",
            "sidera-main-agent-gateway",
        )
    )
    function = next(
        node
        for node in ast.parse(script.PROBE_SOURCE).body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_probe_native_product"
    )
    imported = {
        (node.module, alias.name)
        for node in ast.walk(function)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert {
        ("sidera_product.app", "create_app"),
        ("sidera_product.composition", "build_product_runtime"),
        ("sidera_product.config", "ProductSettings"),
        ("sidera_product", "cli"),
    } <= imported
    constants = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {
        "sidera_product",
        "sidera_product.app",
        "sidera_product.composition",
        "sidera_product.config",
        "sidera_product.cli",
        "sidera_agents",
        "sidera_agents.api",
        "sidera_agents.contracts",
        "sidera_agents.ports",
        "sidera_control_plane",
        "sidera_control_plane.composition.native_runtime",
        "sidera_control_plane_client",
        "sidera_model_connections",
        "sidera_model_connections.contracts",
        "sidera_model_connections.postgres",
        "sidera_runtime_guard",
        "alembic.ini",
        "script_location",
        "env.py",
        "*.py",
        "sidera_product.cli:main",
        "console_scripts",
        "sidera",
    } <= constants
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    call_names = {ast.unparse(node.func) for node in calls}
    assert {
        "files",
        "ConfigParser",
        "metadata.distribution",
        "build_product_runtime",
        "create_app",
    } <= call_names
    assert any(
        ast.unparse(node.func) == "ConfigParser"
        and any(keyword.arg == "defaults" for keyword in node.keywords)
        for node in calls
    )
    assert (
        sum(
            isinstance(node.func, ast.Attribute) and node.func.attr == "is_relative_to"
            for node in calls
        )
        >= 4
    )


def test_native_product_probe_keeps_inert_guards_around_runtime_and_public_consumer(
    script,
):
    function = next(
        (
            node
            for node in ast.parse(script.PROBE_SOURCE).body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_probe_native_product"
        ),
        None,
    )
    assert function is not None, "Native product smoke is missing"
    guard = next(
        (
            node
            for node in ast.walk(function)
            if isinstance(node, ast.With)
            and {
                (
                    ast.unparse(item.context_expr.args[0]),
                    item.context_expr.args[1].value,
                )
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and ast.unparse(item.context_expr.func) == "patch.object"
            }
            >= {("Engine", "connect"), ("subprocess", "Popen")}
        ),
        None,
    )
    assert guard is not None, "DB and native process guards must surround the smoke"
    calls = [node for node in ast.walk(guard) if isinstance(node, ast.Call)]
    call_names = {ast.unparse(node.func) for node in calls}
    assert {
        "build_product_runtime",
        "create_app",
        "httpx.AsyncClient",
        "httpx.ASGITransport",
        "cli.main",
        "exec",
        "runtime.close",
    } <= call_names
    assert not any(
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"start", "start_background", "bootstrap", "migrate"}
        for node in calls
    )
    assert any(
        ast.unparse(node.func) == "runtime.close"
        and [
            (keyword.arg, ast.literal_eval(keyword.value)) for keyword in node.keywords
        ]
        == [("grace_seconds", 0)]
        for node in calls
    )
    assert (
        sum(
            isinstance(node, ast.Attribute) and node.attr == "modules"
            for node in ast.walk(function)
        )
        >= 2
    )
