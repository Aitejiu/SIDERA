from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

from sidera_knowledge.application.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxMessage,
)
from sidera_knowledge.config import KnowledgeSettings
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.outbox_store import PostgresOutboxStore


def _safe_summary(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in ("body", "content", "markdown", "snapshot")):
                continue
            if "id" in lowered or "hash" in lowered or "status" in lowered:
                result[str(key)] = _safe_summary(item)
        return result
    if isinstance(value, list):
        return [_safe_summary(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return None


class JsonLineSink:
    def publish(self, message: OutboxMessage) -> None:
        output = {
            "message_id": str(message.message_id),
            "event_id": str(message.event_id),
            "workspace_id": str(message.workspace_id),
            "topic": message.topic,
            "attempt": message.attempt,
            "payload": _safe_summary(message.payload),
        }
        sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch Knowledge outbox messages once")
    parser.add_argument("--once", action="store_true", help="run one bounded batch")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args()
    if not args.once:
        parser.error("--once is required; continuous scheduling belongs to deployment")

    settings = KnowledgeSettings()  # pyright: ignore[reportCallIssue]
    database = Database(settings.database_url)
    try:
        dispatcher = OutboxDispatcher(
            PostgresOutboxStore(database, settings.workspace_id),
            JsonLineSink(),
            batch_size=args.batch_size,
            max_attempts=args.max_attempts,
        )
        dispatcher.dispatch_once()
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
