from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryService,
)
from sidera_control_plane.domain.notifications import (
    NotificationAttemptErrorCode,
    NotificationAttemptOutcome,
    NotificationDeliveryStatus,
)
from sidera_control_plane.persistence.models import (
    NotificationDeliveryIntentModel,
    NotificationFactModel,
    NotificationThreadModel,
    TaskModel,
)
from tests.integration.test_notification_delivery_concurrency import _seed_delivery

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)


def test_terminal_delivery_failure_converges_to_web_operations_state(
    database, workspace_id, uow_factory, clean_database
) -> None:
    del clean_database
    seed = _seed_delivery(uow_factory, workspace_id)

    with database.session() as session:
        task_before = session.scalar(
            select(TaskModel).where(TaskModel.workspace_id == workspace_id)
        )
        assert task_before is not None
        task_state_before = (task_before.status, task_before.version, task_before.title)

    service = NotificationDeliveryService(
        uow_factory,
        clock=lambda: NOW,
        max_attempts=1,
    )
    envelope = service.claim(
        workspace_id,
        seed.module_id,
        supported_contract_versions=(1,),
        now=NOW,
    )[0]
    settlement = service.nack(
        workspace_id,
        seed.intent_id,
        claim_token=envelope.claim_token,
        fencing_token=envelope.fencing_token,
        outcome=NotificationAttemptOutcome.TERMINAL_FAILURE,
        error_code=NotificationAttemptErrorCode.PAYLOAD_REJECTED,
        now=NOW,
    )

    assert settlement.status is NotificationDeliveryStatus.EXHAUSTED

    with database.session() as session:
        facts = session.scalars(
            select(NotificationFactModel)
            .where(NotificationFactModel.workspace_id == workspace_id)
            .order_by(NotificationFactModel.created_at)
        ).all()
        operations_facts = [fact for fact in facts if fact.priority == "operations"]
        operations_threads = session.scalars(
            select(NotificationThreadModel).where(
                NotificationThreadModel.workspace_id == workspace_id,
                NotificationThreadModel.topic.like("notification_channel_health:%"),
            )
        ).all()
        intent_count = session.scalar(
            select(func.count()).select_from(NotificationDeliveryIntentModel).where(
                NotificationDeliveryIntentModel.workspace_id == workspace_id
            )
        )
        task_after = session.scalar(
            select(TaskModel).where(TaskModel.workspace_id == workspace_id)
        )

        assert len(facts) == 2
        assert len(operations_facts) == 1
        assert operations_facts[0].transition == "degraded"
        assert operations_facts[0].source_projection_type == "notification_delivery"
        assert operations_facts[0].source_resource_version == 1
        assert "payload_rejected" not in operations_facts[0].summary
        assert len(operations_threads) == 1
        assert operations_threads[0].business_status == "open"
        assert intent_count == 1
        assert task_after is not None
        assert (task_after.status, task_after.version, task_after.title) == task_state_before
