from dataclasses import replace
from uuid import uuid4

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.domain.commands import CommandEnvelope


def test_request_hash_uses_semantics_not_transport_ids() -> None:
    envelope = CommandEnvelope(
        id=uuid4(),
        workspace_id=uuid4(),
        command_type="CreateTask",
        actor_type="user",
        actor_id="user:test",
        target_id=None,
        payload={"title": "Build API", "priority": 50},
        expected_version=None,
        idempotency_key="create-task-001",
        correlation_id=uuid4(),
    )

    replay = replace(envelope, id=uuid4(), correlation_id=uuid4())
    changed = replace(envelope, payload={**envelope.payload, "priority": 90})

    assert hash_request(replay) == hash_request(envelope)
    assert hash_request(changed) != hash_request(envelope)
