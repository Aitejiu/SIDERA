import json
from uuid import uuid4

from pytest import CaptureFixture

from sidera_control_plane.application.outbox_dispatcher import OutboxMessage
from sidera_control_plane.outbox_worker import StdoutEventSink


def test_stdout_event_sink_writes_one_structured_json_line(
    capsys: CaptureFixture[str],
) -> None:
    message = OutboxMessage(
        id=uuid4(),
        event_id=uuid4(),
        topic="task.TaskCreated",
        payload={"aggregate_version": 1},
        attempts=1,
        claim_token=uuid4(),
    )

    StdoutEventSink().publish(message)

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output) == {
        "message_id": str(message.id),
        "event_id": str(message.event_id),
        "topic": "task.TaskCreated",
        "payload": {"aggregate_version": 1},
        "attempts": 1,
    }
