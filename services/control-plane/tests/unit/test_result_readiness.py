from pathlib import Path

from sidera_control_plane.config import Settings
from sidera_control_plane.result_readiness import inspect_result_readiness


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://sidera@127.0.0.1:55432/sidera_test",
        "result_integration_enabled": True,
        "result_repository_ref": "repo://integration",
        "result_environment_ref": "env://python",
        "result_store_root": tmp_path / "results",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _catalog(*, include_input: bool = True) -> tuple[dict[str, object], ...]:
    workspace_capabilities: dict[str, object] = {
        "isolation": "isolated-runtime",
        "writable": True,
        "git_worktree": True,
        "stability": "stable",
    }
    if include_input:
        workspace_capabilities["code_input_versions"] = [1]
    return (
        {
            "module_type": "worker_adapter",
            "lifecycle": "active",
            "capabilities": {
                "code_result_versions": [1],
                "verification_versions": [1],
            },
        },
        {
            "module_type": "workspace_provider",
            "lifecycle": "active",
            "capabilities": workspace_capabilities,
        },
    )


def _resolver(tmp_path: Path, *, base_commit: str = "a" * 40) -> dict[str, object]:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    return {
        "repositories": {
            "repo://integration": {
                "path": repository,
                "base_commit": base_commit,
                "git_objects": [base_commit],
            }
        },
        "environments": {"env://python": {"version": 1}},
        "checks": (
            {
                "key": "unit",
                "argv": ["python3", "-m", "unittest"],
                "cwd": ".",
                "environment_ref": "env://python",
                "timeout_seconds": 60,
            },
        ),
    }


def test_disabled_readiness_is_side_effect_free(tmp_path: Path) -> None:
    calls: list[str] = []

    def resolver(ref: str) -> object:
        calls.append(ref)
        return None

    report = inspect_result_readiness(
        _settings(tmp_path, result_integration_enabled=False),
        _catalog(),
        resolver,
    )

    assert report.enabled is False
    assert report.ready is False
    assert report.checks == ()
    assert report.missing_capabilities == ()
    assert calls == []


def test_readiness_reports_missing_capability_without_substitution(tmp_path: Path) -> None:
    report = inspect_result_readiness(
        _settings(tmp_path),
        _catalog(include_input=False),
        _resolver(tmp_path),
    )

    assert report.enabled is True
    assert report.ready is False
    assert "code_input_versions" in report.missing_capabilities
    assert report.check("capabilities").status == "missing"


def test_readiness_reports_missing_git_object_and_does_not_create_one(
    tmp_path: Path,
) -> None:
    resolver = _resolver(tmp_path, base_commit="b" * 40)
    resolver["repositories"] = {
        "repo://integration": {
            "path": tmp_path / "repository",
            "base_commit": "b" * 40,
            "git_objects": [],
        }
    }
    repository = tmp_path / "repository"
    before = sorted(repository.rglob("*"))

    report = inspect_result_readiness(_settings(tmp_path), _catalog(), resolver)

    assert report.ready is False
    assert report.check("repository").code == "repository_git_object_missing"
    assert sorted(repository.rglob("*")) == before


def test_readiness_rejects_unsafe_check_contract(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path)
    resolver["checks"] = (
        {
            "key": "unsafe",
            "argv": ["python3", "-c 'network()'"],
            "cwd": "../outside",
            "environment_ref": "env://python",
        },
    )

    report = inspect_result_readiness(_settings(tmp_path), _catalog(), resolver)

    assert report.ready is False
    assert report.check("checks").code == "check_spec_invalid"


def test_readiness_rejects_unwritable_result_store(tmp_path: Path) -> None:
    store = tmp_path / "missing" / "results"

    report = inspect_result_readiness(
        _settings(tmp_path, result_store_root=store),
        _catalog(),
        _resolver(tmp_path),
    )

    assert report.ready is False
    assert report.check("result_store").code == "result_store_unwritable"


def test_complete_readiness_is_true_for_declared_local_setup(tmp_path: Path) -> None:
    report = inspect_result_readiness(
        _settings(tmp_path),
        _catalog(),
        _resolver(tmp_path),
    )

    assert report.ready is True
    assert all(check.status == "ok" for check in report.checks)
    payload = report.as_dict()
    assert payload["missing_capabilities"] == []
    assert str(tmp_path) not in str(payload)
