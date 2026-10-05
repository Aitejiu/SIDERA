from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sidera_agents.contracts import ProfileRef

from sidera_control_plane.application.proposal_materializer import ProposalMaterializer
from sidera_control_plane.application.revision_materializer import RevisionMaterializer
from sidera_control_plane.domain.commands import CommandEnvelope
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.planning import PlanningRequest, PlanningRequestStatus
from sidera_control_plane.domain.planning_profiles import PlanningProfile
from sidera_control_plane.domain.planning_proposals import (
    PlanningProfileDescriptor,
    ProposalRevision,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
)
from sidera_control_plane.domain.revision_diff import RevisionDiffNormalizer
from tests.unit.test_proposal_materializer import (
    capabilities,
    collaboration_binding,
    proposal_fixture,
)
from tests.unit.test_revision_diff import base_snapshot, revision_payload

NOW = datetime(2026, 10, 5, tzinfo=UTC)
DEFAULT_PLANNER_ID = UUID("72000000-0000-0000-0000-000000000001")
SELECTED_PLANNER_ID = UUID("72000000-0000-0000-0000-000000000002")
PATHS = ("initial", "revision")
ConfirmationPath = Literal["initial", "revision"]


@dataclass(frozen=True)
class ConfirmationCase:
    path: ConfirmationPath
    profile: PlanningProfile
    request: PlanningRequest
    proposal: ProposalRevision
    envelope: CommandEnvelope


def confirmation_case(
    path: ConfirmationPath,
    *,
    native: bool = True,
    descriptor_uses_default: bool = False,
) -> ConfirmationCase:
    policy, _, seed = proposal_fixture()
    base = base_snapshot()
    payload = seed.payload if path == "initial" else revision_payload(base)
    workspace = policy.workspace_id if path == "initial" else base.workspace_id
    policy = replace(
        policy,
        workspace_id=workspace,
        proposal_schema_version=payload.schema_version,
        allowed_capabilities=("code", "docs", "test", "python"),
    )
    if native:
        policy = replace(
            policy,
            execution_mode="native_profile",
            planner_module_instance_id=None,
            planner_module_config_version=None,
            planner_secret_refs=(),
            planner_agent_ref=ProfileRef(
                workspace_id=workspace, profile_id=DEFAULT_PLANNER_ID, revision=1
            ),
        )
        snapshot = replace(
            policy.snapshot(),
            planner_agent_ref=ProfileRef(
                workspace_id=workspace, profile_id=SELECTED_PLANNER_ID, revision=2
            ),
        )
    else:
        snapshot = policy.snapshot(
            replace(capabilities(), proposal_schema_versions=(2, 3))
        )
    if isinstance(payload, PlanningRevisionProposalPayloadV3):
        request = PlanningRequest.create_revision(
            request_id=uuid4(),
            workspace_id=workspace,
            profile_snapshot=snapshot,
            objective="Confirm the original appointed Planner proposal",
            constraints=("Keep the saved policy and Planner appointment",),
            acceptance_criteria=("Confirm only the original saved proposal",),
            context_refs=(),
            requested_outputs=("proposal",),
            source_type="test",
            source_ref="test:native-planner-confirmation",
            actor="operator",
            correlation_id=uuid4(),
            run_id=uuid4(),
            now=NOW,
            base=payload.base_descriptor,
        )
        diff = RevisionDiffNormalizer().normalize(base, payload)
    else:
        request = PlanningRequest.create(
            request_id=uuid4(),
            workspace_id=workspace,
            profile_snapshot=snapshot,
            objective="Confirm the original appointed Planner proposal",
            constraints=("Keep the saved policy and Planner appointment",),
            acceptance_criteria=("Confirm only the original saved proposal",),
            context_refs=(),
            requested_outputs=("proposal",),
            source_type="test",
            source_ref="test:native-planner-confirmation",
            actor="operator",
            correlation_id=uuid4(),
            run_id=uuid4(),
            now=NOW,
        )
        diff = None
    run, lease = request.runs[0], uuid4()
    request.start_planning(run_id=run.id, lease_id=lease, fencing_token=1, now=NOW)
    request.succeed_run(
        run_id=run.id,
        lease_id=lease,
        fencing_token=1,
        provider_ref="native:planner" if native else "fixture:legacy-planner",
        structured_result_type=f"planning.proposal.v{payload.schema_version}",
        usage=run.usage,
        now=NOW,
    )
    descriptor = PlanningProfileDescriptor.from_snapshot(snapshot)
    if descriptor_uses_default:
        descriptor = replace(descriptor, planner_agent_ref=policy.planner_agent_ref)
    proposal = ProposalRevision.create(
        planning_request_id=request.id,
        revision=1,
        brief_revision=1,
        planning_run_id=run.id,
        kind=request.kind,
        payload=payload,
        profile_descriptor=descriptor,
        profile_snapshot=snapshot,
        created_at=NOW,
        derived_diff=diff,
    )
    request.mark_proposal_ready(
        run_id=run.id,
        proposal_revision=proposal.revision,
        content_hash=proposal.content_hash,
        now=NOW,
    )
    command_payload = {
        "profile_id": str(policy.id),
        "profile_version": policy.version,
    }
    if isinstance(payload, PlanningRevisionProposalPayloadV3):
        command_payload.update(
            expected_proposal_revision=proposal.revision,
            expected_proposal_content_hash=proposal.content_hash,
            expected_base_snapshot_id=str(payload.base_descriptor.plan_snapshot_id),
            expected_base_content_hash=payload.base_descriptor.plan_content_hash,
        )
    else:
        command_payload.update(
            proposal_revision=proposal.revision,
            proposal_content_hash=proposal.content_hash,
        )
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace,
        command_type="ConfirmPlanningProposal"
        if path == "initial"
        else "ConfirmPlanRevision",
        target_type="planning_request",
        actor_type="local",
        actor_id="operator",
        target_id=request.id,
        payload=command_payload,
        expected_version=request.version,
        idempotency_key=uuid4().hex,
        correlation_id=uuid4(),
    )
    return ConfirmationCase(path, policy, request, proposal, envelope)


def validate(case: ConfirmationCase) -> object:
    if case.path == "initial":
        return ProposalMaterializer._validate_input(
            case.request, case.proposal, case.profile, case.envelope
        )
    return RevisionMaterializer._validate_input(
        request=case.request,
        proposal=case.proposal,
        profile=case.profile,
        envelope=case.envelope,
    )


@pytest.mark.parametrize("path", PATHS)
def test_native_confirmation_keeps_exact_saved_appointment_and_policy(
    path: ConfirmationPath,
) -> None:
    case = confirmation_case(path)
    original = case.request.profile_snapshot
    assert original.planner_agent_ref is not None
    assert original.planner_agent_ref.profile_id == SELECTED_PLANNER_ID
    assert original.planner_agent_ref.revision == 2
    assert case.profile.planner_agent_ref is not None
    assert case.profile.planner_agent_ref.profile_id == DEFAULT_PLANNER_ID
    assert case.profile.planner_agent_ref.revision == 1
    assert case.request.runs[0].profile_snapshot is original
    assert (
        case.proposal.profile_descriptor.planner_agent_ref == original.planner_agent_ref
    )
    result = validate(case)
    assert result is (None if path == "initial" else case.proposal.payload)
    assert case.request.profile_snapshot is original
    assert case.request.runs[0].profile_snapshot is original
    assert case.request.status is PlanningRequestStatus.PROPOSAL_READY
    assert case.profile.planner_agent_ref.profile_id == DEFAULT_PLANNER_ID
    assert case.profile.planner_agent_ref.revision == 1


@pytest.mark.parametrize("path", PATHS)
def test_native_confirmation_does_not_refresh_saved_pin_to_later_default(
    path: ConfirmationPath,
) -> None:
    case = confirmation_case(path)
    original = case.request.profile_snapshot.planner_agent_ref
    assert original is not None
    current = replace(
        case.profile,
        planner_agent_ref=ProfileRef(
            workspace_id=original.workspace_id,
            profile_id=original.profile_id,
            revision=9,
        ),
    )
    result = validate(replace(case, profile=current))
    assert result is (None if path == "initial" else case.proposal.payload)
    assert case.request.profile_snapshot.planner_agent_ref is original
    assert current.planner_agent_ref is not None
    assert original.revision == 2 and current.planner_agent_ref.revision == 9


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "change",
    (
        "policy_id",
        "policy_version",
        "budget",
        "retry",
        "system_policy",
        "collaboration",
        "descriptor_planner",
        "proposal_content",
        "proposal_hash",
        "workspace",
    ),
)
def test_native_confirmation_preserves_original_policy_and_proposal_denials(
    path: ConfirmationPath, change: str
) -> None:
    case = confirmation_case(
        path, descriptor_uses_default=change == "descriptor_planner"
    )
    if change in {"policy_id", "policy_version"}:
        payload = dict(case.envelope.payload)
        payload["profile_id" if change == "policy_id" else "profile_version"] = (
            str(uuid4()) if change == "policy_id" else case.profile.version + 1
        )
        case = replace(case, envelope=replace(case.envelope, payload=payload))
    elif change == "budget":
        case = replace(
            case,
            profile=replace(
                case.profile,
                budget=replace(
                    case.profile.budget, max_tokens=case.profile.budget.max_tokens + 1
                ),
            ),
        )
    elif change == "retry":
        case = replace(
            case,
            profile=replace(
                case.profile,
                retry_policy=replace(
                    case.profile.retry_policy, max_retries=2, backoff_seconds=(1, 2)
                ),
            ),
        )
    elif change == "system_policy":
        case = replace(
            case, profile=replace(case.profile, system_policy={"changed_rule": True})
        )
    elif change == "collaboration":
        case = replace(
            case,
            profile=replace(
                case.profile, collaboration_runtime_binding=collaboration_binding()
            ),
        )
    elif change in {"proposal_content", "proposal_hash"}:
        changed_payload = replace(
            case.proposal.payload,
            root_task=replace(
                case.proposal.payload.root_task, title="Changed proposal content"
            ),
        )
        changed = ProposalRevision.create(
            planning_request_id=case.request.id,
            revision=case.proposal.revision,
            brief_revision=case.proposal.brief_revision,
            planning_run_id=case.proposal.planning_run_id,
            kind=case.request.kind,
            payload=changed_payload,
            profile_descriptor=case.proposal.profile_descriptor,
            profile_snapshot=case.request.profile_snapshot,
            created_at=NOW,
            derived_diff=RevisionDiffNormalizer().normalize(
                base_snapshot(), changed_payload
            )
            if isinstance(changed_payload, PlanningRevisionProposalPayloadV3)
            else None,
        )
        assert changed.content_hash != case.proposal.content_hash
        if change == "proposal_content":
            case = replace(case, proposal=changed)
        else:
            payload = dict(case.envelope.payload)
            payload[
                "proposal_content_hash"
                if path == "initial"
                else "expected_proposal_content_hash"
            ] = changed.content_hash
            case = replace(case, envelope=replace(case.envelope, payload=payload))
    elif change == "workspace":
        case = replace(case, envelope=replace(case.envelope, workspace_id=uuid4()))
    with pytest.raises(ValidationError):
        validate(case)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("pin", ("missing", "foreign"))
def test_native_confirmation_snapshot_rejects_invalid_pin_at_constructor(
    path: ConfirmationPath, pin: str
) -> None:
    case = confirmation_case(path)
    snapshot = case.request.profile_snapshot
    with pytest.raises(ValidationError):
        replace(
            snapshot,
            planner_agent_ref=None
            if pin == "missing"
            else ProfileRef(
                workspace_id=uuid4(), profile_id=SELECTED_PLANNER_ID, revision=2
            ),
        )
    assert case.request.profile_snapshot is snapshot


@pytest.mark.parametrize("path", PATHS)
def test_legacy_confirmation_still_validates_its_unchanged_snapshot(
    path: ConfirmationPath,
) -> None:
    case = confirmation_case(path, native=False)
    result = validate(case)
    assert result is (None if path == "initial" else case.proposal.payload)
    assert case.request.profile_snapshot.planner_agent_ref is None
    assert (
        case.request.profile_snapshot.planner_module_instance_id
        == case.profile.planner_module_instance_id
    )


@pytest.mark.parametrize("path", PATHS)
def test_legacy_confirmation_still_rejects_changed_policy_snapshot(
    path: ConfirmationPath,
) -> None:
    case = confirmation_case(path, native=False)
    changed = replace(case.profile, default_priority=case.profile.default_priority + 1)
    with pytest.raises(ValidationError):
        validate(replace(case, profile=changed))


@pytest.mark.parametrize(
    "field", ("expected_base_snapshot_id", "expected_base_content_hash")
)
def test_native_revision_confirmation_retains_original_base_denial(field: str) -> None:
    case = confirmation_case("revision")
    payload = dict(case.envelope.payload)
    payload[field] = (
        str(uuid4())
        if field == "expected_base_snapshot_id"
        else case.proposal.content_hash
    )
    with pytest.raises(ValidationError):
        validate(replace(case, envelope=replace(case.envelope, payload=payload)))
