"""Opt-in R6.2 gate for OpenCode handing off to a distinct real Adapter."""

from __future__ import annotations

import os
from typing import Any

import pytest

from sidera_control_plane.gateway_worker import (
    WorkerAdapterRegistration,
    worker_adapter_inventory,
)


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

    selected_type = os.environ.get("SIDERA_R6_2_SECOND_ADAPTER_TYPE", "").strip()
    candidate = next(
        (
            item
            for item in _qualifying_second_adapters()
            if item.adapter_type == selected_type
        ),
        None,
    )
    if candidate is None:
        inventory = ", ".join(item.adapter_type for item in _qualifying_second_adapters())
        pytest.skip(
            "no qualifying registered non-OpenCode real Adapter is available; "
            f"selected={selected_type or '<unset>'}, eligible={inventory or '<none>'}"
        )

    # A qualifying entry must be wired to a real factory before this test can
    # be enabled. Keeping this explicit prevents a future inventory-only edit
    # from silently turning the release gate into a false pass.
    pytest.fail(
        "registered second Adapter has no approved real factory integration yet: "
        f"{candidate.adapter_type} ({candidate.implementation})"
    )


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
