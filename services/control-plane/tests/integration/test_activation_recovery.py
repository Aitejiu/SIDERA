from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sidera_control_plane.api.app import create_app
from sidera_control_plane.application.activation_reconciler import (
    ActivationReconciler,
    ActivationReconcileResult,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionRequestModel,
    ProposalMaterializationModel,
    TaskModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_activation_reconciler import _materialize_plan
from tests.integration.test_proposal_confirmation import command, seed_proposal

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC) + timedelta(minutes=5)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _reconciler(uow_factory) -> ActivationReconciler:
    return ActivationReconciler(
        uow_factory=uow_factory,
        owner_id="activation-recovery-test",
        clock=lambda: NOW,
    )


def test_execution_insert_failure_rolls_back_and_restart_recovers(
    client: TestClient,
    database,
    workspace_id: UUID,
    uow_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _materialize_plan(client, uow_factory, workspace_id)
    original_flush = SqlAlchemyUnitOfWork.flush
    failure_injected = False

    def fail_after_execution_insert(uow: SqlAlchemyUnitOfWork) -> None:
        nonlocal failure_injected
        original_flush(uow)
        if not failure_injected:
            failure_injected = True
            raise RuntimeError("injected activation flush failure")

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "flush", fail_after_execution_insert)
    failed = _reconciler(uow_factory).run_once(limit=10)
    assert failed.examined == 1
    assert failed.materialized == 0
    assert failed.failed == 1

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
        request = session.scalar(select(ExecutionRequestModel))
        assert request is not None
        assert request.status == "ready"

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "flush", original_flush)
    recovered = _reconciler(uow_factory).run_once(limit=10)
    assert recovered.materialized == 1
    assert recovered.failed == 0
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 1
        assert session.scalar(select(func.count()).select_from(ProposalMaterializationModel)) == 1


def test_two_reconcilers_claim_one_ready_request(
    client: TestClient,
    database_url: str,
    database,
    workspace_id: UUID,
    uow_factory,
) -> None:
    request_id, profile_id, content_hash, _proposal_id = seed_proposal(
        uow_factory, workspace_id
    )
    response = client.post(
        "/api/v1/commands",
        json=command(workspace_id, request_id, profile_id, content_hash),
    )
    assert response.status_code == 201, response.text

    def run_once() -> ActivationReconcileResult:
        from sidera_control_plane.persistence.database import Database

        database_instance = Database(database_url)
        try:
            return _reconciler(
                lambda: SqlAlchemyUnitOfWork(database_instance)
            ).run_once(limit=10)
        finally:
            database_instance.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: run_once(), (1, 2)))

    assert sorted(result.materialized for result in results) == [0, 1]
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 1
        assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
