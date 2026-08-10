import argparse
import json

from sidera_control_plane.application.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxMessage,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.outbox_store import SqlAlchemyOutboxStore


class StdoutEventSink:
    def publish(self, message: OutboxMessage) -> None:
        print(
            json.dumps(
                {
                    "message_id": str(message.id),
                    "event_id": str(message.event_id),
                    "topic": message.topic,
                    "payload": message.payload,
                    "attempts": message.attempts,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch SIDERA outbox messages")
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args()

    settings = Settings()  # pyright: ignore[reportCallIssue]
    database = Database(settings.database_url)
    try:
        store = SqlAlchemyOutboxStore(database)
        OutboxDispatcher(store, StdoutEventSink()).dispatch_once()
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
