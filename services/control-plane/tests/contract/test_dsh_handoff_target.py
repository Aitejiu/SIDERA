from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sidera_control_plane.adapters.dsh_acp import DshACPAdapter
from sidera_control_plane.domain.context_packages import ControlPlaneFactSection
from sidera_control_plane.domain.gateway import DeliveryOutcome
from tests.contract.handoff_adapter_conformance import (
    build_handoff_fixture,
)
from tests.unit.test_dsh_acp import _HOST


def test_dsh_target_contract_holds_before_writer_enable(tmp_path: Path) -> None:
    # Build the public handoff fixture shape, then use a DSH target adapter
    # whose host is a real child process but whose model is provider-free.
    adapter = DshACPAdapter(
        argv=(__import__("sys").executable, "-c", _HOST),
        process_env={"PATH": __import__("os").environ.get("PATH", "")},
        workspace_root=tmp_path,
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        handoff_target_enabled=True,
    )
    fixture = build_handoff_fixture(adapter, tmp_path)
    base_target_context = fixture.target_spec.context_package
    assert base_target_context is not None
    target_context = replace(
        base_target_context,
        metadata=replace(
            base_target_context.metadata,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        control_plane_facts=ControlPlaneFactSection(
            facts={"handoff": {"instruction": "continue the transferred work"}}
        ),
    )
    target_spec = replace(
        fixture.target_spec,
        context_package=target_context,
        context_package_ref=(
            f"context-package://{target_context.metadata.package_id}/"
            f"{target_context.metadata.package_hash}"
        ),
    )
    adapter.accept_context(target_context, operation_id=uuid4())
    handle = adapter.start_session(target_spec, fixture.target_workspace, operation_id=uuid4())
    assert handle.context_binding is not None
    assert adapter.active_processes()
    before = adapter.send_input(handle, "must wait", (), operation_id=uuid4())
    assert before.outcome is DeliveryOutcome.BLOCKED
    assert before.failure_code == "handoff_writer_disabled"

    stale = adapter.enable_handoff_writer(
        handle,
        fencing_token=target_spec.fencing_token - 1,
        operation_id=uuid4(),
    )
    assert stale.outcome is not DeliveryOutcome.DELIVERED
    operation_id = uuid4()
    enabled = adapter.enable_handoff_writer(
        handle,
        fencing_token=target_spec.fencing_token,
        operation_id=operation_id,
    )
    repeated = adapter.enable_handoff_writer(
        handle,
        fencing_token=target_spec.fencing_token,
        operation_id=operation_id,
    )
    assert enabled.outcome is DeliveryOutcome.DELIVERED
    assert repeated == enabled
    assert adapter.send_input(handle, "continue", (), operation_id=uuid4()).outcome is DeliveryOutcome.DELIVERED
    inspection = adapter.inspect_handoff_operation(handle, operation_id=operation_id)
    assert inspection.outcome.value == "succeeded"
    assert inspection.context_binding == handle.context_binding
    adapter.stop_session(handle, operation_id=uuid4())


def test_dsh_target_profile_can_be_used_without_source_checkpoint() -> None:
    adapter = DshACPAdapter(
        argv=("node", "host"),
        process_env={"PATH": ""},
        handoff_target_enabled=True,
    )
    profile = adapter._profile
    assert profile.handoff_checkpoint_versions == ()
    assert profile.handoff_session_context_versions == (1,)
    assert profile.handoff_writer_gate_versions == (1,)
    assert profile.handoff_inspection is True
