"""Opt-in R6.2 gate for OpenCode handing off to a distinct real Adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from sidera_control_plane.gateway_worker import (
    WorkerAdapterRegistration,
    worker_adapter_inventory,
)
from tests.e2e.basic_acceptance.runner import run_scenario


def _qualifying_second_adapters() -> tuple[WorkerAdapterRegistration, ...]:
    return tuple(
        item
        for item in worker_adapter_inventory()
        if not item.is_fake
        and not item.delegates_to_opencode
        and not item.is_local_verification
    )


def test_second_adapter_handoff_gate() -> None:
    if os.environ.get("SIDERA_R6_2_SECOND_ADAPTER") != "1":
        pytest.skip(
            "set SIDERA_R6_2_SECOND_ADAPTER=1 to run the distinct Adapter gate"
        )

    manifest_ref = os.environ.get("SIDERA_BASIC_ACCEPTANCE_MANIFEST", "").strip()
    if not manifest_ref:
        pytest.fail(
            "real second Adapter gate requires SIDERA_BASIC_ACCEPTANCE_MANIFEST; "
            "inventory alone is not an acceptance result"
        )
    evidence = run_scenario(
        "second-adapter",
        Path(manifest_ref).expanduser(),
        mode="real",
    )
    assert evidence.status == "PASS", evidence.model_dump(mode="json")


def registered_adapter_inventory() -> tuple[dict[str, Any], ...]:
    """Return bounded inventory facts for release evidence and diagnostics."""

    return tuple(
        {
            "adapter_type": item.adapter_type,
            "implementation": item.implementation,
            "capability_versions": item.capability_versions,
            "is_fake": item.is_fake,
            "delegates_to_opencode": item.delegates_to_opencode,
            "is_local_verification": item.is_local_verification,
            "eligibility_note": item.eligibility_note,
        }
        for item in worker_adapter_inventory()
    )
