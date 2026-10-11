from dataclasses import fields, replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sidera_agents.contracts import ProfileRef
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.domain.agent_bindings import AssigneeRef
from sidera_control_plane.domain.handoff_evidence import HandoffContextBundle
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
    NativeHandoffProposalPayload,
)
from sidera_control_plane.persistence.handoff_repository import (
    HandoffContextBundleRepository,
    HandoffProposalRepository,
    HandoffTransitionRepository,
)
from sidera_control_plane.persistence.models import (
    HandoffContextBundleModel,
    HandoffProposalModel,
    HandoffTransitionModel,
)
from tests.integration.test_handoff_repository import NOW, _bundle, _seed
from tests.support.native_agents import (
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)

pytestmark = pytest.mark.integration


def native_payload(legacy, *, human=False):
    values = {
        field.name: getattr(legacy, field.name)
        for field in fields(NativeHandoffProposalPayload)
        if hasattr(legacy, field.name)
    }
    profile = ProfileRef(
        workspace_id=legacy.workspace_id, profile_id=uuid4(), revision=1
    )
    values.update(
        proposal_schema_version=2,
        source_worker_item_version=1,
        source_run_id=uuid4(),
        source_authority_id=uuid4(),
        source_binding_version=1,
        source_profile_ref=profile,
        source_binding_target_type="worker_item",
        source_binding_target_id=legacy.worker_item_id,
        work_context_id=uuid4(),
        work_context_revision_id=uuid4(),
        work_context_hash="d" * 64,
        work_context_version=1,
        target_assignee=AssigneeRef("human", "integration")
        if human
        else (AssigneeRef("profile", profile.profile_id)),
        target_profile_ref=None if human else profile,
        impact_summary={
            **legacy.impact_summary,
            "worker_item_context_version": legacy.source_worker_context_version,
        },
    )
    return NativeHandoffProposalPayload(**values)


def native_history(database, workspace_id, *, human=False):
    seed = _seed(database, workspace_id, with_transition=True)
    old = replace(
        seed.transition,
        status=HandoffTransitionStatus.CONVERGED,
        phase=HandoffPhase.COMPLETE,
        writer_state=HandoffWriterState.TARGET_ACTIVE,
        completed_at=NOW,
        version=2,
    )
    with database.session() as session:
        HandoffTransitionRepository(session).save(old, previous_version=1)
        session.commit()
    proposal = HandoffProposal.create(
        id=uuid4(),
        payload=native_payload(seed.proposal.payload, human=human),
        created_by_type="user",
        created_by_id="integration",
        source_channel="web",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    proposal.mark_confirmed(now=NOW)
    transition = HandoffTransition.create(
        id=uuid4(),
        proposal=proposal,
        confirmed_by_type="user",
        confirmed_by_id="integration",
        confirmed_at=NOW,
    )
    return seed, old, proposal, transition


def native_bundle(seed, proposal, transition):
    legacy = _bundle(seed)
    values = {
        field.name: getattr(legacy, field.name)
        for field in fields(legacy)
        if field.name not in {"bundle_hash", "version"}
    }
    values.update(
        id=uuid4(),
        schema_version=2,
        handoff_transition_id=transition.id,
        target_worker_id=None,
        target_runtime_snapshot_hash=None,
        target_profile_ref=proposal.payload.target_profile_ref,
        target_binding_version=2,
        target_native_metadata={"execution_mode": "native_profile"},
    )
    return HandoffContextBundle.create(**values)


@pytest.mark.parametrize("human", [False, True])
def test_mixed_native_and_legacy_handoff_history_round_trips(
    database, workspace_id, human
):
    seed, old, proposal, transition = native_history(
        database, workspace_id, human=human
    )
    legacy_bundle = _bundle(seed)
    native = None if human else native_bundle(seed, proposal, transition)
    with database.session() as session:
        HandoffProposalRepository(session).add(proposal)
        session.flush()
        HandoffTransitionRepository(session).add(transition)
        session.flush()
        HandoffContextBundleRepository(session).add_append_only(legacy_bundle)
        if native is not None:
            HandoffContextBundleRepository(session).add_append_only(native)
        session.commit()
    with database.session() as session:
        proposals = HandoffProposalRepository(session)
        transitions = HandoffTransitionRepository(session)
        bundles = HandoffContextBundleRepository(session)
        assert proposals.get(seed.proposal.id, workspace_id) == seed.proposal
        assert proposals.get(proposal.id, workspace_id) == proposal
        assert transitions.get(old.id, workspace_id) == old
        assert transitions.get(transition.id, workspace_id) == transition
        assert bundles.get(legacy_bundle.id, workspace_id) == legacy_bundle
        if native is not None:
            assert bundles.get(native.id, workspace_id) == native


@pytest.mark.parametrize("kind", ["proposal", "transition"])
@pytest.mark.parametrize("bad_field", ["discriminator", "assignee", "float", "string"])
def test_native_json_variant_check_denies_single_invalid_field(
    database, workspace_id, kind, bad_field
):
    _, _, proposal, transition = native_history(database, workspace_id)
    with database.session() as session:
        HandoffProposalRepository(session).add(proposal)
        row = next(row for row in session.new if isinstance(row, HandoffProposalModel))
        if kind == "transition":
            session.flush()
            HandoffTransitionRepository(session).add(transition)
            row = next(
                row for row in session.new if isinstance(row, HandoffTransitionModel)
            )
        value = dict(proposal.payload.to_mapping())
        if bad_field == "discriminator":
            del value["proposal_schema_version"]
        elif bad_field == "assignee":
            del value["target_assignee"]
        else:
            value["proposal_schema_version"] = 2.0 if bad_field == "float" else "2"
        if kind == "proposal":
            row.payload = value
        else:
            row.native_payload = value
        with pytest.raises(IntegrityError) as error:
            session.flush()
        assert error.value.orig.diag.constraint_name == f"ck_handoff_{kind}s_variant"


@pytest.mark.parametrize("bad_field", ["profile", "metadata", "binding"])
def test_native_bundle_check_denies_single_missing_pin(
    database, workspace_id, bad_field
):
    seed, _, proposal, transition = native_history(database, workspace_id)
    with database.session() as session:
        HandoffProposalRepository(session).add(proposal)
        session.flush()
        HandoffTransitionRepository(session).add(transition)
        session.flush()
        HandoffContextBundleRepository(session).add_append_only(
            native_bundle(seed, proposal, transition)
        )
        row = next(
            row for row in session.new if isinstance(row, HandoffContextBundleModel)
        )
        if bad_field == "profile":
            row.target_profile_ref = {}
        elif bad_field == "metadata":
            row.target_native_metadata = {}
        else:
            row.target_binding_version = None
        with pytest.raises(IntegrityError) as error:
            session.flush()
        assert (
            error.value.orig.diag.constraint_name
            == "ck_handoff_context_bundles_variant"
        )
