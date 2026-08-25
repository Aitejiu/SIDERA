from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_knowledge.domain.commands import (
    CommandEnvelope,
    EventEnvelope,
    PayloadSafetyError,
    topic_for_event,
)


def test_command_request_hash_is_stable_and_excludes_transport_identity() -> None:
    command_id = uuid4()
    correlation_id = uuid4()
    left = CommandEnvelope(
        command_id=command_id,
        workspace_id=uuid4(),
        command_type="SubmitKnowledgeCandidate",
        actor_type="worker",
        actor_id="worker-1",
        target_type="knowledge_candidate",
        target_id=uuid4(),
        payload={"title": "Guide", "tags": ["sidera"]},
        expected_version=1,
        idempotency_key="candidate-1",
        correlation_id=correlation_id,
    )
    right = CommandEnvelope(
        command_id=command_id,
        workspace_id=left.workspace_id,
        command_type=left.command_type,
        actor_type=left.actor_type,
        actor_id=left.actor_id,
        target_type=left.target_type,
        target_id=left.target_id,
        payload={"tags": ["sidera"], "title": "Guide"},
        expected_version=1,
        idempotency_key="candidate-1",
        correlation_id=uuid4(),
    )

    assert left.request_hash == right.request_hash
    assert len(left.request_hash) == 64


def test_event_payload_rejects_knowledge_body_and_event_topic_is_stable() -> None:
    with pytest.raises(PayloadSafetyError, match="snapshot"):
        EventEnvelope(
            event_id=uuid4(),
            workspace_id=uuid4(),
            aggregate_type="knowledge_candidate",
            aggregate_id=uuid4(),
            aggregate_version=1,
            event_type="KnowledgeCandidateSubmitted",
            payload={"nested": [{"snapshot": {"title": "secret"}}]},
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            correlation_id=uuid4(),
        )

    assert topic_for_event("KnowledgeCandidateSubmitted") == (
        "knowledge.candidate_submitted"
    )
