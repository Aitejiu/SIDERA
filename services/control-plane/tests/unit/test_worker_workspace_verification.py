from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    Uuid,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import Session

from sidera_control_plane.persistence import worker_workspace_query_store as query_store
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionRequestModel,
)

WORKSPACE_ID = UUID(int=101)
WORKER_ITEM_ID = UUID(int=102)
ROOT_TASK_ID = UUID(int=103)
LINEAGE_ID = UUID(int=104)
EXECUTION_ID = UUID(int=105)


@pytest.fixture
def verification_database() -> Iterator[tuple[Session, Table, Table]]:
    metadata = MetaData()
    executions = Table(
        ExecutionModel.__tablename__,
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("workspace_id", Uuid(as_uuid=True), nullable=False),
        Column("worker_item_id", Uuid(as_uuid=True), nullable=False),
        Column("lineage_id", Uuid(as_uuid=True), nullable=False),
    )
    requests = Table(
        ExecutionRequestModel.__tablename__,
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("workspace_id", Uuid(as_uuid=True), nullable=False),
        Column("worker_item_id", Uuid(as_uuid=True), nullable=False),
        Column("root_task_id", Uuid(as_uuid=True), nullable=False),
        Column("execution_lineage_id", Uuid(as_uuid=True)),
        Column("result_derivation_kind", String),
        Column("source_candidate_id", Uuid(as_uuid=True)),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        metadata.create_all(engine)
        with engine.connect() as connection, Session(bind=connection) as session:
            yield session, executions, requests
    finally:
        engine.dispose()


def _execution_row(**changes: Any) -> dict[str, Any]:
    return {
        "id": EXECUTION_ID,
        "workspace_id": WORKSPACE_ID,
        "worker_item_id": WORKER_ITEM_ID,
        "lineage_id": LINEAGE_ID,
        **changes,
    }


def _request_row(**changes: Any) -> dict[str, Any]:
    return {
        "id": UUID(int=106),
        "workspace_id": WORKSPACE_ID,
        "worker_item_id": WORKER_ITEM_ID,
        "root_task_id": ROOT_TASK_ID,
        "execution_lineage_id": LINEAGE_ID,
        "result_derivation_kind": "verification",
        "source_candidate_id": UUID(int=107),
        **changes,
    }


def _classify(session: Session, execution_ids: set[UUID]) -> set[UUID]:
    classifier = getattr(query_store, "_result_verification_execution_ids", None)
    assert callable(classifier), "Persisted result-verification classifier is missing"
    return classifier(
        session, WORKSPACE_ID, WORKER_ITEM_ID, ROOT_TASK_ID, execution_ids
    )


def test_verification_query_reads_first_and_retry_once_without_mutating_rows(
    verification_database: tuple[Session, Table, Table],
) -> None:
    session, executions, requests = verification_database
    retry_id, outside_id, ordinary_id = UUID(int=108), UUID(int=109), UUID(int=110)
    session.execute(
        executions.insert(),
        [
            _execution_row(),
            _execution_row(id=retry_id),
            _execution_row(id=outside_id),
            _execution_row(id=ordinary_id, lineage_id=UUID(int=111)),
        ],
    )
    session.execute(requests.insert(), [_request_row()])
    before = (
        session.execute(select(executions).order_by(executions.c.id)).all(),
        session.execute(select(requests).order_by(requests.c.id)).all(),
    )
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", record_statement)
    try:
        classified = _classify(session, {EXECUTION_ID, retry_id, ordinary_id})
    finally:
        event.remove(connection, "before_cursor_execute", record_statement)

    assert classified == {EXECUTION_ID, retry_id}
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT ")
    assert before == (
        session.execute(select(executions).order_by(executions.c.id)).all(),
        session.execute(select(requests).order_by(requests.c.id)).all(),
    )


@pytest.mark.parametrize(
    "request_changes",
    (
        {"result_derivation_kind": None, "source_candidate_id": None},
        {"result_derivation_kind": "rework"},
        {"result_derivation_kind": "reintegration"},
        {"result_derivation_kind": "Verification"},
        {"execution_lineage_id": None},
        {"execution_lineage_id": UUID(int=112)},
        {"workspace_id": UUID(int=113)},
        {"worker_item_id": UUID(int=114)},
        {"root_task_id": UUID(int=115)},
        {"source_candidate_id": None},
    ),
    ids=(
        "ordinary",
        "rework",
        "reintegration",
        "kind_must_match_exactly",
        "unmaterialized",
        "unmatched_lineage",
        "foreign_workspace",
        "foreign_worker_item",
        "foreign_root_task",
        "missing_source_candidate",
    ),
)
def test_non_verification_or_foreign_request_is_not_classified(
    verification_database: tuple[Session, Table, Table],
    request_changes: dict[str, Any],
) -> None:
    session, executions, requests = verification_database
    session.execute(executions.insert(), [_execution_row()])
    session.execute(requests.insert(), [_request_row(**request_changes)])

    assert _classify(session, {EXECUTION_ID}) == set()


@pytest.mark.parametrize(
    "execution_changes",
    ({"workspace_id": UUID(int=113)}, {"worker_item_id": UUID(int=114)}),
    ids=("foreign_workspace", "foreign_worker_item"),
)
def test_foreign_execution_is_not_classified_by_a_matching_lineage(
    verification_database: tuple[Session, Table, Table],
    execution_changes: dict[str, Any],
) -> None:
    session, executions, requests = verification_database
    session.execute(executions.insert(), [_execution_row(**execution_changes)])
    session.execute(requests.insert(), [_request_row()])

    assert _classify(session, {EXECUTION_ID}) == set()


def test_execution_without_a_persisted_request_remains_unclassified(
    verification_database: tuple[Session, Table, Table],
) -> None:
    session, executions, _requests = verification_database
    session.execute(executions.insert(), [_execution_row()])

    assert _classify(session, {EXECUTION_ID}) == set()


def test_empty_execution_set_does_not_execute_sql(
    verification_database: tuple[Session, Table, Table],
) -> None:
    session, _executions, _requests = verification_database
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", record_statement)
    try:
        assert _classify(session, set()) == set()
    finally:
        event.remove(connection, "before_cursor_execute", record_statement)

    assert statements == []


def test_execution_mapping_defaults_to_ordinary_and_preserves_classification() -> None:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    row = ExecutionModel(
        id=EXECUTION_ID,
        workspace_id=WORKSPACE_ID,
        task_id=UUID(int=116),
        worker_item_id=WORKER_ITEM_ID,
        lineage_id=LINEAGE_ID,
        attempt=1,
        purpose="verification",
        status="running",
        failure_code=None,
        assigned_worker_id=None,
        pinned_worker_id=None,
        active_lease_id=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=None,
        version=1,
        capability_requirements=["verification"],
        adapter_preferences=[],
        adapter_forbidden=[],
    )

    ordinary = query_store._execution(row, None)

    assert getattr(ordinary, "is_result_verification", None) is False
    classified = query_store._execution(
        row, None, is_result_verification=True, native_binding_version=3
    )
    assert classified.is_result_verification is True
    assert classified.native_binding_version == 3
