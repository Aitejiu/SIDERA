"""Opt-in R6.2 gate for OpenCode handing off to a distinct real Adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass(frozen=True, slots=True)
class _RegisteredAdapterInventory:
    adapter_type: str
    implementation: str
    capability_versions: tuple[str, ...]
    is_fake: bool
    delegates_to_opencode: bool
    eligibility_note: str


# This inventory mirrors the worker adapter factories registered in
# sidera_control_plane.gateway_worker.build_gateway. It is intentionally kept
# in the gate until the runtime exposes a queryable registry API.
_REGISTERED_ADAPTERS = (
    _RegisteredAdapterInventory(
        adapter_type="fake",
        implementation="FakeWorkerAdapter",
        capability_versions=("context-package:v1", "handoff:v1"),
        is_fake=True,
        delegates_to_opencode=False,
        eligibility_note="deterministic test boundary; excluded from the real gate",
    ),
    _RegisteredAdapterInventory(
        adapter_type="opencode",
        implementation="OpenCodeACPAdapter",
        capability_versions=("context-package:v1", "handoff:v1"),
        is_fake=False,
        delegates_to_opencode=True,
        eligibility_note="same provider as source; excluded from the distinct gate",
    ),
    _RegisteredAdapterInventory(
        adapter_type="opencode-acp",
        implementation="OpenCodeACPAdapter",
        capability_versions=("context-package:v1", "handoff:v1"),
        is_fake=False,
        delegates_to_opencode=True,
        eligibility_note="alias of opencode; excluded from the distinct gate",
    ),
)


def _qualifying_second_adapters() -> tuple[_RegisteredAdapterInventory, ...]:
    return tuple(
        item
        for item in _REGISTERED_ADAPTERS
        if not item.is_fake and not item.delegates_to_opencode
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
            "eligibility_note": item.eligibility_note,
        }
        for item in _REGISTERED_ADAPTERS
    )
