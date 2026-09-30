from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.database import (
    DemoDatabaseError,
    assert_database_ownership,
    database_url_for,
    validate_database_url,
)
from sidera_control_plane.demo.test_runner import (
    DemoTestIsolationError,
    bootstrap_test_command,
    build_interview_test_environment,
    interview_test_files,
    validate_interview_test_context,
)


def _manifest(tmp_path: Path) -> DemoManifest:
    run_id = uuid4()
    run_root = tmp_path / "run"
    repository = run_root / "repository"
    repository.mkdir(parents=True)
    return DemoManifest(
        run_id=run_id,
        workspace_id=uuid4(),
        scenario="mainline",
        run_root=run_root,
        repository=repository,
        base_commit="a" * 40,
        code_commit="b" * 40,
        database_name=f"sidera_interview_test_{run_id.hex}",
        knowledge_database_name=f"sidera_interview_knowledge_{run_id.hex}",
        endpoints={
            "control_plane": "http://127.0.0.1:15432",
            "knowledge": "http://127.0.0.1:15433",
            "main_agent": "http://127.0.0.1:15434",
            "web": "http://127.0.0.1:15435",
        },
        module_ids={key: uuid4() for key in (
            "worker_primary", "worker_secondary", "workspace", "planner",
            "collaboration", "verification", "notification",
        )},
        worker_ids={key: uuid4() for key in (
            "validation", "report", "review", "integration", "replacement",
            "verification",
        )},
        planning_profile_id=uuid4(),
    )


def test_database_url_is_bound_to_the_manifest_target(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = database_url_for(manifest)

    assert validate_database_url(manifest, target).database_name == manifest.database_name

    for invalid in (
        target.replace("127.0.0.1", "localhost"),
        target.replace(":55432/", ":55431/"),
        target.replace(manifest.database_name, "sidera_interview_test_other"),
        "sqlite:////tmp/interview.sqlite",
    ):
        with pytest.raises(DemoDatabaseError):
            validate_database_url(manifest, invalid)


def test_same_database_name_with_a_different_marker_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(DemoDatabaseError, match="ownership"):
        assert_database_ownership(
            manifest,
            manifest.database_name,
            "sidera-interview-demo:another-run",
        )

    assert_database_ownership(
        manifest,
        manifest.database_name,
        f"sidera-interview-demo:{manifest.run_id}",
    )


def test_database_owner_must_remain_the_runtime_role(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    marker = f"sidera-interview-demo:{manifest.run_id}"

    with pytest.raises(DemoDatabaseError, match="owner"):
        assert_database_ownership(
            manifest, manifest.database_name, marker, owner="aitejiu"
        )

    assert_database_ownership(
        manifest, manifest.database_name, marker, owner=manifest.pg_user
    )


def test_bootstrap_runner_has_a_fixed_child_test_command() -> None:
    command = bootstrap_test_command()

    assert command[:3] == ("pytest", "tests/integration/test_interview_bootstrap.py", "-q")
    assert all(";" not in item and "&&" not in item for item in command)


def test_interview_test_discovery_uses_only_matching_python_files(tmp_path: Path) -> None:
    integration = tmp_path / "tests" / "integration"
    integration.mkdir(parents=True)
    (integration / "test_interview_first.py").touch()
    (integration / "test_interview_second.py").touch()
    (integration / "test_unrelated.py").touch()

    files = interview_test_files(tmp_path)

    assert [path.name for path in files] == [
        "test_interview_first.py",
        "test_interview_second.py",
    ]


def test_interview_test_environment_strips_model_and_proxy_credentials(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    passfile = tmp_path / "pgpass"
    passfile.write_text("local-only", encoding="utf-8")

    environment = build_interview_test_environment(
        manifest,
        inherited={
            "PATH": "/usr/bin",
            "DEEPSEEK_API_KEY": "do-not-copy",
            "HTTPS_PROXY": "http://proxy.invalid",
            "SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL": (
                "postgresql+psycopg://sidera@127.0.0.1:55432/postgres"
            ),
        },
        pgpassfile=passfile,
    )

    assert environment["SIDERA_DATABASE_URL"] == database_url_for(manifest)
    assert environment["SIDERA_INTERVIEW_DEMO_TEST_RUN"] == "1"
    assert environment["SIDERA_INTERVIEW_DEMO_MANIFEST"] == str(
        manifest.run_root / "manifest.json"
    )
    assert environment["PGPASSFILE"] == str(passfile)
    assert environment["SIDERA_INTERVIEW_DEMO_PGPASSFILE"] == str(passfile)
    assert "DEEPSEEK_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment


def test_interview_test_collection_refuses_without_child_run_marker() -> None:
    with pytest.raises(DemoTestIsolationError, match="child_run_required"):
        validate_interview_test_context({})


def test_pytest_collection_hook_blocks_interview_tests_without_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import conftest as integration_conftest

    monkeypatch.delenv("SIDERA_INTERVIEW_DEMO_TEST_RUN", raising=False)
    item = SimpleNamespace(path=Path("tests/integration/test_interview_unsafe.py"))

    with pytest.raises(pytest.UsageError, match="interview_test_database_refused"):
        integration_conftest.pytest_collection_modifyitems(None, None, [item])


def test_interview_test_collection_rejects_a_database_outside_its_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sidera_control_plane.demo import test_runner

    root = tmp_path / "sidera"
    root.mkdir()
    manifest = _manifest(tmp_path)
    run_root = root / ".sidera" / "interview-demo" / str(manifest.run_id)
    run_root.mkdir(parents=True)
    repository = tmp_path / "external-repository"
    repository.mkdir()
    manifest = manifest.model_copy(
        update={"run_root": run_root, "repository": repository}
    )
    path = run_root / "manifest.json"
    path.write_text(manifest.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(test_runner, "_PROJECT_ROOT", root)

    with pytest.raises(DemoTestIsolationError, match="database_target_mismatch"):
        validate_interview_test_context(
            {
                "SIDERA_INTERVIEW_DEMO_TEST_RUN": "1",
                "SIDERA_INTERVIEW_DEMO_MANIFEST": str(path),
                "SIDERA_DATABASE_URL": (
                    "postgresql+psycopg://sidera@127.0.0.1:55432/"
                    "sidera_interview_test_00000000000000000000000000000000"
                ),
            }
        )
