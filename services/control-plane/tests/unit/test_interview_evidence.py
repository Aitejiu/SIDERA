import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.demo.checks import (
    action_catalog_ids,
    build_report,
    register_child_run,
)
from sidera_control_plane.demo.evidence import CheckResult, evaluate_readiness
from tests.unit.interview_demo_support import manifest_for


def _evidence(tmp_path: Path, name: str = "proof.json") -> str:
    path = tmp_path / name
    path.write_text("{}", encoding="utf-8")
    return str(path)


def test_missing_checks_never_produce_ready() -> None:
    assert evaluate_readiness((), ("M1", "M2", "M3", "M4", "M5", "M6")) == "NOT_READY"


def test_all_overall_checks_with_real_files_are_mainline_ready(tmp_path: Path) -> None:
    results = tuple(
        CheckResult(
            id=check_id,
            status="PASS",
            evidence_kind="real_local_verification",
            evidence_paths=(_evidence(tmp_path, f"{check_id}.json"),),
        )
        for check_id in ("M1", "M2", "M3", "M4", "M5", "M6")
    )

    assert evaluate_readiness(results, ("M1", "M2", "M3", "M4", "M5", "M6")) == "MAINLINE_READY"


def test_failed_duplicate_or_skipped_observation_is_not_ready(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    passed = CheckResult("M1", "PASS", "simulated_interview", (path,))
    failed = CheckResult("M1", "FAIL", "simulated_interview", (), "assertion_failed")

    assert evaluate_readiness((passed, failed), ("M1",)) == "NOT_READY"
    assert evaluate_readiness(
        (CheckResult("M1", "INCOMPLETE", "simulated_interview", (path,)),),
        ("M1",),
    ) == "NOT_READY"


def test_passing_check_requires_a_real_evidence_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="passing_check_requires_evidence"):
        CheckResult("M1", "PASS", "simulated_interview")
    missing = str(tmp_path / "missing.json")
    result = CheckResult("M1", "PASS", "simulated_interview", (missing,))
    assert evaluate_readiness((result,), ("M1",)) == "NOT_READY"


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return str(path)


def _action_report(manifest, evidence_path: str, *, actions: list[dict[str, object]] | None = None) -> dict[str, object]:
    catalog_path = Path(__file__).parents[4] / "apps" / "web" / "tests" / "interview" / "action-catalog.json"
    return {
        "schema_version": 1,
        "run_id": str(manifest.run_id),
        "scenario": manifest.scenario,
        "code_commit": manifest.code_commit,
        "catalog_fingerprint": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        "actions": actions if actions is not None else [
            {
                "id": action_id,
                "status": "PASS",
                "test_id": "interview.test",
                "evidence_paths": [evidence_path],
                "failure_code": None,
            }
            for action_id in action_catalog_ids()
        ],
    }


def _overall_report(manifest, evidence_path: str, **overrides: object) -> dict[str, object]:
    checks = [
        {
            "id": check_id,
            "status": "PASS",
            "evidence_kind": "real_local_verification",
            "evidence_paths": [evidence_path],
            "failure_code": None,
        }
        for check_id in ("M1", "M2", "M3", "M4", "M5", "M6")
    ]
    override_checks = overrides.pop("checks", [])
    by_id = {item["id"]: item for item in checks}
    by_id.update({item["id"]: item for item in override_checks})  # type: ignore[index]
    checks = [by_id[check_id] for check_id in ("M1", "M2", "M3", "M4", "M5", "M6")]
    return {
        "schema_version": 1,
        "run_id": str(manifest.run_id),
        "scenario": manifest.scenario,
        "code_commit": manifest.code_commit,
        "checks": checks,
        **overrides,
    }


def _write_source_receipts(manifest, root: Path) -> None:
    reports = root / "reports"
    content_path = reports / "source-content.txt"
    content_path.parent.mkdir(parents=True, exist_ok=True)
    content = "verified artifact\n"
    content_path.write_text(content, encoding="utf-8")
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    candidate_hash = "a" * 64
    _write_json(
        reports / "control-plane-query.json",
        {
            "schema_version": 1,
            "run_id": str(manifest.run_id),
            "workspace_id": str(manifest.workspace_id),
            "code_commit": manifest.code_commit,
            "candidate_hash": candidate_hash,
        },
    )
    _write_json(
        reports / "artifact-hash.json",
        {
            "schema_version": 1,
            "run_id": str(manifest.run_id),
            "code_commit": manifest.code_commit,
            "candidate_hash": candidate_hash,
            "artifact_hash": content_hash,
            "content_sha256": content_hash,
            "content_path": str(content_path),
        },
    )
    _write_json(
        reports / "local-check-receipt.json",
        {
            "schema_version": 1,
            "run_id": str(manifest.run_id),
            "code_commit": manifest.code_commit,
            "candidate_hash": candidate_hash,
            "status": "passed",
            "exit_code": 0,
        },
    )
    safety_log = root / "logs" / "safety-audit.log"
    safety_log.parent.mkdir(parents=True, exist_ok=True)
    safety_log.write_text(
        f"run_id={manifest.run_id}\nstatus=passed\n",
        encoding="utf-8",
    )


def test_build_report_requires_an_explicit_overall_receipt(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    evidence_path = _write_json(manifest.run_root / "reports" / "browser-proof.json", {})
    _write_json(manifest.run_root / "reports" / "action-report.json", _action_report(manifest, evidence_path))

    report = build_report(manifest)

    assert report["readiness"] == "NOT_READY"
    overall = {item["id"]: item for item in report["checks"]}  # type: ignore[index]
    assert overall["M1"]["status"] == "NOT_RUN"
    assert overall["M1"]["failure_code"] == "overall_receipt_missing"


def test_build_report_does_not_silently_accept_duplicate_action_ids(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    evidence_path = _write_json(manifest.run_root / "reports" / "browser-proof.json", {})
    duplicate = {
        "id": "F01-01",
        "status": "PASS",
        "test_id": "interview.test",
        "evidence_paths": [evidence_path],
        "failure_code": None,
    }
    _write_json(
        manifest.run_root / "reports" / "action-report.json",
        _action_report(manifest, evidence_path, actions=[duplicate, duplicate]),
    )

    report = build_report(manifest)

    assert report["readiness"] == "NOT_READY"
    assert "action_id_duplicate:F01-01" in report["diagnostics"]


def test_duplicate_action_prefers_failure_while_staying_not_ready(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    evidence_path = _write_json(manifest.run_root / "reports" / "browser-proof.json", {})
    actions = [
        {
            "id": "F01-01",
            "status": "PASS",
            "test_id": "interview.pass",
            "evidence_paths": [evidence_path],
            "failure_code": None,
        },
        {
            "id": "F01-01",
            "status": "FAIL",
            "test_id": "interview.fail",
            "evidence_paths": [evidence_path],
            "failure_code": "assertion_failed",
        },
    ]
    _write_json(
        manifest.run_root / "reports" / "action-report.json",
        _action_report(manifest, evidence_path, actions=actions),
    )

    report = build_report(manifest)
    action = next(item for item in report["checks"] if item["id"] == "F01-01")  # type: ignore[index]

    assert action["status"] == "FAIL"
    assert action["failure_code"] == "assertion_failed"
    assert report["readiness"] == "NOT_READY"


def test_build_report_rejects_real_verification_label_on_browser_action(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    evidence_path = _write_json(manifest.run_root / "reports" / "browser-proof.json", {})
    actions = [
        {
            "id": "F01-01",
            "status": "PASS",
            "evidence_kind": "real_local_verification",
            "test_id": "interview.test",
            "evidence_paths": [evidence_path],
            "failure_code": None,
        }
    ]
    _write_json(
        manifest.run_root / "reports" / "action-report.json",
        _action_report(manifest, evidence_path, actions=actions),
    )

    report = build_report(manifest)
    action = next(item for item in report["checks"] if item["id"] == "F01-01")  # type: ignore[index]

    assert action["status"] == "INCOMPLETE"
    assert action["failure_code"] == "action_evidence_kind_invalid"


def test_build_report_rejects_candidate_hash_mismatch(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    evidence_path = _write_json(manifest.run_root / "reports" / "browser-proof.json", {})
    action_report = _action_report(manifest, evidence_path)
    _write_json(manifest.run_root / "reports" / "action-report.json", action_report)
    _write_json(
        manifest.run_root / "reports" / "overall-checks.json",
        _overall_report(
            manifest,
            evidence_path,
            checks=[
                {
                    "id": "M2",
                    "status": "PASS",
                    "evidence_kind": "real_local_verification",
                    "evidence_paths": [evidence_path],
                    "candidate_hash": "a" * 64,
                    "artifact_hash": "b" * 64,
                    "failure_code": None,
                }
            ],
        ),
    )

    report = build_report(manifest)
    check = next(item for item in report["checks"] if item["id"] == "M2")  # type: ignore[index]

    assert report["readiness"] == "NOT_READY"
    assert check["status"] == "INCOMPLETE"
    assert check["failure_code"] == "candidate_hash_mismatch"


def test_build_report_accepts_a_registered_child_evidence_root(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    child_id = uuid4()
    child_root = manifest.run_root.parent / str(child_id)
    child_root.mkdir(parents=True)
    child_manifest = manifest.model_copy(
        update={
            "run_id": child_id,
            "run_root": child_root,
            "database_name": f"sidera_interview_test_{child_id.hex}",
            "knowledge_database_name": f"sidera_interview_knowledge_{child_id.hex}",
        }
    )
    _write_json(child_root / "manifest.json", json.loads(child_manifest.model_dump_json()))
    evidence_path = _write_json(child_root / "reports" / "child-proof.json", {})
    _write_source_receipts(child_manifest, child_root)
    registry = {
        "schema_version": 1,
        "run_id": str(manifest.run_id),
        "children": [{"run_id": str(child_id), "run_root": str(child_root), "suite": "browser"}],
    }
    _write_json(manifest.run_root / "reports" / "child-runs.json", registry)
    _write_json(
        manifest.run_root / "reports" / "action-report.json",
        _action_report(manifest, evidence_path),
    )
    _write_json(
        manifest.run_root / "reports" / "overall-checks.json",
        _overall_report(manifest, evidence_path),
    )

    report = build_report(manifest)

    assert report["readiness"] == "INTERVIEW_DEMO_READY"
    assert str(child_root) in report["evidence_roots"]


def test_build_report_prefers_a_newer_observation_over_an_old_failure(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    _write_source_receipts(manifest, manifest.run_root)
    _write_json(
        manifest.run_root / "reports" / "overall-checks.json",
        _overall_report(manifest, _evidence(manifest.run_root, "reports/overall-proof.json")),
    )

    old_id = uuid4()
    old_root = manifest.run_root.parent / str(old_id)
    old_root.mkdir(parents=True)
    old_manifest = manifest.model_copy(
        update={
            "run_id": old_id,
            "run_root": old_root,
            "database_name": f"sidera_interview_test_{old_id.hex}",
            "knowledge_database_name": f"sidera_interview_knowledge_{old_id.hex}",
        }
    )
    _write_json(old_root / "manifest.json", json.loads(old_manifest.model_dump_json()))
    (old_root / "reports").mkdir(parents=True)
    old_evidence = _evidence(old_root, "reports/old-proof.json")
    old_actions = _action_report(old_manifest, old_evidence)["actions"]
    old_actions[0] = {
        **old_actions[0],
        "status": "FAIL",
        "failure_code": "old_failure",
    }
    _write_json(
        old_root / "reports" / "action-report.json",
        _action_report(old_manifest, old_evidence, actions=old_actions),
    )

    new_id = uuid4()
    new_root = manifest.run_root.parent / str(new_id)
    new_root.mkdir(parents=True)
    new_manifest = manifest.model_copy(
        update={
            "run_id": new_id,
            "run_root": new_root,
            "database_name": f"sidera_interview_test_{new_id.hex}",
            "knowledge_database_name": f"sidera_interview_knowledge_{new_id.hex}",
        }
    )
    _write_json(new_root / "manifest.json", json.loads(new_manifest.model_dump_json()))
    (new_root / "reports").mkdir(parents=True)
    new_evidence = _evidence(new_root, "reports/new-proof.json")
    _write_json(
        new_root / "reports" / "action-report.json",
        _action_report(new_manifest, new_evidence),
    )

    register_child_run(manifest, old_manifest, suite="browser", status="failed")
    register_child_run(manifest, new_manifest, suite="browser", status="passed")
    os.utime(
        old_root / "reports" / "action-report.json",
        ns=(1_000_000_000, 1_000_000_000),
    )
    os.utime(
        new_root / "reports" / "action-report.json",
        ns=(2_000_000_000, 2_000_000_000),
    )

    report = build_report(manifest)
    action = next(item for item in report["checks"] if item["id"] == "F01-01")  # type: ignore[index]

    assert action["status"] == "PASS"
    assert action["evidence_paths"] == [new_evidence]


def test_mainline_parent_accepts_registered_extension_scenario(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    child_id = uuid4()
    child_root = manifest.run_root.parent / str(child_id)
    child_root.mkdir(parents=True)
    child_manifest = manifest.model_copy(
        update={
            "run_id": child_id,
            "run_root": child_root,
            "scenario": "governance",
            "database_name": f"sidera_interview_test_{child_id.hex}",
            "knowledge_database_name": f"sidera_interview_knowledge_{child_id.hex}",
        }
    )
    _write_json(child_root / "manifest.json", json.loads(child_manifest.model_dump_json()))

    register_child_run(manifest, child_manifest, suite="browser", status="passed")

    report = build_report(manifest)

    assert str(child_root) in report["evidence_roots"]


def test_non_mainline_parent_rejects_registered_extension_scenario(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "controls"})
    child_id = uuid4()
    child_root = manifest.run_root.parent / str(child_id)
    child_root.mkdir(parents=True)
    child_manifest = manifest.model_copy(
        update={
            "run_id": child_id,
            "run_root": child_root,
            "scenario": "governance",
            "database_name": f"sidera_interview_test_{child_id.hex}",
            "knowledge_database_name": f"sidera_interview_knowledge_{child_id.hex}",
        }
    )

    with pytest.raises(ValueError, match="child_run_registry_identity_mismatch"):
        register_child_run(manifest, child_manifest, suite="browser", status="passed")


def test_build_report_records_catalog_mismatch_without_raising(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    invalid_catalog = tmp_path / "action-catalog.json"
    invalid_catalog.write_text(json.dumps({"schema_version": 1, "actions": []}), encoding="utf-8")

    report = build_report(manifest, catalog_path=invalid_catalog)

    assert report["readiness"] == "NOT_READY"
    assert report["catalog_mismatch"] is True
    assert "action_group_missing:F01" in report["diagnostics"]
