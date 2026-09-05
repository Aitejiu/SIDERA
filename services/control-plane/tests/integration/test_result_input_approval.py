from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update

from sidera_control_plane.application.collaboration_queries import (
    CollaborationQueryService,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.persistence.collaboration_query_store import (
    SqlAlchemyCollaborationQueryStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CollaborationReleaseProposalModel,
)

from .result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database: Database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def test_late_unselected_source_keeps_pending_approval_valid(
    scenario: ResultInputScenario,
) -> None:
    scenario.confirm_plan()
    scenario.pause_release("summary")
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()

    proposal = scenario.pending_proposal("summary")
    version = scenario.request_version("summary")
    assert proposal.input_selection_id is not None
    assert proposal.input_selection_hash is not None
    proposal_view = CollaborationQueryService(
        SqlAlchemyCollaborationQueryStore(scenario.database)
    ).get_governance_proposal(scenario.workspace_id, proposal.id)
    assert proposal_view.input_selection_id == proposal.input_selection_id
    assert proposal_view.input_selection_hash == proposal.input_selection_hash

    scenario.complete_source("a", body=b"late-a\n")
    scenario.reconcile()

    assert scenario.pending_proposal("summary").id == proposal.id
    assert scenario.request_version("summary") == version

    scenario.approve(proposal)

    assert scenario.release_count("summary") == 1
    assert scenario.selection_count("summary") == 1
    selection = scenario.selection("summary")
    assert selection.id == proposal.input_selection_id
    assert selection.selection_hash == proposal.input_selection_hash
    assert tuple(source.worker_item_key for source in selection.groups[0].sources) == (
        "b",
        "c",
    )


def _pending_scenario(scenario: ResultInputScenario):
    scenario.confirm_plan()
    scenario.pause_release("summary")
    scenario.complete_source("b", body=b"report-b\n")
    scenario.complete_source("c", body=b"report-c\n")
    scenario.reconcile()
    return scenario.pending_proposal("summary")


@pytest.mark.parametrize(
    ("include_selection", "selection_hash", "expected_code"),
    [
        (False, None, "collaboration_governance_selection_expected"),
        (True, "f" * 64, "collaboration_governance_selection_conflict"),
    ],
)
def test_v2_approval_requires_the_exact_reviewed_selection(
    scenario: ResultInputScenario,
    include_selection: bool,
    selection_hash: str | None,
    expected_code: str,
) -> None:
    proposal = _pending_scenario(scenario)
    command = scenario.approval_command(
        proposal,
        include_selection=include_selection,
        selection_hash=selection_hash,
    )

    with pytest.raises(DomainError) as caught:
        scenario.governance_service().handle(command)

    assert caught.value.code == expected_code
    assert scenario.release_count("summary") == 0
    assert scenario.pending_proposal("summary").id == proposal.id


def test_v2_approval_command_replay_is_exactly_once(
    scenario: ResultInputScenario,
) -> None:
    proposal = _pending_scenario(scenario)
    command = scenario.approval_command(proposal)
    service = scenario.governance_service()

    result = service.handle(command)
    replay = service.handle(command)

    assert result == replay
    assert scenario.release_count("summary") == 1
    assert scenario.selection_count("summary") == 1


def test_changed_selected_source_stales_old_proposal_and_preserves_ledger(
    scenario: ResultInputScenario,
) -> None:
    proposal = _pending_scenario(scenario)
    old_selection_id = proposal.input_selection_id
    assert old_selection_id is not None
    assert scenario.selection_count("summary") == 1
    with scenario.uow_factory() as uow:
        stored_selection = uow.input_selections.get(
            scenario.workspace_id, old_selection_id
        )
        assert stored_selection is not None
        artifact_id = stored_selection.groups[0].sources[0].artifacts[0].artifact_id
    with scenario.database.session() as session:
        session.execute(
            update(ArtifactModel)
            .where(ArtifactModel.id == artifact_id)
            .values(content_hash="f" * 64)
        )
        session.commit()

    result = scenario.approve(proposal)

    assert result.data["result"] == "stale"
    assert result.data["stale_reason"] == "input_selection_source_stale"
    assert scenario.release_count("summary") == 0
    assert scenario.selection_count("summary") == 1
    with scenario.database.session() as session:
        old_status = session.scalar(
            select(CollaborationReleaseProposalModel.status).where(
                CollaborationReleaseProposalModel.id == proposal.id
            )
        )
    assert old_status == "stale"

    scenario.complete_source("a", body=b"report-a\n")
    scenario.reconcile()
    replacement = scenario.pending_proposal("summary")
    assert replacement.id != proposal.id
    assert replacement.input_selection_id != old_selection_id
    assert scenario.selection_count("summary") == 2


def test_v2_rejection_command_replay_does_not_release(
    scenario: ResultInputScenario,
) -> None:
    proposal = _pending_scenario(scenario)
    command = scenario.rejection_command(proposal)
    service = scenario.governance_service()

    result = service.handle(command)
    replay = service.handle(command)

    assert result == replay
    assert result.data["proposal_status"] == "rejected"
    assert scenario.release_count("summary") == 0
    assert scenario.selection_count("summary") == 1


def test_concurrent_v2_approvals_create_one_aligned_release(
    scenario: ResultInputScenario,
) -> None:
    proposal = _pending_scenario(scenario)
    commands = (
        scenario.approval_command(proposal),
        scenario.approval_command(proposal),
    )

    def approve(command):
        try:
            return scenario.governance_service().handle(command)
        except DomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(approve, commands))

    assert sum(not isinstance(item, DomainError) for item in outcomes) == 1
    assert scenario.release_count("summary") == 1
    assert scenario.release_selection_ref("summary") == scenario.request_selection_ref(
        "summary"
    )


def test_resume_and_v2_approval_race_converges_to_one_aligned_release(
    scenario: ResultInputScenario,
) -> None:
    proposal = _pending_scenario(scenario)
    commands = (
        scenario.approval_command(proposal),
        scenario.resume_command(proposal),
    )

    def apply(command):
        try:
            return scenario.governance_service().handle(command)
        except DomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(apply, commands))

    scenario.reconcile()

    assert scenario.release_count("summary") == 1
    assert scenario.release_selection_ref("summary") == scenario.request_selection_ref(
        "summary"
    )
