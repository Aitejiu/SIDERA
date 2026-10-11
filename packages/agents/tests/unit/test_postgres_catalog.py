from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

from sidera_agents.contracts import ResourceRef
from sidera_agents.postgres.store import (
    PostgresAgentStore,
    _profile_from_row,
    _resource_from_row,
)


class _RecordingResult:
    def mappings(self):
        return self

    def one_or_none(self):
        return {"enabled": True}

    def first(self):
        return (1,)


class _RecordingConnection:
    def __init__(self):
        self.locked_resource_ids = []

    def execute(self, statement):
        if getattr(statement, "_for_update_arg", None) is not None:
            compiled = statement.compile(dialect=postgresql.dialect())
            resource_id = next(
                value
                for key, value in compiled.params.items()
                if key.startswith("resource_id")
            )
            self.locked_resource_ids.append(resource_id)
        return _RecordingResult()


def _profile_row(catalog_lab):
    return {
        "workspace_id": catalog_lab.workspace_id,
        "profile_id": UUID(int=100),
        "revision": 1,
        "draft": catalog_lab.draft.model_dump(mode="json"),
        "config_hash": "sha256:" + "a" * 64,
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
    }


def _resource_row(catalog_lab):
    return {
        "workspace_id": catalog_lab.workspace_id,
        "resource_id": UUID(int=200),
        "revision": 1,
        "draft": catalog_lab.skill_draft.model_dump(mode="json"),
        "content_hash": "sha256:" + "b" * 64,
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
    }


@pytest.mark.parametrize(
    ("mapper", "row_factory"),
    [(_profile_from_row, _profile_row), (_resource_from_row, _resource_row)],
)
@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        (
            datetime(2026, 10, 1, 8, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 10, 1, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 30, 19, tzinfo=timezone(-timedelta(hours=5))),
            datetime(2026, 10, 1, tzinfo=UTC),
        ),
    ],
)
def test_row_mappers_normalize_aware_offsets_without_changing_instant(
    catalog_lab, mapper, row_factory, stamp, expected
):
    row = row_factory(catalog_lab)
    row["created_at"] = stamp

    assert mapper(row).created_at == expected


@pytest.mark.parametrize(
    ("mapper", "row_factory"),
    [(_profile_from_row, _profile_row), (_resource_from_row, _resource_row)],
)
def test_row_mappers_reject_naive_timestamps(catalog_lab, mapper, row_factory):
    row = row_factory(catalog_lab)
    row["created_at"] = datetime(2026, 10, 1)  # noqa: DTZ001 - exercise naive rejection

    with pytest.raises(ValueError):
        mapper(row)


def test_reversed_refs_use_canonical_lock_order_without_reordering_refs():
    workspace_id = UUID("10000000-0000-0000-0000-000000000001")
    refs = tuple(
        ResourceRef(workspace_id=workspace_id, resource_id=UUID(int=index), revision=1)
        for index in (1, 2, 3)
    )
    reversed_refs = tuple(reversed(refs))
    store = PostgresAgentStore(cast(Engine, object()))
    reverse_connection = _RecordingConnection()
    forward_connection = _RecordingConnection()

    store._require_enabled_resources(reverse_connection, workspace_id, reversed_refs)
    store._require_enabled_resources(forward_connection, workspace_id, refs)

    assert reverse_connection.locked_resource_ids == [ref.resource_id for ref in refs]
    assert (
        reverse_connection.locked_resource_ids == forward_connection.locked_resource_ids
    )
    assert reversed_refs == tuple(reversed(refs))
