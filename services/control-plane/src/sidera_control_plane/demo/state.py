from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

_SCHEMA_VERSION = 1
_GATE_NAMES = frozenset({"parallel", "before-artifact", "handoff", "governance"})
GateName = Literal["parallel", "before-artifact", "handoff", "governance"]
OperationStatus = Literal["pending", "applied", "rejected", "unknown"]
SettlementOutcome = Literal["applied", "rejected", "unknown"]


class StateIdentityConflict(ValueError):
    """Raised when a durable key is reused for a different operation."""


class StateCorrupt(ValueError):
    """Raised when a state document is missing or cannot be trusted."""


@dataclass(frozen=True)
class OperationRecord:
    operation_id: UUID
    request_hash: str
    status: OperationStatus
    result: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "operation_id": str(self.operation_id),
            "request_hash": self.request_hash,
            "status": self.status,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> OperationRecord:
        try:
            operation_id = UUID(str(value["operation_id"]))
            request_hash = str(value["request_hash"])
            status = str(value["status"])
            result = value["result"]
        except (KeyError, ValueError, TypeError) as exc:
            raise StateCorrupt("invalid_operation_record") from exc
        if status not in {"pending", "applied", "rejected", "unknown"}:
            raise StateCorrupt("invalid_operation_status")
        if not isinstance(result, dict):
            raise StateCorrupt("invalid_operation_result")
        return cls(operation_id, request_hash, status, dict(result))  # type: ignore[arg-type]


@dataclass(frozen=True)
class StoredEvent:
    sequence: int
    operation_id: UUID
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "operation_id": str(self.operation_id),
            "event_type": self.event_type,
            "payload": self.payload,
            "occurred_at": self.occurred_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> StoredEvent:
        try:
            raw_sequence = value["sequence"]
            operation_id = UUID(str(value["operation_id"]))
            event_type = str(value["event_type"])
            payload = value["payload"]
            occurred_at = datetime.fromisoformat(str(value["occurred_at"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise StateCorrupt("invalid_stored_event") from exc
        if (
            not isinstance(raw_sequence, int)
            or isinstance(raw_sequence, bool)
            or raw_sequence < 1
            or not event_type
            or not isinstance(payload, dict)
        ):
            raise StateCorrupt("invalid_stored_event")
        if occurred_at.tzinfo is None:
            raise StateCorrupt("event_timestamp_must_have_timezone")
        return cls(
            raw_sequence, operation_id, event_type, dict(payload), occurred_at
        )  # type: ignore[arg-type]


class DemoStateStore:
    def __init__(self, root: Path, run_id: UUID) -> None:
        self.root = root.expanduser()
        if not self.root.is_absolute():
            raise ValueError("state_root_must_be_absolute")
        self.run_id = run_id
        self.operations_path = self.root / "operations.json"
        self.events_path = self.root / "events.json"
        self.gates_path = self.root / "gates.json"
        self.lock_path = self.root / "lock"
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.lock_path.touch(exist_ok=True)
        os.chmod(self.lock_path, 0o600)
        with self._locked():
            self._ensure_document(
                self.operations_path, {"operations": {}}
            )
            self._ensure_document(self.events_path, {"sessions": {}})
            self._ensure_document(
                self.gates_path,
                {"gates": {name: False for name in sorted(_GATE_NAMES)}},
            )

    def read(self, key: str) -> dict[str, object] | None:
        self._validate_key(key)
        with self._locked():
            document = self._read_document(self.operations_path)
            value = document["operations"].get(key)  # type: ignore[union-attr]
            if value is None:
                return None
            if not isinstance(value, dict):
                raise StateCorrupt("invalid_operation_record")
            return dict(value)

    def find_operation(self, operation_id: UUID) -> OperationRecord | None:
        if not isinstance(operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        with self._locked():
            document = self._read_document(self.operations_path)
            operations = document["operations"]
            if not isinstance(operations, dict):
                raise StateCorrupt("invalid_operations")
            for raw_value in operations.values():
                if not isinstance(raw_value, dict):
                    raise StateCorrupt("invalid_operation_record")
                record = OperationRecord.from_dict(raw_value)
                if record.operation_id == operation_id:
                    return record
        return None

    def list_operations(self) -> tuple[tuple[str, OperationRecord], ...]:
        """Return durable operations with their keys for recovery inspection."""

        with self._locked():
            document = self._read_document(self.operations_path)
            operations = document["operations"]
            if not isinstance(operations, dict):
                raise StateCorrupt("invalid_operations")
            records: list[tuple[str, OperationRecord]] = []
            for key, raw_value in operations.items():
                if not isinstance(key, str) or not key:
                    raise StateCorrupt("invalid_operation_key")
                if not isinstance(raw_value, dict):
                    raise StateCorrupt("invalid_operation_record")
                records.append((key, OperationRecord.from_dict(raw_value)))
            return tuple(sorted(records, key=lambda item: item[0]))

    def reserve(
        self, key: str, operation_id: UUID, request: dict[str, object]
    ) -> OperationRecord:
        self._validate_key(key)
        request_hash = self.request_hash(request)
        with self._locked():
            document = self._read_document(self.operations_path)
            operations = document["operations"]
            if not isinstance(operations, dict):
                raise StateCorrupt("invalid_operations")
            for existing_key, existing_value in operations.items():
                if not isinstance(existing_value, dict):
                    raise StateCorrupt("invalid_operation_record")
                existing = OperationRecord.from_dict(existing_value)
                if existing_key == key and existing.operation_id != operation_id:
                    raise StateIdentityConflict("operation_id_mismatch")
                if existing.operation_id != operation_id:
                    continue
                if existing_key != key:
                    raise StateIdentityConflict("operation_id_already_used")
                if existing.request_hash != request_hash:
                    raise StateIdentityConflict("request_hash_mismatch")
                return existing

            record = OperationRecord(operation_id, request_hash, "pending", {})
            operations[key] = record.as_dict()
            self._write_document(self.operations_path, document)
            return record

    def settle(
        self,
        key: str,
        operation_id: UUID,
        outcome: SettlementOutcome,
        result: dict[str, object],
    ) -> OperationRecord:
        self._validate_key(key)
        if outcome not in {"applied", "rejected", "unknown"}:
            raise ValueError("invalid_settlement_outcome")
        with self._locked():
            document = self._read_document(self.operations_path)
            operations = document["operations"]
            if not isinstance(operations, dict) or key not in operations:
                raise StateIdentityConflict("operation_not_reserved")
            raw_record = operations[key]
            if not isinstance(raw_record, dict):
                raise StateCorrupt("invalid_operation_record")
            current = OperationRecord.from_dict(raw_record)
            if current.operation_id != operation_id:
                raise StateIdentityConflict("operation_id_mismatch")
            if current.status not in {"pending", "unknown"}:
                if current.status == outcome and current.result == result:
                    return current
                raise StateIdentityConflict("operation_already_settled")
            settled = OperationRecord(operation_id, current.request_hash, outcome, dict(result))
            operations[key] = settled.as_dict()
            self._write_document(self.operations_path, document)
            return settled

    def append_event(
        self,
        session_id: UUID,
        operation_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> StoredEvent:
        if not event_type or event_type.strip() != event_type:
            raise ValueError("invalid_event_type")
        with self._locked():
            document = self._read_document(self.events_path)
            sessions = document["sessions"]
            if not isinstance(sessions, dict):
                raise StateCorrupt("invalid_sessions")
            for existing_session, raw_events in sessions.items():
                if not isinstance(raw_events, list):
                    raise StateCorrupt("invalid_session_events")
                for raw_event in raw_events:
                    if not isinstance(raw_event, dict):
                        raise StateCorrupt("invalid_stored_event")
                    event = StoredEvent.from_dict(raw_event)
                    if event.operation_id != operation_id:
                        continue
                    if (
                        existing_session == str(session_id)
                        and event.event_type == event_type
                        and event.payload == payload
                    ):
                        return event
                    raise StateIdentityConflict("event_operation_id_already_used")

            session_key = str(session_id)
            raw_events = sessions.setdefault(session_key, [])
            if not isinstance(raw_events, list):
                raise StateCorrupt("invalid_session_events")
            last_sequence = 0
            for raw_event in raw_events:
                if not isinstance(raw_event, dict):
                    raise StateCorrupt("invalid_stored_event")
                last_sequence = max(last_sequence, StoredEvent.from_dict(raw_event).sequence)
            event = StoredEvent(
                sequence=last_sequence + 1,
                operation_id=operation_id,
                event_type=event_type,
                payload=dict(payload),
                occurred_at=datetime.now(UTC),
            )
            raw_events.append(event.as_dict())
            self._write_document(self.events_path, document)
            return event

    def events_after(self, session_id: UUID, sequence: int) -> tuple[StoredEvent, ...]:
        if sequence < 0:
            raise ValueError("sequence_must_not_be_negative")
        with self._locked():
            document = self._read_document(self.events_path)
            sessions = document["sessions"]
            if not isinstance(sessions, dict):
                raise StateCorrupt("invalid_sessions")
            raw_events = sessions.get(str(session_id), [])
            if not isinstance(raw_events, list):
                raise StateCorrupt("invalid_session_events")
            events = tuple(
                StoredEvent.from_dict(raw_event)
                for raw_event in raw_events
                if isinstance(raw_event, dict)
            )
            if len(events) != len(raw_events):
                raise StateCorrupt("invalid_stored_event")
            return tuple(event for event in events if event.sequence > sequence)

    def set_gate(self, name: GateName, released: bool) -> None:
        if name not in _GATE_NAMES:
            raise ValueError("invalid_gate_name")
        with self._locked():
            document = self._read_document(self.gates_path)
            gates = document["gates"]
            if not isinstance(gates, dict):
                raise StateCorrupt("invalid_gates")
            gates[name] = bool(released)
            self._write_document(self.gates_path, document)

    def read_gate(self, name: GateName) -> bool:
        if name not in _GATE_NAMES:
            raise ValueError("invalid_gate_name")
        with self._locked():
            document = self._read_document(self.gates_path)
            gates = document["gates"]
            if not isinstance(gates, dict) or not isinstance(gates.get(name), bool):
                raise StateCorrupt("invalid_gates")
            return gates[name]

    @staticmethod
    def request_hash(request: dict[str, object]) -> str:
        try:
            encoded = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("request_must_be_json_serializable") from exc
        return sha256(encoded).hexdigest()

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or key.strip() != key:
            raise ValueError("invalid_state_key")

    def _ensure_document(self, path: Path, body: dict[str, object]) -> None:
        if path.exists():
            self._read_document(path)
            return
        self._write_document(
            path,
            {"schema_version": _SCHEMA_VERSION, "run_id": str(self.run_id), **body},
        )

    def _read_document(self, path: Path) -> dict[str, object]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorrupt("invalid_json") from exc
        if not isinstance(document, dict):
            raise StateCorrupt("invalid_document")
        if document.get("schema_version") != _SCHEMA_VERSION:
            raise StateCorrupt("unsupported_schema_version")
        if document.get("run_id") != str(self.run_id):
            raise StateIdentityConflict("run_id_mismatch")
        return document

    def _write_document(self, path: Path, document: dict[str, object]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    document,
                    temporary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            flock(lock_file.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(lock_file.fileno(), LOCK_UN)
