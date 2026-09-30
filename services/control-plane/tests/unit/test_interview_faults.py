from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sidera_control_plane.demo.faults import consume_fault, consume_fault_once
from tests.unit.interview_demo_support import manifest_for


def test_fault_is_scoped_to_one_logical_operation(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "rework"})
    first, second = uuid4(), uuid4()

    assert consume_fault(manifest, "verification-fails-once", first) is True
    assert consume_fault(manifest, "verification-fails-once", first) is True
    assert consume_fault(manifest, "verification-fails-once", second) is False


def test_faults_are_disabled_for_mainline(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path)
    operation_id = uuid4()

    assert consume_fault(manifest, "after-input-persist", operation_id) is False
    assert not (manifest.run_root / "state" / "operations.json").exists()


def test_fault_point_is_validated_before_consumption(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "rework"})

    with pytest.raises(ValueError, match="unknown_demo_fault_point"):
        consume_fault(manifest, "not-a-fault", uuid4())  # type: ignore[arg-type]


def test_one_shot_fault_does_not_fire_again_after_restart(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "recovery"})
    operation_id = uuid4()

    assert consume_fault_once(manifest, "after-candidate-publish", operation_id) is True
    assert consume_fault_once(manifest, "after-candidate-publish", operation_id) is False
    assert consume_fault_once(manifest, "after-candidate-publish", uuid4()) is False
