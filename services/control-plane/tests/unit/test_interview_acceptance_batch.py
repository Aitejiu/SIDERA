from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.demo.acceptance_batch import (
    AcceptanceBatchError,
    build_batch,
    build_entry,
    read_batch,
    validate_batch,
    write_batch,
)


def _entry(batch_id):
    return build_entry(
        batch_id=batch_id,
        suite="browser",
        scenario="mainline",
        stable_test_key="tests/interview/mainline.spec.ts::mainline",
        project="desktop",
        viewport={"width": 1440, "height": 900},
        check_id="F01-01",
    )


def test_batch_round_trips_with_stable_identity(tmp_path: Path) -> None:
    batch_id = uuid4()
    batch = build_batch(
        suite="browser",
        scenario="mainline",
        run_id=uuid4(),
        workspace_id=uuid4(),
        implementation_fingerprint="a" * 64,
        fingerprint_version=2,
        code_commit="b" * 40,
        catalog_fingerprint="c" * 64,
        expected=[_entry(batch_id)],
        batch_id=batch_id,
    )
    path = tmp_path / "reports" / "batches" / f"{batch_id}.json"
    write_batch(path, batch)
    assert read_batch(path) == batch
    assert path.stat().st_mode & 0o077 == 0


def test_batch_rejects_unknown_fields_and_duplicate_tests() -> None:
    batch_id = uuid4()
    entry = _entry(batch_id)
    batch = build_batch(
        suite="browser",
        scenario="mainline",
        run_id=None,
        workspace_id=None,
        implementation_fingerprint=None,
        fingerprint_version=None,
        code_commit=None,
        catalog_fingerprint=None,
        expected=[entry],
        batch_id=batch_id,
    )
    with pytest.raises(AcceptanceBatchError, match="fields"):
        validate_batch({**batch, "unexpected": True})
    with pytest.raises(AcceptanceBatchError, match="duplicate"):
        validate_batch({**batch, "expected": [entry, entry]})


def test_browser_and_backend_identity_rules_are_explicit() -> None:
    batch_id = uuid4()
    with pytest.raises(AcceptanceBatchError, match="viewport"):
        build_entry(
            batch_id=batch_id,
            suite="browser",
            scenario="mainline",
            stable_test_key="same",
            project="mobile",
            viewport=None,
            check_id="F01-01",
        )
    with pytest.raises(AcceptanceBatchError, match="backend"):
        build_entry(
            batch_id=batch_id,
            suite="unit",
            scenario="none",
            stable_test_key="tests/unit/test_demo.py::test_one",
            project="none",
            viewport={"width": 1, "height": 1},
            check_id="M1",
            evidence_kind="real_local_verification",
        )
