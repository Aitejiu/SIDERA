from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.domain.execution import TERMINAL_EXECUTION_STATUSES
from sidera_control_plane.domain.projections import (
    TraceCategory,
    TraceTimingSummary,
    TraceUsage,
    WorkerContextBranchProjection,
    WorkerTraceEvent,
    WorkerTraceProjection,
)

MAX_TITLE_LENGTH = 160
MAX_SUMMARY_LENGTH = 4_096
MAX_EXTENSIONS_BYTES = 32 * 1024
_SENSITIVE_KEY = re.compile(
    r"(?i)(token|secret|authorization|cookie|password|credential|fencing|absolute_path)"
)


@dataclass(frozen=True, slots=True)
class RawTraceFact:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID | None
    work_item_key: str | None
    execution_id: UUID
    session_id: UUID
    sequence: int
    occurred_at: datetime
    event_type: str
    status: str
    payload: Mapping[str, Any]
    duration_ms: int | None = None
    provider_payload_ref: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RawTraceFact:
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            workspace_id=UUID(str(payload["workspace_id"])),
            root_task_id=UUID(str(payload["root_task_id"])),
            plan_snapshot_id=(
                UUID(str(payload["plan_snapshot_id"]))
                if payload.get("plan_snapshot_id")
                else None
            ),
            work_item_key=(
                str(payload["work_item_key"]) if payload.get("work_item_key") else None
            ),
            execution_id=UUID(str(payload["execution_id"])),
            session_id=UUID(str(payload["session_id"])),
            sequence=int(payload["sequence"]),
            occurred_at=_timestamp(payload["occurred_at"]),
            event_type=str(payload["event_type"]),
            status=str(payload.get("status", "unknown")),
            payload=(
                dict(payload["payload"])
                if isinstance(payload.get("payload"), Mapping)
                else {}
            ),
            duration_ms=_optional_int(payload.get("duration_ms")),
            provider_payload_ref=(
                str(payload["provider_payload_ref"])
                if payload.get("provider_payload_ref")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class TraceArtifactFact:
    artifact_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerTraceQueryResult:
    projection: WorkerTraceProjection
    context_branch: WorkerContextBranchProjection | None


class WorkerTraceProjectionService:
    def normalize_events(
        self, facts: Iterable[RawTraceFact]
    ) -> tuple[WorkerTraceEvent, ...]:
        return tuple(self.normalize_event(fact) for fact in facts)

    def normalize_event(self, fact: RawTraceFact) -> WorkerTraceEvent:
        category = _category(fact.event_type, fact.payload)
        usage = _usage(fact.payload)
        if fact.event_type.strip().lower() == "turn.completed":
            title = "回合已完成"
            summary = "Agent 已完成本轮执行"
        else:
            title = _bounded_text(
                _text_value(
                    fact.payload,
                    "title",
                    "tool",
                    "name",
                    "event_type",
                )
                or fact.event_type,
                MAX_TITLE_LENGTH,
            )
            summary = _bounded_text(
                _text_value(
                    fact.payload,
                    "summary",
                    "trace_summary",
                    "text",
                    "message",
                    "tool",
                    "kind",
                )
                or _category_summary(category, fact.event_type),
                MAX_SUMMARY_LENGTH,
            )
        artifact_refs = _uuid_values(
            fact.payload.get("artifact_refs") or fact.payload.get("artifact_ids")
        )
        return WorkerTraceEvent(
            schema_version=1,
            workspace_id=fact.workspace_id,
            root_task_id=fact.root_task_id,
            work_item_key=fact.work_item_key,
            plan_snapshot_id=fact.plan_snapshot_id,
            execution_id=fact.execution_id,
            session_id=fact.session_id,
            sequence=fact.sequence,
            occurred_at=fact.occurred_at,
            category=category,
            status=fact.status,
            title=title,
            summary=summary,
            duration_ms=fact.duration_ms or _optional_int(fact.payload.get("duration_ms")),
            usage=usage,
            artifact_refs=artifact_refs,
            provider_payload_ref=_bounded_ref(fact.provider_payload_ref),
            provider_extensions=_safe_extensions(fact.payload),
        )

    def compose_projection(
        self,
        events: Iterable[WorkerTraceEvent],
        *,
        execution_id: UUID,
        session_id: UUID,
        worker_id: UUID | None,
        status: str,
        plan_generation: int | None,
        last_change_cursor: int,
        previous_before_sequence: int | None,
    ) -> WorkerTraceProjection:
        ordered = tuple(sorted(events, key=lambda event: event.sequence))
        timing = _timing(ordered)
        checkpoint_sequence = max(
            (
                event.sequence
                for event in ordered
                if event.category is TraceCategory.CHECKPOINT
            ),
            default=None,
        )
        return WorkerTraceProjection(
            schema_version=1,
            execution_id=execution_id,
            session_id=session_id,
            worker_id=worker_id,
            status=status,
            current_action=ordered[-1].summary if ordered else None,
            plan_generation=plan_generation,
            checkpoint_sequence=checkpoint_sequence,
            freshness_at=ordered[-1].occurred_at if ordered else None,
            last_change_cursor=last_change_cursor,
            timing=timing,
            events=ordered,
            previous_before_sequence=previous_before_sequence,
        )


class WorkerTraceQueryService:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._projection = WorkerTraceProjectionService()

    def get_trace(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> WorkerTraceQueryResult:
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("before_sequence must be positive")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        snapshot = self._store.load_trace(
            workspace_id,
            root_task_id,
            execution_id,
            session_id,
            before_sequence=before_sequence,
            limit=limit,
        )
        if snapshot is None:
            raise NotFound("worker trace session not found")
        facts = snapshot.events
        previous_before_sequence = snapshot.previous_before_sequence
        if before_sequence is None and snapshot.status in {
            status.value for status in TERMINAL_EXECUTION_STATUSES
        }:
            referenced_artifact_ids = {
                artifact_id
                for fact in facts
                for artifact_id in _uuid_values(
                    fact.payload.get("artifact_refs")
                    or fact.payload.get("artifact_ids")
                )
            }
            untraced_artifacts = tuple(
                artifact
                for artifact in snapshot.trace_artifacts
                if artifact.artifact_id not in referenced_artifact_ids
            )
            if untraced_artifacts:
                occurred_at = max(
                    (
                        *(fact.occurred_at for fact in facts),
                        *(artifact.occurred_at for artifact in untraced_artifacts),
                    )
                )
                artifact_fact = RawTraceFact(
                    schema_version=1,
                    workspace_id=snapshot.workspace_id,
                    root_task_id=snapshot.root_task_id,
                    plan_snapshot_id=snapshot.plan_snapshot_id,
                    work_item_key=snapshot.work_item_key,
                    execution_id=snapshot.execution_id,
                    session_id=snapshot.session_id,
                    sequence=max((fact.sequence for fact in facts), default=0) + 1,
                    occurred_at=occurred_at,
                    event_type="artifact.available",
                    status=snapshot.status,
                    payload={
                        "title": "执行产物已保存",
                        "summary": f"本次执行生成了 {len(untraced_artifacts)} 个产物",
                        "artifact_refs": [
                            str(artifact.artifact_id) for artifact in untraced_artifacts
                        ],
                    },
                )
                facts = (*facts, artifact_fact)
                if len(facts) > limit:
                    facts = facts[-limit:]
                    previous_before_sequence = min(fact.sequence for fact in facts)

        events = self._projection.normalize_events(facts)
        projection = self._projection.compose_projection(
            events,
            execution_id=snapshot.execution_id,
            session_id=snapshot.session_id,
            worker_id=snapshot.worker_id,
            status=snapshot.status,
            plan_generation=snapshot.plan_generation,
            last_change_cursor=snapshot.last_change_cursor,
            previous_before_sequence=previous_before_sequence,
        )
        context = None
        if (
            snapshot.plan_snapshot_id is not None
            and snapshot.work_item_key is not None
            and snapshot.worker_slot_key is not None
        ):
            context = WorkerContextBranchProjection(
                root_task_id=snapshot.root_task_id,
                plan_snapshot_id=snapshot.plan_snapshot_id,
                work_item_key=snapshot.work_item_key,
                worker_slot_key=snapshot.worker_slot_key,
                execution_lineage_id=snapshot.lineage_id,
                current_execution_id=snapshot.execution_id,
                current_session_id=snapshot.session_id,
                head_checkpoint_sequence=projection.checkpoint_sequence,
                artifact_refs=snapshot.artifact_refs,
                replay_capability="portable_replay",
            )
        return WorkerTraceQueryResult(projection=projection, context_branch=context)


def _category(event_type: str, payload: Mapping[str, Any]) -> TraceCategory:
    normalized = event_type.lower()
    declared_category = payload.get("trace_category")
    if isinstance(declared_category, str):
        try:
            return TraceCategory(declared_category)
        except ValueError:
            pass
    update_type = str(payload.get("sessionUpdate", "")).lower()
    if normalized.startswith("input"):
        return TraceCategory.INPUT
    if normalized.startswith(("execution", "lease", "scheduler")):
        return TraceCategory.SCHEDULER
    if normalized.startswith("artifact"):
        return TraceCategory.ARTIFACT
    if normalized.startswith("checkpoint"):
        return TraceCategory.CHECKPOINT
    if "error" in normalized or normalized.startswith("failed"):
        return TraceCategory.ERROR
    if update_type in {"agent_message_chunk", "plan"} or normalized in {
        "assistant.summary",
        "progress.summary",
    }:
        return TraceCategory.ASSISTANT_SUMMARY
    if update_type == "usage_update" or normalized in {"model.usage", "usage.update"}:
        return TraceCategory.MODEL
    if update_type == "tool_call":
        return TraceCategory.TOOL
    if update_type == "tool_call_update":
        return TraceCategory.SUBTOOL if payload.get("parent_tool") else TraceCategory.TOOL
    if "tool" in normalized or payload.get("tool"):
        return TraceCategory.TOOL
    return TraceCategory.MODEL


def _category_summary(category: TraceCategory, event_type: str) -> str:
    if category is TraceCategory.MODEL:
        return event_type
    return category.value


def _usage(payload: Mapping[str, Any]) -> TraceUsage | None:
    usage = payload.get("trace_usage") or payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    return TraceUsage(
        input_tokens=_optional_int(usage.get("input_tokens")),
        output_tokens=_optional_int(usage.get("output_tokens")),
        cached_tokens=_optional_int(usage.get("cached_tokens")),
        cost_microunits=_optional_int(usage.get("cost_microunits")),
    )


def _timing(events: tuple[WorkerTraceEvent, ...]) -> TraceTimingSummary:
    input_ms = sum(
        event.duration_ms or 0 for event in events if event.category is TraceCategory.INPUT
    )
    model_ms = sum(
        event.duration_ms or 0 for event in events if event.category is TraceCategory.MODEL
    )
    tools_ms = sum(
        event.duration_ms or 0
        for event in events
        if event.category in {TraceCategory.TOOL, TraceCategory.SUBTOOL}
    )
    return TraceTimingSummary(
        input_ms=input_ms,
        model_ms=model_ms,
        tools_ms=tools_ms,
        total_ms=input_ms + model_ms + tools_ms,
    )


def _safe_extensions(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized_keys = {
        "artifact_ids",
        "artifact_refs",
        "duration_ms",
        "kind",
        "message",
        "name",
        "operation_id",
        "schema_version",
        "sessionUpdate",
        "stop_reason",
        "summary",
        "text",
        "title",
        "tool",
        "trace_category",
        "trace_summary",
        "trace_usage",
        "usage",
    }
    value = {
        str(key): item
        for key, item in payload.items()
        if str(key) not in normalized_keys
    }
    redacted = _redact(value)
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_EXTENSIONS_BYTES:
        return redacted if isinstance(redacted, dict) else {"value": redacted}
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "size_bytes": len(encoded.encode("utf-8")),
    }


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item, key=str(item_key))
            for item_key, item in value.items()
            if not _SENSITIVE_KEY.search(str(item_key))
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _bounded_text(value, MAX_SUMMARY_LENGTH)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(str(value), MAX_SUMMARY_LENGTH)


def _text_value(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _bounded_text(value: str, limit: int) -> str:
    return value.strip()[:limit]


def _bounded_ref(value: str | None) -> str | None:
    return value.strip()[:500] if value and value.strip() else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime:
    timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)


def _uuid_values(value: Any) -> tuple[UUID, ...]:
    values = value if isinstance(value, (list, tuple)) else ()
    result: list[UUID] = []
    for item in values:
        try:
            result.append(item if isinstance(item, UUID) else UUID(str(item)))
        except (TypeError, ValueError):
            continue
    return tuple(result)
