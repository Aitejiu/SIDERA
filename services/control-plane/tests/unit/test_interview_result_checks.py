from __future__ import annotations

from uuid import uuid4

import pytest

from sidera_control_plane.adapters.demo_planner import build_initial_proposal
from sidera_control_plane.demo.bootstrap import _resources
from sidera_control_plane.demo.results import (
    DemoResultError,
    closure_check_argv,
    closure_check_spec,
    demo_check_argv,
    demo_check_spec,
    restore_demo_candidate,
)
from sidera_control_plane.demo.sample_repository import (
    create_sample_repository,
    materialize_sample_stage,
)
from tests.unit.interview_demo_support import manifest_for


def test_demo_check_spec_is_shared_by_bootstrap_and_planner(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    resources = _resources(manifest)
    configured = resources.instances["verification"].config["verification.check_specs"]
    planned = build_initial_proposal(manifest).integration_spec.checks
    expected = [
        demo_check_spec().to_mapping(),
        closure_check_spec().to_mapping(),
    ]

    assert configured == expected
    assert len(planned) == 1
    assert planned[0].to_mapping() == expected[0]


def test_demo_check_uses_the_fixed_python_unittest_suite() -> None:
    check = demo_check_spec()

    assert demo_check_argv() == (
        "python3",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
    )
    assert check.argv == demo_check_argv()
    assert check.timeout_seconds == 60
    assert check.success_exit_codes == (0,)
    assert check.required is True


def test_closure_check_uses_the_control_plane_checker_in_the_candidate_cwd() -> None:
    check = closure_check_spec()

    assert closure_check_argv()[-1] == "--repository-cwd"
    assert check.key == "closure"
    assert check.argv == closure_check_argv()
    assert check.environment_ref == "env://interview-demo"
    assert check.timeout_seconds == 60
    assert check.success_exit_codes == (0,)
    assert check.required is True


def test_closure_proposal_binds_the_closure_checker(tmp_path) -> None:
    from sidera_control_plane.adapters.demo_planner import build_closure_sample_proposal

    proposal = build_closure_sample_proposal(manifest_for(tmp_path))

    assert tuple(check.key for check in proposal.integration_spec.checks) == ("closure",)
    assert proposal.integration_spec.checks[0].to_mapping() == closure_check_spec().to_mapping()


def test_demo_check_runs_against_the_actual_sample_repository(tmp_path) -> None:
    import subprocess

    repository, _ = create_sample_repository(tmp_path)
    materialize_sample_stage(repository, "validation", phone_optional=True)
    materialize_sample_stage(repository, "report", phone_optional=True)
    materialize_sample_stage(repository, "integration", phone_optional=True)
    completed = subprocess.run(
        demo_check_argv(),
        cwd=repository,
        capture_output=True,
        check=False,
        timeout=60,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_restore_rejects_a_destination_outside_the_demo_run(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir(parents=True)
    destination = tmp_path / "outside-run"
    destination.mkdir()

    with pytest.raises(DemoResultError, match="restore_destination_outside_run"):
        restore_demo_candidate(manifest, uuid4(), destination)


def test_restore_rejects_a_non_empty_destination_before_database_access(
    tmp_path, monkeypatch
) -> None:
    manifest = manifest_for(tmp_path)
    manifest.run_root.mkdir(parents=True)
    destination = manifest.run_root / "restore"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")

    def database_must_not_be_opened(*_args, **_kwargs):
        raise AssertionError("database access happened before destination validation")

    monkeypatch.setattr(
        "sidera_control_plane.demo.results.Database", database_must_not_be_opened
    )

    with pytest.raises(DemoResultError, match="restore_destination_not_empty"):
        restore_demo_candidate(manifest, uuid4(), destination)
