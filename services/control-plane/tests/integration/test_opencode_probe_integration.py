import os
from pathlib import Path

import pytest

from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    ResumeCapability,
    StabilityLevel,
)

pytestmark = pytest.mark.integration


def test_configured_opencode_probe_reports_experimental_capabilities(
    tmp_path: Path,
) -> None:
    executable = os.environ.get("SIDERA_OPENCODE_EXECUTABLE")
    if not executable:
        pytest.skip("SIDERA_OPENCODE_EXECUTABLE is not configured")

    profile = OpenCodeACPAdapter(executable=executable).probe({"cwd": str(tmp_path)})

    assert profile.input is CapabilityInput.QUEUED
    assert profile.resume is ResumeCapability.SAME_PROCESS
    assert profile.stability is StabilityLevel.EXPERIMENTAL
