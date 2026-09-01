import importlib.util
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[2]
LEGACY_REVISION = "0019_r6_2_agent_handoff"
REVISION = "0020_r6_2_workspace_transfer_outcome"
PREVIOUS_REVISION = "0018_r6_1_execution_request_lineage"


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    """Keep migration rehearsal isolated from the shared integration fixture."""
    yield


@pytest.fixture
def isolated_database_url(database_url: str) -> Iterator[str]:
    schema = f"r62_test_{uuid4().hex}"
    base_url = make_url(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema}"
    isolated_url = base_url.set(query=query).render_as_string(hide_password=False)
    try:
        yield isolated_url
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@contextmanager
def _migration_environment(database_url: str) -> Iterator[None]:
    previous = os.environ.get("SIDERA_DATABASE_URL")
    os.environ["SIDERA_DATABASE_URL"] = database_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SIDERA_DATABASE_URL", None)
        else:
            os.environ["SIDERA_DATABASE_URL"] = previous


def _config() -> Config:
    return Config(str(CONTROL_PLANE_ROOT / "alembic.ini"))


def _upgrade(database_url: str, revision: str) -> None:
    with _migration_environment(database_url):
        command.upgrade(_config(), revision)


def _downgrade(database_url: str, revision: str) -> None:
    with _migration_environment(database_url):
        command.downgrade(_config(), revision)


def _current(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _migration_module(revision: str = LEGACY_REVISION):
    path = CONTROL_PLANE_ROOT / "migrations" / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load migration from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0019_has_0018_as_down_revision() -> None:
    assert _migration_module().down_revision == PREVIOUS_REVISION


def test_head_round_trip_on_isolated_empty_schema(isolated_database_url: str) -> None:
    _upgrade(isolated_database_url, REVISION)
    assert _current(isolated_database_url) == REVISION

    _downgrade(isolated_database_url, PREVIOUS_REVISION)
    assert _current(isolated_database_url) == PREVIOUS_REVISION


def test_head_upgrades_r6_1_fixture_without_backfill(
    isolated_database_url: str,
) -> None:
    _upgrade(isolated_database_url, PREVIOUS_REVISION)
    _upgrade(isolated_database_url, REVISION)

    engine = create_engine(isolated_database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM handoff_transitions")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_head_exposes_handoff_tables_columns_and_constraints(
    isolated_database_url: str,
) -> None:
    _upgrade(isolated_database_url, REVISION)
    engine = create_engine(isolated_database_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            assert {
                "handoff_proposals",
                "handoff_transitions",
                "handoff_context_bundles",
                "handoff_recovery_decisions",
                "execution_workspace_transfers",
            } <= tables

            assert {"handoff_transition_id"} <= {
                item["name"] for item in inspector.get_columns("executions")
            }
            assert {"handoff_transition_id"} <= {
                item["name"] for item in inspector.get_columns("execution_requests")
            }
            assert {
                "source_input_id",
                "handoff_transition_id",
            } <= {item["name"] for item in inspector.get_columns("execution_inputs")}

            assert {
                "uq_handoff_proposals_prepared_worker_item",
                "uq_handoff_transitions_active_worker_item",
                "uq_handoff_transitions_active_source_session",
                "uq_execution_requests_proposal_worker_item",
                "uq_execution_requests_handoff_transition",
                "uq_execution_inputs_handoff_source",
                "uq_execution_workspaces_active_location",
            } <= {
                item["name"]
                for table in (
                    "handoff_proposals",
                    "handoff_transitions",
                    "execution_requests",
                    "execution_inputs",
                    "execution_workspaces",
                )
                for item in inspector.get_indexes(table)
            }

            assert {
                "trg_handoff_proposals_immutable_payload",
                "trg_handoff_context_bundles_immutable_payload",
                "trg_execution_workspace_transfers_immutable_provenance",
                "trg_handoff_recovery_decisions_append_only",
            } <= {
                row["tgname"]
                for row in connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE NOT tgisinternal "
                        "AND tgname LIKE 'trg_%'"
                    )
                ).mappings()
            }
    finally:
        engine.dispose()
