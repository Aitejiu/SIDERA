import pytest
from sqlalchemy import inspect

from sidera_control_plane.persistence.database import Database

pytestmark = pytest.mark.integration


def test_foundation_migration_creates_authoritative_tables(database: Database) -> None:
    with database.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())

    assert {
        "workspaces",
        "tasks",
        "commands",
        "events",
        "outbox_messages",
    } <= tables
