from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore


class _Result:
    rowcount = 1


class _Session:
    def __init__(self) -> None:
        self.statement = None
        self.commits = 0

    def execute(self, statement):
        self.statement = statement
        return _Result()

    def commit(self) -> None:
        self.commits += 1


class _Database:
    def __init__(self) -> None:
        self.session_value = _Session()

    def session(self):
        database = self

        class _Context:
            def __enter__(self):
                return database.session_value

            def __exit__(self, *_args) -> None:
                return None

        return _Context()


def test_renew_claim_extends_a_live_claim_only() -> None:
    database = _Database()
    store = SqlAlchemyOutboxStore(
        database, claim_seconds=3, clock=lambda: datetime(2026, 9, 18, tzinfo=UTC)
    )
    message_id = uuid4()
    claim_token = uuid4()

    assert store.renew_claim(message_id, claim_token) is True

    statement = database.session_value.statement
    assert statement is not None
    params = statement.compile().params
    assert params["claimed_until"] == datetime(2026, 9, 18, 0, 0, 3, tzinfo=UTC)
    assert database.session_value.commits == 1
