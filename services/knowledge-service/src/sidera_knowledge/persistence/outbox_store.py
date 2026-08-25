from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import or_, select

from sidera_knowledge.application.outbox_dispatcher import OutboxMessage
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.models import OutboxMessageModel


class OutboxLeaseLost(RuntimeError):
    """Raised when a worker tries to update a message after losing its lease."""


def _row_to_message(row: OutboxMessageModel) -> OutboxMessage:
    if row.created_at is None or row.lease_id is None:
        raise RuntimeError("claimed outbox row is missing lease metadata")
    return OutboxMessage(
        message_id=row.id,
        event_id=row.event_id,
        workspace_id=row.workspace_id,
        topic=row.topic,
        payload=cast(dict[str, object], dict(row.payload)),
        attempt=row.publish_attempts,
        created_at=row.created_at,
        lease_id=row.lease_id,
    )


class PostgresOutboxStore:
    def __init__(self, database: Database, workspace_id: UUID | None = None) -> None:
        self._database = database
        self._workspace_id = workspace_id

    def claim_batch(
        self,
        *,
        now: datetime,
        lease_id: UUID,
        lease_until: datetime,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._database.session() as session:
            statement = (
                select(OutboxMessageModel)
                .where(
                    OutboxMessageModel.published_at.is_(None),
                    OutboxMessageModel.dead_lettered_at.is_(None),
                    OutboxMessageModel.available_at <= now,
                    or_(
                        OutboxMessageModel.lease_id.is_(None),
                        OutboxMessageModel.leased_until.is_(None),
                        OutboxMessageModel.leased_until < now,
                    ),
                )
                .order_by(OutboxMessageModel.created_at, OutboxMessageModel.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            if self._workspace_id is not None:
                statement = statement.where(
                    OutboxMessageModel.workspace_id == self._workspace_id
                )
            rows = session.scalars(statement).all()
            for row in rows:
                row.lease_id = lease_id
                row.leased_until = lease_until
                row.publish_attempts += 1
            session.commit()
            return tuple(_row_to_message(row) for row in rows)

    def mark_published(
        self, *, message_id: UUID, lease_id: UUID, published_at: datetime
    ) -> None:
        with self._database.session() as session:
            row = session.scalar(
                select(OutboxMessageModel)
                .where(
                    OutboxMessageModel.id == message_id,
                    OutboxMessageModel.lease_id == lease_id,
                    OutboxMessageModel.published_at.is_(None),
                    OutboxMessageModel.dead_lettered_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                raise OutboxLeaseLost("outbox lease is no longer valid")
            row.published_at = published_at
            row.lease_id = None
            row.leased_until = None
            session.commit()

    def mark_failed(
        self,
        *,
        message_id: UUID,
        lease_id: UUID,
        available_at: datetime,
        error_code: str,
        dead_lettered_at: datetime | None,
    ) -> None:
        with self._database.session() as session:
            row = session.scalar(
                select(OutboxMessageModel)
                .where(
                    OutboxMessageModel.id == message_id,
                    OutboxMessageModel.lease_id == lease_id,
                    OutboxMessageModel.published_at.is_(None),
                    OutboxMessageModel.dead_lettered_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                raise OutboxLeaseLost("outbox lease is no longer valid")
            row.available_at = available_at
            row.last_error_code = error_code
            row.dead_lettered_at = dead_lettered_at
            row.lease_id = None
            row.leased_until = None
            session.commit()
