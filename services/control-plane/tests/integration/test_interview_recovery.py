from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4, uuid5

import pytest

from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapterError
from sidera_control_plane.demo.faults import DemoProcessCrash
from sidera_control_plane.demo.recovery import (
    crash_after_candidate_publish,
    reconcile_demo_operations,
    write_recovery_marker,
)
from sidera_control_plane.demo.state import DemoStateStore
from sidera_control_plane.domain.workspace import workspace_manifest_digest
from tests.unit.interview_demo_support import manifest_for
from tests.unit.test_interview_handoff import _adapter, _session_inputs

pytestmark = pytest.mark.integration


def test_recovery_settles_a_persisted_input_only_when_its_output_is_present(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "recovery"})
    state = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    session_id = uuid4()
    operation_id = uuid4()
    input_key = f"input/{session_id}/{operation_id}"
    state.reserve(input_key, operation_id, {"session_id": str(session_id)})
    accepted = state.append_event(
        session_id,
        uuid4(),
        "input.accepted",
        {
            "input_operation_id": str(operation_id),
            "worker_item_key": "validate",
            "text": "手机号允许为空",
        },
    )
    output_key = f"worker-output/{session_id}/{operation_id}"
    output_operation_id = uuid4()
    state.reserve(output_key, output_operation_id, {"session_id": str(session_id)})
    state.settle(
        output_key,
        output_operation_id,
        "applied",
        {"content_hash": "a" * 64},
    )

    records = reconcile_demo_operations(manifest)

    recovered = next(record for record in records if record.operation_id == operation_id)
    assert recovered.status == "applied"
    assert recovered.result == {
        "outcome": "delivered",
        "accepted_sequence": accepted.sequence,
        "content_hash": "a" * 64,
    }


def test_recovery_marks_persisted_input_without_output_unknown(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "recovery"})
    state = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    session_id = uuid4()
    operation_id = uuid4()
    key = f"input/{session_id}/{operation_id}"
    state.reserve(key, operation_id, {"session_id": str(session_id)})
    state.append_event(
        session_id,
        uuid4(),
        "input.accepted",
        {"input_operation_id": str(operation_id), "worker_item_key": "validate"},
    )

    records = reconcile_demo_operations(manifest)

    recovered = next(record for record in records if record.operation_id == operation_id)
    assert recovered.status == "unknown"
    assert recovered.result["reason"] == "input_persisted_without_output"


def test_recovery_requires_a_matching_file_marker_and_content_hash(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "recovery"})
    state = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    operation_id = uuid4()
    target = manifest.run_root / "results" / "recovered.txt"
    target.parent.mkdir(parents=True)
    content = b"recovered artifact\n"
    target.write_bytes(content)
    state.reserve(
        f"file-effect/{operation_id}", operation_id, {"path": str(target)}
    )
    write_recovery_marker(
        manifest,
        operation_id,
        kind="file",
        path=target,
        content_hash=hashlib.sha256(content).hexdigest(),
        result={"path": str(target), "content_hash": hashlib.sha256(content).hexdigest()},
    )

    records = reconcile_demo_operations(manifest)

    recovered = next(record for record in records if record.operation_id == operation_id)
    assert recovered.status == "applied"
    assert recovered.result["content_hash"] == hashlib.sha256(content).hexdigest()


def test_recovery_does_not_trust_a_stale_file_marker(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "recovery"})
    state = DemoStateStore(manifest.run_root / "state", manifest.run_id)
    operation_id = uuid4()
    target = manifest.run_root / "results" / "stale.txt"
    target.parent.mkdir(parents=True)
    target.write_text("changed\n", encoding="utf-8")
    state.reserve(
        f"file-effect/{operation_id}", operation_id, {"path": str(target)}
    )
    marker_dir = manifest.run_root / "recovery" / "markers"
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{operation_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": str(manifest.run_id),
                "operation_id": str(operation_id),
                "kind": "file",
                "path": str(target),
                "content_hash": hashlib.sha256(b"original\n").hexdigest(),
                "result": {"content_hash": "original"},
            }
        ),
        encoding="utf-8",
    )

    records = reconcile_demo_operations(manifest)

    recovered = next(record for record in records if record.operation_id == operation_id)
    assert recovered.status == "unknown"
    assert recovered.result["reason"] == "file_marker_hash_mismatch"


def test_unknown_session_start_without_a_start_fact_stays_blocked(
    tmp_path: Path,
) -> None:
    manifest, adapter = _adapter(tmp_path, scenario="recovery")
    spec, workspace = _session_inputs(manifest, tmp_path)
    operation_id = uuid4()
    state = DemoStateStore(
        manifest.run_root / "worker-state" / adapter.instance.id.hex,
        manifest.run_id,
    )
    session_key = f"session/{spec.execution_id}"
    request = {
        "execution_id": str(spec.execution_id),
        "workspace_id": str(spec.workspace_id),
        "worker_id": str(spec.worker_id),
        "adapter_module_id": str(spec.adapter_module_ref),
        "workspace_module_id": str(spec.workspace_module_ref),
        "workspace_digest": workspace_manifest_digest(workspace),
    }
    state.reserve(session_key, operation_id, request)
    state.settle(session_key, operation_id, "unknown", {"reason": "restart"})

    with pytest.raises(DemoWorkerAdapterError, match="session_start_outcome_unknown"):
        adapter.start_session(spec, workspace, operation_id=operation_id)

    session_id = uuid5(
        manifest.run_id,
        f"demo-worker-session:{adapter.instance.id}:{spec.execution_id}",
    )
    assert state.events_after(session_id, 0) == ()


def test_candidate_publish_crash_is_one_shot_and_recovery_can_continue(
    tmp_path: Path,
) -> None:
    manifest = manifest_for(tmp_path).model_copy(update={"scenario": "recovery"})
    candidate_id = uuid4()

    with pytest.raises(DemoProcessCrash, match="after-candidate-publish"):
        crash_after_candidate_publish(manifest, candidate_id)

    crash_after_candidate_publish(manifest, candidate_id)
