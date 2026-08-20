from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.dml import Update

from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import OutboxMessageModel


class SqlAlchemyOutboxStore:
    def __init__(self, database: Database, *, claim_seconds: int = 30) -> None:
        self._database = database
        self._claim_seconds = claim_seconds

    def claim(
        self,
        batch_size: int,
        *,
        topics: Collection[str] | None = None,
    ) -> tuple[OutboxMessage, ...]:
        now = datetime.now(UTC)
        claimed_until = now + timedelta(seconds=self._claim_seconds)
        with self._database.session() as session:
            statement = select(OutboxMessageModel).where(
                OutboxMessageModel.published_at.is_(None),
                OutboxMessageModel.next_attempt_at <= now,
                or_(
                    OutboxMessageModel.claimed_until.is_(None),
                    OutboxMessageModel.claimed_until < now,
                ),
            )
            if topics:
                statement = statement.where(OutboxMessageModel.topic.in_(tuple(topics)))
            rows = session.scalars(
                statement
                .order_by(OutboxMessageModel.created_at, OutboxMessageModel.id)
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            ).all()
            messages = []
            for row in rows:
                claim_token = uuid4()
                row.claim_token = claim_token
                row.claimed_until = claimed_until
                row.attempts += 1
                messages.append(
                    OutboxMessage(
                        id=row.id,
                        event_id=row.event_id,
                        topic=row.topic,
                        payload=dict(row.payload),
                        attempts=row.attempts,
                        claim_token=claim_token,
                    )
                )
            session.commit()
        return tuple(messages)

    def acknowledge(self, message_id: UUID, claim_token: UUID) -> bool:
        statement = (
            update(OutboxMessageModel)
            .where(
                OutboxMessageModel.id == message_id,
                OutboxMessageModel.claim_token == claim_token,
                OutboxMessageModel.published_at.is_(None),
            )
            .values(
                published_at=datetime.now(UTC),
                claimed_until=None,
                claim_token=None,
                last_error=None,
            )
        )
        return self._execute_fenced(statement)

    def reschedule(
        self,
        message_id: UUID,
        claim_token: UUID,
        next_attempt_at: datetime,
        error: str,
    ) -> bool:
        statement = (
            update(OutboxMessageModel)
            .where(
                OutboxMessageModel.id == message_id,
                OutboxMessageModel.claim_token == claim_token,
                OutboxMessageModel.published_at.is_(None),
            )
            .values(
                next_attempt_at=next_attempt_at,
                claimed_until=None,
                claim_token=None,
                last_error=error[:1_000],
            )
        )
        return self._execute_fenced(statement)

    def _execute_fenced(self, statement: Update) -> bool:
        with self._database.session() as session:
            result = session.execute(statement)
            changed = isinstance(result, CursorResult) and result.rowcount == 1
            session.commit()
        return changed
