from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4, uuid5

import pytest

from sidera_control_plane.adapters.demo_worker import DemoWorkerAdapter
from sidera_control_plane.demo.bootstrap import _resources
from sidera_control_plane.demo.state import DemoStateStore
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    GatewayOperationType,
    gateway_operation_id,
)
from sidera_control_plane.domain.workspace import workspace_manifest_digest
from tests.unit.test_interview_handoff import _adapter, _session_inputs

pytestmark = pytest.mark.integration


def test_demo_handoff_has_one_effective_writer_across_restart(tmp_path: Path) -> None:
    manifest, source_adapter = _adapter(tmp_path)
    source_spec, source_workspace = _session_inputs(manifest, tmp_path)
    source_start_operation = uuid4()
    source = source_adapter.start_session(
        source_spec,
        source_workspace,
        operation_id=source_start_operation,
    )
    tuple(source_adapter.read_events(source))
    checkpoint = source_adapter.capture_handoff_checkpoint(
        source,
        transition_id=uuid4(),
        source_input_cutoff_sequence=1,
        operation_id=uuid4(),
    )

    source_input = source_adapter.send_input(
        source,
        "旧 Agent 不再写入",
        (),
        operation_id=uuid4(),
    )
    assert source_input.outcome is DeliveryOutcome.BLOCKED
    assert source_input.failure_code == "handoff_checkpointed"

    stop = source_adapter.stop_session(
        source,
        operation_id=gateway_operation_id(
            source_spec.execution_id,
            GatewayOperationType.STOP_SESSION,
        ),
    )
    assert stop.outcome is DeliveryOutcome.DELIVERED
    assert stop.accepted_sequence == checkpoint.accepted_sequence

    target_execution_id = uuid4()
    target_spec, target_workspace = _session_inputs(
        manifest,
        tmp_path,
        execution_id=target_execution_id,
    )
    target_adapter = DemoWorkerAdapter(
        manifest,
        _resources(manifest).instances["worker_secondary"],
        lambda _workspace: tmp_path / "workspace",
    )
    target_spec = replace(
        target_spec,
        worker_id=manifest.worker_ids["replacement"],
        adapter_module_ref=target_adapter.instance.id,
    )
    target = target_adapter.start_session(
        target_spec,
        target_workspace,
        operation_id=uuid4(),
    )
    assert target.context_binding is not None

    blocked_input = target_adapter.send_input(
        target,
        "尚未打开写权限",
        (),
        operation_id=uuid4(),
    )
    assert blocked_input.outcome is DeliveryOutcome.BLOCKED
    assert blocked_input.failure_code == "handoff_writer_disabled"

    wrong_writer = target_adapter.enable_handoff_writer(
        target,
        fencing_token=2,
        operation_id=uuid4(),
    )
    assert wrong_writer.outcome is DeliveryOutcome.BLOCKED
    assert wrong_writer.failure_code == "handoff_fencing_mismatch"

    writer_operation = uuid4()
    enabled = target_adapter.enable_handoff_writer(
        target,
        fencing_token=1,
        operation_id=writer_operation,
    )
    replay = target_adapter.enable_handoff_writer(
        target,
        fencing_token=1,
        operation_id=writer_operation,
    )
    assert enabled.outcome is DeliveryOutcome.DELIVERED
    assert replay == enabled

    state = DemoStateStore(
        manifest.run_root / "worker-state" / source_adapter.instance.id.hex,
        manifest.run_id,
    )
    source_after_checkpoint = state.events_after(
        source.execution_session_id,
        checkpoint.accepted_sequence,
    )
    assert [event.event_type for event in source_after_checkpoint] == [
        "session.stopped"
    ]
    target_events = tuple(target_adapter.read_events(target))
    assert any(event.event_type == "handoff.writer_enabled" for event in target_events)


def test_demo_target_start_unknown_with_persisted_start_fact_is_not_duplicated(
    tmp_path: Path,
) -> None:
    manifest, adapter = _adapter(tmp_path)
    spec, workspace = _session_inputs(manifest, tmp_path)
    spec = replace(spec, worker_id=manifest.worker_ids["replacement"])
    operation_id = uuid4()
    session_id = uuid5(
        manifest.run_id,
        f"demo-worker-session:{adapter.instance.id}:{spec.execution_id}",
    )
    request = {
        "execution_id": str(spec.execution_id),
        "workspace_id": str(spec.workspace_id),
        "worker_id": str(spec.worker_id),
        "adapter_module_id": str(spec.adapter_module_ref),
        "workspace_module_id": str(spec.workspace_module_ref),
        "workspace_digest": workspace_manifest_digest(workspace),
    }
    state = DemoStateStore(
        manifest.run_root / "worker-state" / adapter.instance.id.hex,
        manifest.run_id,
    )
    key = f"session/{spec.execution_id}"
    state.reserve(key, operation_id, request)
    state.append_event(
        session_id,
        operation_id,
        "session.started",
        {
            "execution_id": str(spec.execution_id),
            "worker_id": str(spec.worker_id),
            "adapter_module_id": str(spec.adapter_module_ref),
            "workspace_digest": workspace_manifest_digest(workspace),
            "simulation": True,
        },
    )
    state.settle(key, operation_id, "unknown", {"reason": "simulated restart"})

    recovered = adapter.start_session(spec, workspace, operation_id=operation_id)
    replayed = adapter.start_session(spec, workspace, operation_id=operation_id)
    assert replayed == recovered
    events = state.events_after(session_id, 0)
    assert [event.event_type for event in events].count("session.started") == 1
