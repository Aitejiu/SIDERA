from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from sidera_control_plane.domain.collaboration import (
    CollaborationPolicyRequestV1,
    CollaborationPolicyRequestV2,
    CollaborationPolicySnapshotV2,
    CollaborationRuntimeBindingSnapshot,
    resolve_collaboration_policy_v2,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import (
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    StabilityLevel,
)

from .result_input_fixtures import group_mapping


def runtime_capability(
    *,
    projection_versions: tuple[int, ...] = (2,),
    decision_versions: tuple[int, ...] = (2,),
    strategy_ids: tuple[str, ...] = ("all_required", "input_groups_v1"),
) -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime-v2",
        projection_schema_versions=projection_versions,
        decision_schema_versions=decision_versions,
        strategy_ids=strategy_ids,
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.EXPERIMENTAL,
    )


def runtime_binding(
    *, capability: CollaborationRuntimeCapabilityProfile | None = None,
) -> CollaborationRuntimeBindingSnapshot:
    capability = capability or runtime_capability()
    return CollaborationRuntimeBindingSnapshot(
        runtime_definition_id=uuid4(),
        runtime_instance_id=uuid4(),
        runtime_interface_version=capability.interface_version,
        runtime_config_version=3,
        runtime_capability_snapshot=capability,
    )


def request_mapping(
    *, groups: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "mode": "enabled",
        "default_policy": {"strategy_id": "all_required"},
        "subtask_overrides": [],
        "input_groups": (
            deepcopy(groups) if groups is not None else [group_mapping()]
        ),
    }


def test_policy_v2_round_trips_input_groups_without_changing_v1() -> None:
    raw = request_mapping()

    value = CollaborationPolicyRequestV2.from_mapping(raw)

    assert value.schema_version == 2
    assert value.to_mapping() == raw
    assert value.default_policy.strategy_id.value == "all_required"
    assert len(value.input_groups[0].candidates) == 3
    assert value.input_groups[0].minimum_count == 2
    with pytest.raises(ValidationError, match="unknown collaboration policy request"):
        CollaborationPolicyRequestV1.from_mapping(raw)


def test_policy_v2_resolves_against_a_v2_runtime_binding() -> None:
    request = CollaborationPolicyRequestV2.from_mapping(request_mapping())

    resolved = resolve_collaboration_policy_v2(
        request,
        runtime_binding(),
        subtask_keys=frozenset({"research", "summary"}),
    )

    assert isinstance(resolved, CollaborationPolicySnapshotV2)
    assert resolved.schema_version == 2
    assert resolved.input_groups[0].group_key == "research"
    assert resolved.effective_policy("summary").strategy_id.value == "all_required"
    assert CollaborationPolicySnapshotV2.from_mapping(resolved.to_mapping()) == resolved


def test_policy_snapshot_v2_fixes_runtime_binding_and_capability() -> None:
    request = CollaborationPolicyRequestV2.from_mapping(request_mapping())
    binding = runtime_binding()

    snapshot = CollaborationPolicySnapshotV2.from_request(request, binding)

    assert snapshot.binding == binding
    assert snapshot.runtime_capability_snapshot == runtime_capability()
    assert snapshot.input_groups == request.input_groups
    assert snapshot.to_mapping()["schema_version"] == 2


def test_policy_snapshot_v2_assigns_input_group_strategy_only_to_consumers() -> None:
    request = CollaborationPolicyRequestV2.from_mapping(request_mapping())
    snapshot = CollaborationPolicySnapshotV2.from_request(request, runtime_binding())

    assert snapshot.effective_strategy_id(
        worker_item_key="summary", subtask_key="summary"
    ) == "input_groups_v1"
    assert snapshot.effective_strategy_id(
        worker_item_key="other", subtask_key="summary"
    ) == "all_required"

    disabled_request = request_mapping()
    disabled_request["subtask_overrides"] = [
        {
            "subtask_key": "summary",
            "enabled": False,
            "strategy_id": "all_required",
        }
    ]
    disabled_snapshot = CollaborationPolicySnapshotV2.from_request(
        CollaborationPolicyRequestV2.from_mapping(disabled_request),
        runtime_binding(),
    )
    with pytest.raises(ValidationError, match="input group consumer"):
        disabled_snapshot.effective_strategy_id(
            worker_item_key="summary", subtask_key="summary"
        )


def test_policy_v2_rejects_disabled_groups_and_bad_schema_version() -> None:
    disabled = request_mapping()
    disabled["mode"] = "disabled"
    with pytest.raises(ValidationError, match="disabled collaboration policy"):
        CollaborationPolicyRequestV2.from_mapping(disabled)

    invalid_version = request_mapping()
    invalid_version["schema_version"] = 1
    with pytest.raises(ValidationError, match="schema version"):
        CollaborationPolicyRequestV2.from_mapping(invalid_version)


@pytest.mark.parametrize(
    "capability",
    [
        runtime_capability(projection_versions=(1,)),
        runtime_capability(decision_versions=(1,)),
        runtime_capability(strategy_ids=("all_required",)),
    ],
)
def test_policy_v2_requires_runtime_input_group_capabilities(
    capability: CollaborationRuntimeCapabilityProfile,
) -> None:
    request = CollaborationPolicyRequestV2.from_mapping(request_mapping())

    with pytest.raises(ValidationError, match="input_groups_v1"):
        resolve_collaboration_policy_v2(
            request,
            runtime_binding(capability=capability),
            subtask_keys=frozenset({"research", "summary"}),
        )
