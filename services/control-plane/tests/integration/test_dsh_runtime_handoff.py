from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sidera_control_plane.adapters.dsh_acp import DshACPAdapter
from sidera_control_plane.domain.modules import HandoffCheckpointMode
from tests.unit.test_dsh_acp import _HOST


@pytest.fixture
def clean_database():
    """This capability integration test has no database side effects."""

    yield


def test_dsh_runtime_binding_exposes_target_only_capabilities(tmp_path: Path) -> None:
    adapter = DshACPAdapter(
        argv=(sys.executable, "-c", _HOST),
        process_env={"PATH": os.environ.get("PATH", "")},
        workspace_root=tmp_path,
        request_timeout_seconds=2,
        stop_grace_seconds=0.2,
        handoff_target_enabled=True,
    )
    profile = adapter.probe({"cwd": str(tmp_path)})
    assert profile.handoff_checkpoint_mode is HandoffCheckpointMode.UNSUPPORTED
    assert profile.handoff_checkpoint_versions == ()
    assert profile.handoff_session_context_versions == (1,)
    assert profile.handoff_writer_gate_versions == (1,)
    assert profile.handoff_inspection is True
