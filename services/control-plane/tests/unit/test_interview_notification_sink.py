from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryEnvelope,
)
from sidera_control_plane.demo.contracts import DEMO_NOTIFICATION_SOURCE_REF
from sidera_control_plane.demo.notification_sink import deliver_claimed_notification
from tests.unit.interview_demo_support import manifest_for


def _envelope() -> NotificationDeliveryEnvelope:
    delivery_id = uuid4()
    return NotificationDeliveryEnvelope(
        delivery_id=delivery_id,
        notification_id=uuid4(),
        thread_id=uuid4(),
        contract_version=1,
        claim_token=uuid4(),
        fencing_token=1,
        idempotency_key=f"demo:{delivery_id}",
        channel_type="local",
        source_ref=DEMO_NOTIFICATION_SOURCE_REF,
        kind="attention",
        priority="high",
        root_task_title="CSV 批量导入",
        subtask_path=("CSV 数据校验",),
        worker_item_title="CSV 校验 WorkerItem",
        title="手机号规则需要确认",
        summary="校验 Worker 正在等待输入。",
        next_action="确认手机号允许为空",
        deep_link="/workspaces/demo/worker-items/validate",
        correlation_id=str(uuid4()),
        notification_ref="NTF-DEMO",
        created_at=datetime.now(UTC),
    )


def test_repeated_delivery_writes_one_message(tmp_path) -> None:
    manifest = manifest_for(tmp_path)
    envelope = _envelope()

    first = deliver_claimed_notification(manifest, envelope)
    second = deliver_claimed_notification(manifest, envelope)

    assert first == second == f"demo-message:{envelope.delivery_id}"
    files = tuple((manifest.run_root / "notifications").glob("message-*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["delivery_id"] == str(envelope.delivery_id)
    assert record["message_id"] == first
