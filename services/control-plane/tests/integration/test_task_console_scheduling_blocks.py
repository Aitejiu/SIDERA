from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_r4_task_first_api import (
    CONSOLE_CALLBACK_TASK_ID,
    CONSOLE_ROOT_TASK_ID,
    CONSOLE_WAITING_EXECUTION_ID,
    NOW,
    _seed_console,
)

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.scheduling import (
    SchedulingBlockReason,
    scheduling_fingerprint,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    SchedulingBlockModel,
)

pytestmark = pytest.mark.integration

BLOCK_ID = UUID("b0000000-0000-0000-0000-000000000101")
CHANGED_BLOCK_ID = UUID("b0000000-0000-0000-0000-000000000102")


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _add_active_block(
    database: Database,
    *,
    block_id: UUID,
    reason: SchedulingBlockReason,
    reason_details: dict[str, object],
    version: int = 1,
) -> None:
    with database.session() as session:
        session.add(
            SchedulingBlockModel(
                id=block_id,
                workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
                execution_id=CONSOLE_WAITING_EXECUTION_ID,
                status="active",
                reason_code=reason.value,
                reason_details=reason_details,
                fingerprint=scheduling_fingerprint(
                    reason,
                    reason_details,
                    requires_operator=True,
                ),
                requires_operator=True,
                first_seen_at=NOW,
                last_seen_at=NOW,
                occurrence_count=1,
                resolved_at=None,
                version=version,
            )
        )
        session.commit()


def _block_execution(database: Database, *, version: int = 2) -> None:
    with database.session() as session:
        session.execute(
            update(ExecutionModel)
            .where(
                ExecutionModel.id == CONSOLE_WAITING_EXECUTION_ID,
                ExecutionModel.workspace_id
                == UUID("00000000-0000-0000-0000-000000000001"),
            )
            .values(status="queued", version=version, updated_at=NOW)
        )
        session.commit()


def _graph(client: TestClient) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/tasks/{CONSOLE_ROOT_TASK_ID}/graph",
        params={"workspace_id": "00000000-0000-0000-0000-000000000001"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _acknowledge_request(attention: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "command_type": "AcknowledgeAttentionItem",
        "target_type": "task",
        "actor": {"type": "local", "id": "aitejiu"},
        "target_id": str(CONSOLE_ROOT_TASK_ID),
        "expected_version": 1,
        "idempotency_key": f"ack:{attention['key']}",
        "correlation_id": str(uuid4()),
        "payload": {
            "attention_key": attention["key"],
            "source_correlation_id": attention["correlation_id"],
            "source_resource_version": attention["resource_version"],
        },
    }


def test_active_block_surfaces_safe_subtask_action_and_block_attention(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    del workspace_id
    _seed_console(database)
    _block_execution(database)
    _add_active_block(
        database,
        block_id=BLOCK_ID,
        reason=SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
        reason_details={"candidate_count": 2},
    )

    payload = _graph(client)
    callback = next(
        node for node in payload["nodes"] if node["subtask_key"] == "callback-idempotency"
    )
    assert callback["state"] == "blocked"
    assert callback["current_action"] == "2 个候选执行节点未配置运行环境"
    assert payload["overview"]["attention_items"] == [
        {
            "key": f"scheduling_block:{CONSOLE_ROOT_TASK_ID}:callback-idempotency:{BLOCK_ID}",
            "kind": "blocked",
            "task_title": "支付重构",
            "subtask_key": "callback-idempotency",
            "subtask_title": "回调幂等",
            "reason": "2 个候选执行节点未配置运行环境",
            "resource_id": str(BLOCK_ID),
            "resource_version": 1,
            "correlation_id": str(BLOCK_ID),
            "occurred_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert str(CONSOLE_WAITING_EXECUTION_ID) not in callback["current_action"]
    assert str(CONSOLE_CALLBACK_TASK_ID) not in callback["current_action"]


def test_block_acknowledgement_is_episode_bound_and_reason_changes_reappear(
    client: TestClient,
    database: Database,
    workspace_id: UUID,
) -> None:
    del workspace_id
    _seed_console(database)
    _block_execution(database)
    _add_active_block(
        database,
        block_id=BLOCK_ID,
        reason=SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
        reason_details={"candidate_count": 2},
    )
    attention = _graph(client)["overview"]["attention_items"][0]

    acknowledged = client.post(
        "/api/v1/commands", json=_acknowledge_request(attention)
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert _graph(client)["overview"]["attention_items"] == []

    with database.session() as session:
        session.execute(
            update(SchedulingBlockModel)
            .where(SchedulingBlockModel.id == BLOCK_ID)
            .values(
                version=2,
                occurrence_count=2,
                last_seen_at=NOW + timedelta(minutes=1),
            )
        )
        session.commit()
    assert _graph(client)["overview"]["attention_items"] == []

    with database.session() as session:
        session.execute(
            update(SchedulingBlockModel)
            .where(SchedulingBlockModel.id == BLOCK_ID)
            .values(status="resolved", resolved_at=NOW + timedelta(minutes=2))
        )
        session.commit()
    _add_active_block(
        database,
        block_id=CHANGED_BLOCK_ID,
        reason=SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
        reason_details={"candidate_count": 1},
    )
    changed = _graph(client)["overview"]["attention_items"]
    assert len(changed) == 1
    assert changed[0]["resource_id"] == str(CHANGED_BLOCK_ID)
    assert changed[0]["reason"] == "1 个候选执行节点暂不可用"

    with database.session() as session:
        session.execute(
            update(SchedulingBlockModel)
            .where(SchedulingBlockModel.id == CHANGED_BLOCK_ID)
            .values(status="resolved", resolved_at=datetime.now(UTC))
        )
        session.commit()
    assert _graph(client)["overview"]["attention_items"] == []
