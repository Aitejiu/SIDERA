from dataclasses import replace

import pytest
from test_planning_proposals import payload, planning_profile_snapshot
from test_revision_diff import base_snapshot, revision_payload
from test_revision_planning_context import _request

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import PlanningRequestKind
from sidera_control_plane.domain.planning_proposals import (
    PlanningProposalPayload,
)
from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    SubtaskDependency,
)
from sidera_control_plane.domain.proposal_validation import (
    ProposalValidationError,
    validate_proposal,
)


def expect_code(item: PlanningProposalPayload, code: str) -> None:
    with pytest.raises(ProposalValidationError) as error:
        validate_proposal(
            item,
            profile_snapshot=planning_profile_snapshot(),
            brief_revision=1,
        )
    assert error.value.code == code


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        (lambda item: replace(item, schema_version=1), "proposal_schema_invalid"),
        (
            lambda item: replace(
                item,
                subtask_dependencies=(
                    *item.subtask_dependencies,
                    SubtaskDependency(
                        upstream_subtask_key="verify",
                        downstream_subtask_key="design",
                        kind=DependencyKind.HARD,
                        required_evidence_refs=(),
                        enforcement=DependencyEnforcement.CONTROL_PLANE,
                    ),
                ),
            ),
            "proposal_cycle",
        ),
        (
            lambda item: replace(
                item,
                worker_items=(
                    replace(item.worker_items[0], capability_requirements=("secret",)),
                    *item.worker_items[1:],
                ),
            ),
            "proposal_capability_forbidden",
        ),
        (
            lambda item: replace(
                item,
                decision_summary=replace(
                    item.decision_summary,
                    estimated_cost_microunits=2_000_000,
                ),
            ),
            "proposal_budget_exceeded",
        ),
        (
            lambda item: replace(
                item,
                worker_items=(
                    replace(item.worker_items[0], pinned_worker_id="worker-id"),
                    *item.worker_items[1:],
                ),
            ),
            "proposal_worker_binding_forbidden",
        ),
        (
            lambda item: replace(
                item,
                kind="revision",  # type: ignore[arg-type]
            ),
            "proposal_revision_operation_forbidden",
        ),
        (
            lambda item: replace(
                item,
                execution_intents=item.execution_intents[:-1],
            ),
            "proposal_intent_mismatch",
        ),
        (
            lambda item: replace(
                item,
                execution_intents=(
                    replace(
                        item.execution_intents[0],
                        expected_artifact_refs=("not-a-reference",),
                    ),
                    *item.execution_intents[1:],
                ),
            ),
            "proposal_reference_invalid",
        ),
        (
            lambda item: replace(
                item,
                impact_policy=replace(item.impact_policy, manual_gate=True),
            ),
            "proposal_manual_gate_unsupported",
        ),
    ),
)
def test_proposal_validation_rejects_unsupported_shapes(
    mutation, code: str
) -> None:
    expect_code(mutation(payload()), code)


def test_proposal_validation_requires_exact_worker_item_capability_intents() -> None:
    item = payload()
    changed_intent = replace(
        item.execution_intents[0], capability_requirements=("test",)
    )
    expect_code(
        replace(item, execution_intents=(changed_intent, *item.execution_intents[1:])),
        "proposal_intent_mismatch",
    )


def test_proposal_validation_rejects_unknown_mapping_fields_and_implicit_ids() -> None:
    item = payload().to_mapping()
    with pytest.raises(ProposalValidationError) as error:
        validate_proposal(
            {**item, "unknown": True},  # type: ignore[arg-type]
            profile_snapshot=planning_profile_snapshot(),
            brief_revision=1,
        )
    assert error.value.code == "proposal_schema_invalid"

    with pytest.raises(ProposalValidationError) as error:
        validate_proposal(
            {**item, "root_task_id": "root"},  # type: ignore[arg-type]
            profile_snapshot=planning_profile_snapshot(),
            brief_revision=1,
        )
    assert error.value.code == "proposal_worker_binding_forbidden"


def test_proposal_validation_rejects_forbidden_adapter_preference() -> None:
    base = payload()
    item = replace(
        base,
        execution_intents=(
            replace(base.execution_intents[0], adapter_preferences=("unsafe",)),
            *base.execution_intents[1:],
        ),
    )

    expect_code(item, "proposal_capability_forbidden")


def test_revision_validation_accepts_v3_against_the_exact_base() -> None:
    snapshot = base_snapshot()
    request = _request(snapshot)
    proposal = revision_payload(snapshot)
    revision_profile = replace(
        request.profile_snapshot,
        allowed_capabilities=("code", "python"),
    )

    validated = validate_proposal(
        proposal,
        brief_revision=1,
        request=request,
        profile_snapshot=revision_profile,
        capability_snapshot=request.profile_snapshot.planner_capabilities,
        base_snapshot=snapshot,
    )

    assert validated is proposal
    assert validated.schema_version == 3
    assert validated.kind is PlanningRequestKind.REVISION


def test_revision_validation_rejects_a_create_payload_even_with_a_v3_profile() -> None:
    snapshot = base_snapshot()
    request = _request(snapshot)

    with pytest.raises(ValidationError, match="revision proposal schema v3"):
        validate_proposal(
            payload(),
            brief_revision=1,
            request=request,
            profile_snapshot=request.profile_snapshot,
            capability_snapshot=request.profile_snapshot.planner_capabilities,
            base_snapshot=snapshot,
        )


def test_revision_validation_rejects_a_base_mismatch_with_a_typed_error() -> None:
    snapshot = base_snapshot()
    request = _request(snapshot)
    proposal = revision_payload(snapshot)
    changed_snapshot = replace(snapshot, title="different base")

    with pytest.raises(ValidationError, match="base.*hash"):
        validate_proposal(
            proposal,
            brief_revision=1,
            request=request,
            profile_snapshot=request.profile_snapshot,
            capability_snapshot=request.profile_snapshot.planner_capabilities,
            base_snapshot=changed_snapshot,
        )
