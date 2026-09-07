from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from sidera_control_plane.domain.modules import (
    CapabilityInput,
    IdempotencyCapability,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
    WorkerAdapterCapabilityProfile,
    WorkspaceProviderCapabilityProfile,
)
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.planning_proposals import PlanningProposalPayloadV5
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.proposal_validation import (
    ProposalValidationError,
    validate_proposal,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_from_payload,
    capability_profile_to_payload,
)

from .result_input_fixtures import proposal_v5_mapping
from .result_integration_fixtures import (
    integration_mapping,
    proposal_v6_mapping,
    revision_v6_mapping,
)
from .test_plan_snapshot_v4 import plan_v4_payload
from .test_planning_proposals_v5 import v5_profile_snapshot

V5_PROPOSAL_BYTES_HASH = (
    "57de93aed4ef8331a5f1dec74f883b1d74e0ea36e3a5145da853a13b35646ce8"
)
SNAPSHOT_V4_HASH = "270347a52efaee175dae753175a8c7a1325bbf0ab811546301cba798a6232820"


def v6_profile_snapshot():
    profile = v5_profile_snapshot()
    capabilities = replace(profile.planner_capabilities, proposal_schema_versions=(6,))
    return replace(
        profile,
        proposal_schema_version=6,
        planner_capabilities=capabilities,
    )


def test_v6_acceptance_change_changes_plan_hash() -> None:
    value = proposal_v6_mapping()

    first = _v6_payload(value)
    value["integration_spec"]["criteria"][0]["text"] = (  # type: ignore[index]
        "Reject all expired credentials"
    )
    second = _v6_payload(value)

    assert first.to_mapping() != second.to_mapping()
    assert first.content_hash != second.content_hash
    assert first.integration_spec.to_mapping()["finalization_policy"] == "stop_unused"


def test_v6_mapping_dispatches_through_proposal_validation() -> None:
    validated = validate_proposal(
        proposal_v6_mapping(),
        profile_snapshot=v6_profile_snapshot(),
        brief_revision=1,
    )

    assert validated.schema_version == 6
    assert validated.to_mapping()["integration_spec"] == proposal_v6_mapping()[
        "integration_spec"
    ]


def test_revision_v6_round_trips_and_hashes_the_integration_spec() -> None:
    raw = revision_v6_mapping()

    value = PlanningRevisionProposalPayloadV6.from_mapping(raw)
    changed = deepcopy(raw)
    changed["integration_spec"]["criteria"][0]["text"] = (  # type: ignore[index]
        "Reject all expired credentials before integration"
    )
    changed_value = PlanningRevisionProposalPayloadV6.from_mapping(changed)

    assert value.to_mapping() == raw
    assert value.content_hash != changed_value.content_hash


def test_v6_rejects_two_initial_intents_for_one_worker_item() -> None:
    value = proposal_v6_mapping()
    intents = value["execution_intents"]
    assert isinstance(intents, list)
    intents.append(deepcopy(intents[0]))

    with pytest.raises(ProposalValidationError, match="execution intent"):
        validate_proposal(
            value,
            profile_snapshot=v6_profile_snapshot(),
            brief_revision=1,
        )


def test_existing_v5_proposal_bytes_remain_frozen() -> None:
    raw = proposal_v5_mapping()
    payload = PlanningProposalPayloadV5.from_mapping(raw)
    encoded = json.dumps(
        payload.to_mapping(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert payload.to_mapping() == raw
    assert hashlib.sha256(encoded).hexdigest() == V5_PROPOSAL_BYTES_HASH


def test_existing_snapshot_v4_hash_remains_frozen() -> None:
    snapshot = TaskPlanSnapshot.from_payload(plan_v4_payload())

    assert plan_snapshot_content_hash(snapshot) == SNAPSHOT_V4_HASH


def test_snapshot_v5_round_trips_an_explicit_integration_spec() -> None:
    raw = plan_v4_payload()
    raw["schema_version"] = 5
    spec = integration_mapping()
    spec["integration_worker_item_key"] = "integration-checker"
    spec["input_group_keys"] = ["research"]
    spec["rework_policy"] = {
        "max_auto_rework_rounds": 2,
        "max_auto_rework_seconds": 1800,
        "allowed_worker_item_keys": [
            "callback-implementer",
            "callback-reviewer",
            "integration-checker",
        ],
    }
    raw["integration_spec"] = spec

    snapshot = TaskPlanSnapshot.from_payload(raw)

    assert snapshot.integration_spec is not None
    assert snapshot.integration_spec.integration_worker_item_key == (
        "integration-checker"
    )
    assert snapshot.to_payload() == raw


def test_result_capabilities_are_explicit_and_legacy_payloads_do_not_infer_support() -> None:
    worker = WorkerAdapterCapabilityProfile(
        input=CapabilityInput.LIVE_SAFE_BOUNDARY,
        idempotency=IdempotencyCapability.OPERATION_ID,
        resume=ResumeCapability.DURABLE,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.ISOLATED_RUNTIME,
        stability=StabilityLevel.STABLE,
        code_result_versions=(1,),
        verification_versions=(1,),
    )
    worker_payload = capability_profile_to_payload(worker)

    assert worker_payload["code_result_versions"] == [1]
    assert worker_payload["verification_versions"] == [1]
    assert capability_profile_from_payload("worker_adapter", worker_payload) == worker

    legacy_worker = dict(worker_payload)
    legacy_worker.pop("code_result_versions")
    legacy_worker.pop("verification_versions")
    restored_legacy = capability_profile_from_payload("worker_adapter", legacy_worker)
    assert restored_legacy.code_result_versions == ()
    assert restored_legacy.verification_versions == ()

    workspace = WorkspaceProviderCapabilityProfile(
        isolation=SandboxCapability.ISOLATED_RUNTIME,
        writable=True,
        git_worktree=True,
        stability=StabilityLevel.STABLE,
        code_input_versions=(1,),
    )
    workspace_payload = capability_profile_to_payload(workspace)
    assert workspace_payload["code_input_versions"] == [1]
    assert capability_profile_from_payload(
        "workspace_provider", workspace_payload
    ) == workspace


def _v6_payload(value: dict[str, object]):
    from sidera_control_plane.domain.planning_proposals import PlanningProposalPayloadV6

    return PlanningProposalPayloadV6.from_mapping(value)
