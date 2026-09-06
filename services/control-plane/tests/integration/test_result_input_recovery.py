from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from sidera_control_plane.application.worker_gateway import (
    GatewayRecoveryRequired,
)
from sidera_control_plane.persistence.models import (
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    SchedulingBlockModel,
)

from .result_input_scenario import ResultInputScenario

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(
    database, workspace_id: UUID, tmp_path: Path
) -> ResultInputScenario:
    return ResultInputScenario(database, workspace_id, tmp_path)


def _prepare_released_context(
    scenario: ResultInputScenario,
) -> tuple[object, UUID, str]:
    scenario.run_to_released_summary()
    selected = scenario.selection("summary")
    artifact_id = selected.groups[0].sources[0].artifacts[0].artifact_id
    selection_hash = selected.selection_hash
    scenario.activate()
    package = scenario.assemble_context("summary")
    scenario.complete_source("a", body=b"report-a\n")
    return package, artifact_id, selection_hash


def _audit_rows(scenario: ResultInputScenario, package_id: UUID):
    with scenario.database.session() as session:
        return tuple(
            session.scalars(
                select(ContextPackageMaterializationAuditModel)
                .where(
                    ContextPackageMaterializationAuditModel.workspace_id
                    == scenario.workspace_id,
                    ContextPackageMaterializationAuditModel.package_id == package_id,
                )
                .order_by(ContextPackageMaterializationAuditModel.attempt.asc())
            ).all()
        )


def test_missing_body_does_not_replace_released_selection(
    scenario: ResultInputScenario,
) -> None:
    package, artifact_id, selection_hash = _prepare_released_context(scenario)

    scenario.make_artifact_unavailable(artifact_id)
    result = scenario.start_worker("summary")

    assert getattr(result, "code", None) == "result_input_unavailable"
    assert scenario.start_call_count("summary") == 0
    assert scenario.selection("summary").selection_hash == selection_hash
    assert scenario.release_count("summary") == 1
    assert scenario.package_ids("summary") == (package.package_id,)
    audits = _audit_rows(scenario, package.package_id)
    assert [(row.attempt, row.outcome, row.failure_code) for row in audits] == [
        (1, "failed", "result_input_unavailable")
    ]


def test_restoring_same_body_retries_delivery_and_resolves_attention(
    scenario: ResultInputScenario,
) -> None:
    package, artifact_id, selection_hash = _prepare_released_context(scenario)
    scenario.make_artifact_unavailable(artifact_id)

    failed = scenario.start_worker("summary")
    assert getattr(failed, "code", None) == "result_input_unavailable"

    scenario.restore_artifact(artifact_id)
    started = scenario.start_worker("summary")

    assert getattr(started, "session_id", None) is not None
    assert scenario.start_call_count("summary") == 1
    assert scenario.selection("summary").selection_hash == selection_hash
    assert scenario.release_count("summary") == 1
    assert scenario.package_ids("summary") == (package.package_id,)
    audits = _audit_rows(scenario, package.package_id)
    assert [(row.attempt, row.outcome, row.failure_code) for row in audits] == [
        (1, "failed", "result_input_unavailable"),
        (2, "succeeded", None),
    ]
    with scenario.database.session() as session:
        assert (
            session.scalar(
                select(SchedulingBlockModel).where(
                    SchedulingBlockModel.workspace_id == scenario.workspace_id,
                    SchedulingBlockModel.execution_id
                    == scenario.latest_execution_id("summary"),
                    SchedulingBlockModel.status == "active",
                )
            )
            is None
        )


def test_corrupt_body_quarantines_package_and_restore_does_not_revive_it(
    scenario: ResultInputScenario,
) -> None:
    package, artifact_id, selection_hash = _prepare_released_context(scenario)
    scenario.corrupt_artifact(artifact_id, body=b"tampered-result\n")

    result = scenario.start_worker("summary")

    assert getattr(result, "code", None) == "result_input_integrity_failed"
    assert scenario.start_call_count("summary") == 0
    assert scenario.selection("summary").selection_hash == selection_hash
    assert scenario.release_count("summary") == 1
    with scenario.database.session() as session:
        stored = session.scalar(
            select(ContextPackageModel).where(
                ContextPackageModel.workspace_id == scenario.workspace_id,
                ContextPackageModel.id == package.package_id,
            )
        )
        assert stored is not None
        assert stored.status == "corrupt"

    scenario.restore_artifact(artifact_id)
    with pytest.raises(Exception, match="corrupt"):
        scenario.start_worker("summary")
    assert scenario.start_call_count("summary") == 0


def test_retry_context_reassembles_blocked_execution_without_new_selection(
    scenario: ResultInputScenario,
) -> None:
    scenario.run_to_released_summary()
    selection_hash = scenario.selection("summary").selection_hash
    scenario.activate()
    scenario.mutate_context_assembly_budget(
        "summary",
        lambda budget: budget.update({"execution_context_inheritance": "invalid"}),
    )

    blocked = scenario.run_context_worker("summary")
    assert blocked.ready == 3
    assert blocked.blocked == 1
    scenario.mutate_context_assembly_budget(
        "summary",
        lambda budget: budget.pop("execution_context_inheritance", None),
    )
    retry = scenario.retry_context("summary")
    assert retry.data["status"] == "queued"
    ready = scenario.run_context_worker("summary")
    assert ready.ready == 1
    assert ready.blocked == 0

    scenario.assemble_context("summary")
    scenario.complete_source("a", body=b"report-a\n")
    scenario.start_worker("summary")
    assert scenario.selection("summary").selection_hash == selection_hash
    assert scenario.release_count("summary") == 1


def test_ack_before_start_recovery_does_not_duplicate_start(
    scenario: ResultInputScenario,
) -> None:
    package, _artifact_id, _selection_hash = _prepare_released_context(scenario)

    with pytest.raises(RuntimeError, match="after context acknowledgement"):
        scenario.start_worker("summary", crash_after_context_ack=True)
    assert scenario.start_call_count("summary") == 0

    started = scenario.start_worker("summary")

    assert getattr(started, "session_id", None) is not None
    assert scenario.start_call_count("summary") == 1
    assert scenario.package_ids("summary") == (package.package_id,)


def test_unknown_start_is_not_replayed_after_restart(
    scenario: ResultInputScenario,
) -> None:
    _prepare_released_context(scenario)

    with pytest.raises(GatewayRecoveryRequired):
        scenario.start_worker("summary", start_outcome_unknown=True)
    assert scenario.start_call_count("summary") == 1

    with pytest.raises(GatewayRecoveryRequired):
        scenario.start_worker("summary")
    assert scenario.start_call_count("summary") == 1
