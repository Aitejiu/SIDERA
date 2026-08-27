from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID

from sidera_control_plane.domain.context_packages import (
    ContextMaterializationV1,
    ContextMetadata,
    context_package_reference,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
)
from sidera_control_plane.domain.plan_revisions import (
    RevisionBaseDescriptorV1,
    plan_snapshot_content_hash,
)
from sidera_control_plane.domain.planning import PlanningRequest, PlanningRequestKind
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker_item import WorkerItem

PLANNING_CONTEXT_SCHEMA_VERSION = 1
_SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class AdapterCapabilitySnapshotV1:
    """Safe, provider-independent capability facts exposed to a planner."""

    adapter_ref: str
    interface_version: str
    config_version: int
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        _required_text(self.adapter_ref, "adapter_ref")
        _required_text(self.interface_version, "adapter interface_version")
        _positive(self.config_version, "adapter config_version")
        object.__setattr__(
            self,
            "capabilities",
            _sorted_texts(self.capabilities, "adapter capabilities"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "adapter_ref": self.adapter_ref,
            "interface_version": self.interface_version,
            "config_version": self.config_version,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class ConfirmedContextPackageRefV1:
    """An exact, already-confirmed package reference without package contents."""

    ref: str
    package_hash: str | None = None
    context_version: int | None = None

    def __post_init__(self) -> None:
        _required_text(self.ref, "context package ref")
        if self.package_hash is not None:
            _sha256(self.package_hash, "context package hash")
        if self.context_version is not None:
            _positive(self.context_version, "context package context_version")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "package_hash": self.package_hash,
            "context_version": self.context_version,
        }


@dataclass(frozen=True, slots=True)
class RevisionTaskFactV1:
    task_id: UUID
    plan_key: str
    title: str
    status: str
    version: int

    def __post_init__(self) -> None:
        _uuid(self.task_id, "task_id")
        _required_text(self.plan_key, "task plan_key")
        _required_text(self.title, "task title")
        _required_text(self.status, "task status")
        _positive(self.version, "task version")

    def to_mapping(self) -> dict[str, object]:
        return {
            "task_id": str(self.task_id),
            "plan_key": self.plan_key,
            "title": self.title,
            "status": self.status,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class RevisionWorkerItemFactV1:
    worker_item_id: UUID
    stable_key: str
    subtask_key: str
    title: str
    role: str
    responsibility: str
    status: str
    context_version: int
    acknowledged_context_version: int
    version: int
    capability_requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid(self.worker_item_id, "worker_item_id")
        _required_text(self.stable_key, "WorkerItem stable_key")
        _required_text(self.subtask_key, "WorkerItem subtask_key")
        _required_text(self.title, "WorkerItem title")
        _required_text(self.role, "WorkerItem role")
        _required_text(self.responsibility, "WorkerItem responsibility")
        _required_text(self.status, "WorkerItem status")
        _positive(self.context_version, "WorkerItem context_version")
        _positive(
            self.acknowledged_context_version,
            "WorkerItem acknowledged_context_version",
        )
        if self.acknowledged_context_version > self.context_version:
            raise ValidationError(
                "WorkerItem acknowledged_context_version cannot exceed context_version"
            )
        _positive(self.version, "WorkerItem version")
        object.__setattr__(
            self,
            "capability_requirements",
            _sorted_texts(
                self.capability_requirements,
                "WorkerItem capability_requirements",
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "worker_item_id": str(self.worker_item_id),
            "stable_key": self.stable_key,
            "subtask_key": self.subtask_key,
            "title": self.title,
            "role": self.role,
            "responsibility": self.responsibility,
            "status": self.status,
            "context_version": self.context_version,
            "acknowledged_context_version": self.acknowledged_context_version,
            "version": self.version,
            "capability_requirements": list(self.capability_requirements),
        }


@dataclass(frozen=True, slots=True)
class RevisionExecutionFactV1:
    execution_id: UUID
    task_id: UUID
    worker_item_id: UUID | None
    lineage_id: UUID
    attempt: int
    status: str
    assigned_worker_id: UUID | None
    pinned_worker_id: UUID | None
    version: int
    capability_requirements: tuple[str, ...]
    adapter_preferences: tuple[str, ...]
    adapter_forbidden: tuple[str, ...]
    session_id: UUID | None = None

    def __post_init__(self) -> None:
        _uuid(self.execution_id, "execution_id")
        _uuid(self.task_id, "execution task_id")
        _optional_uuid(self.worker_item_id, "execution worker_item_id")
        _uuid(self.lineage_id, "execution lineage_id")
        _positive(self.attempt, "execution attempt")
        _required_text(self.status, "execution status")
        _optional_uuid(self.assigned_worker_id, "execution assigned_worker_id")
        _optional_uuid(self.pinned_worker_id, "execution pinned_worker_id")
        _positive(self.version, "execution version")
        _optional_uuid(self.session_id, "execution session_id")
        object.__setattr__(
            self,
            "capability_requirements",
            _sorted_texts(
                self.capability_requirements,
                "execution capability_requirements",
            ),
        )
        object.__setattr__(
            self,
            "adapter_preferences",
            _sorted_texts(self.adapter_preferences, "execution adapter_preferences"),
        )
        object.__setattr__(
            self,
            "adapter_forbidden",
            _sorted_texts(self.adapter_forbidden, "execution adapter_forbidden"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "execution_id": str(self.execution_id),
            "task_id": str(self.task_id),
            "worker_item_id": (
                str(self.worker_item_id) if self.worker_item_id is not None else None
            ),
            "lineage_id": str(self.lineage_id),
            "attempt": self.attempt,
            "status": self.status,
            "assigned_worker_id": (
                str(self.assigned_worker_id)
                if self.assigned_worker_id is not None
                else None
            ),
            "pinned_worker_id": (
                str(self.pinned_worker_id) if self.pinned_worker_id is not None else None
            ),
            "version": self.version,
            "capability_requirements": list(self.capability_requirements),
            "adapter_preferences": list(self.adapter_preferences),
            "adapter_forbidden": list(self.adapter_forbidden),
            "session_id": str(self.session_id) if self.session_id is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RevisionSessionFactV1:
    session_id: UUID
    execution_id: UUID
    worker_id: UUID
    status: str
    last_sequence: int
    last_activity_at: datetime
    input_mode: str
    version: int
    capability_snapshot: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _uuid(self.session_id, "session_id")
        _uuid(self.execution_id, "session execution_id")
        _uuid(self.worker_id, "session worker_id")
        _required_text(self.status, "session status")
        _non_negative(self.last_sequence, "session last_sequence")
        _aware(self.last_activity_at, "session last_activity_at")
        _required_text(self.input_mode, "session input_mode")
        _positive(self.version, "session version")
        if self.capability_snapshot is not None:
            object.__setattr__(
                self,
                "capability_snapshot",
                _freeze_json(self.capability_snapshot, "session capability_snapshot"),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "session_id": str(self.session_id),
            "execution_id": str(self.execution_id),
            "worker_id": str(self.worker_id),
            "status": self.status,
            "last_sequence": self.last_sequence,
            "last_activity_at": self.last_activity_at.astimezone(UTC).isoformat(),
            "input_mode": self.input_mode,
            "version": self.version,
            "capability_snapshot": _plain_json(self.capability_snapshot),
        }


@dataclass(frozen=True, slots=True)
class CreatePlanningContextV1:
    """Discriminator for the existing create planning input."""

    context_materialization: ContextMaterializationV1 | None = None
    schema_version: int = PLANNING_CONTEXT_SCHEMA_VERSION
    kind: Literal["create"] = "create"

    def __post_init__(self) -> None:
        if self.schema_version != PLANNING_CONTEXT_SCHEMA_VERSION:
            raise ValidationError("unsupported create planning context schema_version")
        if self.kind != "create":
            raise ValidationError("create planning context kind must be create")
        if self.context_materialization is not None and not isinstance(
            self.context_materialization, ContextMaterializationV1
        ):
            raise TypeError("create context_materialization must be typed")


@dataclass(frozen=True, slots=True)
class RevisionPlanningContextV1:
    schema_version: int
    base_descriptor: RevisionBaseDescriptorV1
    plan_snapshot_payload: Mapping[str, object]
    tasks: tuple[RevisionTaskFactV1, ...]
    worker_items: tuple[RevisionWorkerItemFactV1, ...]
    executions: tuple[RevisionExecutionFactV1, ...]
    sessions: tuple[RevisionSessionFactV1, ...]
    context_package_refs: tuple[ConfirmedContextPackageRefV1, ...]
    adapter_capabilities: tuple[AdapterCapabilitySnapshotV1, ...]
    max_context_bytes: int
    kind: Literal["revision"] = "revision"
    byte_size: int = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PLANNING_CONTEXT_SCHEMA_VERSION:
            raise ValidationError("unsupported revision planning context schema_version")
        if self.kind != "revision":
            raise ValidationError("revision planning context kind must be revision")
        if not isinstance(self.base_descriptor, RevisionBaseDescriptorV1):
            raise TypeError("revision context base_descriptor must be typed")
        _positive(self.max_context_bytes, "revision planning context max_context_bytes")
        object.__setattr__(
            self,
            "plan_snapshot_payload",
            _freeze_json(self.plan_snapshot_payload, "revision plan snapshot payload"),
        )
        object.__setattr__(self, "tasks", tuple(self.tasks))
        object.__setattr__(self, "worker_items", tuple(self.worker_items))
        object.__setattr__(self, "executions", tuple(self.executions))
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "context_package_refs", tuple(self.context_package_refs))
        object.__setattr__(self, "adapter_capabilities", tuple(self.adapter_capabilities))
        byte_size = len(self.canonical_bytes())
        if byte_size > self.max_context_bytes:
            raise ValidationError(
                "revision planning context exceeds byte budget "
                f"({byte_size} > {self.max_context_bytes})"
            )
        object.__setattr__(self, "byte_size", byte_size)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "base_descriptor": self.base_descriptor.to_mapping(),
            "plan_snapshot": _plain_json(self.plan_snapshot_payload),
            "tasks": [item.to_mapping() for item in self.tasks],
            "worker_items": [item.to_mapping() for item in self.worker_items],
            "executions": [item.to_mapping() for item in self.executions],
            "sessions": [item.to_mapping() for item in self.sessions],
            "context_package_refs": [
                item.to_mapping() for item in self.context_package_refs
            ],
            "adapter_capabilities": [
                item.to_mapping() for item in self.adapter_capabilities
            ],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


type PlanningContextV1 = CreatePlanningContextV1 | RevisionPlanningContextV1


class RevisionPlanningContextBuilder:
    """Build a deterministic planner view from already-loaded domain facts."""

    def __init__(self, *, max_context_bytes: int | None = None) -> None:
        if max_context_bytes is not None:
            _positive(max_context_bytes, "max_context_bytes")
        self._max_context_bytes = max_context_bytes

    def build(
        self,
        *,
        request: PlanningRequest,
        snapshot: TaskPlanSnapshot,
        tasks: Sequence[Task],
        worker_items: Sequence[WorkerItem],
        executions: Sequence[Execution],
        sessions: Sequence[ExecutionSession],
        context_package_refs: Sequence[
            ConfirmedContextPackageRefV1 | ContextMetadata | ContextMaterializationV1 | str
        ],
        adapter_capabilities: Sequence[AdapterCapabilitySnapshotV1],
    ) -> RevisionPlanningContextV1:
        if not isinstance(request, PlanningRequest):
            raise TypeError("revision planning context request must be typed")
        if request.kind is not PlanningRequestKind.REVISION:
            raise ValidationError("revision planning context requires a revision request")
        base = request.base_descriptor
        if base is None:
            raise ValidationError("revision planning context requires an exact base")
        if snapshot.id != base.plan_snapshot_id:
            raise ValidationError("revision base snapshot ID does not match snapshot")
        if snapshot.workspace_id != base.workspace_id:
            raise ValidationError("revision base workspace does not match snapshot")
        if snapshot.root_task_id != base.root_task_id:
            raise ValidationError("revision base root task does not match snapshot")
        if snapshot.generation != base.plan_generation:
            raise ValidationError("revision base generation does not match snapshot")
        actual_hash = plan_snapshot_content_hash(snapshot)
        if actual_hash != base.plan_content_hash:
            raise ValidationError("revision base plan content hash does not match snapshot")

        plan_payload = _normalize_plan_payload(snapshot.to_payload())
        task_facts = _build_task_facts(snapshot, tasks)
        worker_facts, worker_ids = _build_worker_facts(snapshot, worker_items)
        execution_facts, active_execution_ids = _build_execution_facts(
            executions,
            task_ids={item.task_id for item in task_facts},
            worker_ids=worker_ids,
        )
        session_facts = _build_session_facts(
            sessions,
            execution_ids=active_execution_ids,
        )
        session_by_execution: dict[UUID, UUID] = {}
        for item in session_facts:
            session_by_execution.setdefault(item.execution_id, item.session_id)
        execution_facts = tuple(
            RevisionExecutionFactV1(
                execution_id=item.execution_id,
                task_id=item.task_id,
                worker_item_id=item.worker_item_id,
                lineage_id=item.lineage_id,
                attempt=item.attempt,
                status=item.status,
                assigned_worker_id=item.assigned_worker_id,
                pinned_worker_id=item.pinned_worker_id,
                version=item.version,
                capability_requirements=item.capability_requirements,
                adapter_preferences=item.adapter_preferences,
                adapter_forbidden=item.adapter_forbidden,
                session_id=session_by_execution.get(item.execution_id),
            )
            for item in execution_facts
        )
        normalized_context_refs = tuple(
            sorted(
                (_context_package_ref(item) for item in context_package_refs),
                key=lambda item: (item.ref, item.package_hash or "", item.context_version or 0),
            )
        )
        normalized_adapters = tuple(
            sorted(adapter_capabilities, key=lambda item: item.adapter_ref)
        )
        return RevisionPlanningContextV1(
            schema_version=PLANNING_CONTEXT_SCHEMA_VERSION,
            base_descriptor=base,
            plan_snapshot_payload=plan_payload,
            tasks=task_facts,
            worker_items=worker_facts,
            executions=execution_facts,
            sessions=session_facts,
            context_package_refs=normalized_context_refs,
            adapter_capabilities=normalized_adapters,
            max_context_bytes=(
                self._max_context_bytes
                or request.profile_snapshot.budget.max_context_bytes
            ),
        )


def _build_task_facts(
    snapshot: TaskPlanSnapshot,
    tasks: Sequence[Task],
) -> tuple[RevisionTaskFactV1, ...]:
    task_by_id = _unique_by_id(tasks, "task")
    expected: dict[UUID, str] = {snapshot.root_task_id: "root"}
    expected.update(
        {
            item.task_id: item.key
            for item in snapshot.subtasks
            if item.task_id is not None
        }
    )
    missing = sorted(set(expected) - set(task_by_id), key=str)
    if missing:
        raise ValidationError(
            "revision planning context is missing current tasks: "
            + ", ".join(str(item) for item in missing)
        )
    facts = [
        RevisionTaskFactV1(
            task_id=task_id,
            plan_key=plan_key,
            title=task_by_id[task_id].title,
            status=_enum_or_text(task_by_id[task_id].status),
            version=task_by_id[task_id].version,
        )
        for task_id, plan_key in expected.items()
    ]
    return tuple(sorted(facts, key=lambda item: (item.plan_key, str(item.task_id))))


def _build_worker_facts(
    snapshot: TaskPlanSnapshot,
    worker_items: Sequence[WorkerItem],
) -> tuple[tuple[RevisionWorkerItemFactV1, ...], set[UUID]]:
    worker_by_id = _unique_by_id(worker_items, "WorkerItem")
    facts: list[RevisionWorkerItemFactV1] = []
    for placement in snapshot.worker_item_plans:
        if placement.worker_item_id is None:
            continue
        item = worker_by_id.get(placement.worker_item_id)
        if item is None:
            raise ValidationError(
                "revision planning context is missing current WorkerItem: "
                f"{placement.worker_item_id}"
            )
        if item.workspace_id != snapshot.workspace_id or item.root_task_id != snapshot.root_task_id:
            raise ValidationError("WorkerItem does not belong to the revision root")
        facts.append(
            RevisionWorkerItemFactV1(
                worker_item_id=item.id,
                stable_key=item.stable_key,
                subtask_key=placement.subtask_key,
                title=item.title,
                role=placement.role,
                responsibility=item.responsibility,
                status=_enum_or_text(item.status),
                context_version=item.context_version,
                acknowledged_context_version=item.acknowledged_context_version,
                version=item.version,
                capability_requirements=placement.capability_requirements,
            )
        )
    facts.sort(key=lambda item: (item.stable_key, str(item.worker_item_id)))
    return tuple(facts), {item.worker_item_id for item in facts}


def _build_execution_facts(
    executions: Sequence[Execution],
    *,
    task_ids: set[UUID],
    worker_ids: set[UUID],
) -> tuple[tuple[RevisionExecutionFactV1, ...], set[UUID]]:
    seen: set[UUID] = set()
    facts: list[RevisionExecutionFactV1] = []
    for execution in executions:
        if execution.id in seen:
            raise ValidationError("revision planning context contains duplicate execution")
        seen.add(execution.id)
        if execution.task_id not in task_ids:
            continue
        if execution.worker_item_id is not None and execution.worker_item_id not in worker_ids:
            continue
        if execution.status in TERMINAL_EXECUTION_STATUSES:
            continue
        facts.append(
            RevisionExecutionFactV1(
                execution_id=execution.id,
                task_id=execution.task_id,
                worker_item_id=execution.worker_item_id,
                lineage_id=execution.lineage_id,
                attempt=execution.attempt,
                status=_enum_or_text(execution.status),
                assigned_worker_id=execution.assigned_worker_id,
                pinned_worker_id=execution.pinned_worker_id,
                version=execution.version,
                capability_requirements=tuple(execution.capability_requirements),
                adapter_preferences=execution.adapter_preferences,
                adapter_forbidden=tuple(execution.adapter_forbidden),
            )
        )
    facts.sort(key=lambda item: (str(item.task_id), str(item.worker_item_id), str(item.execution_id)))
    return tuple(facts), {item.execution_id for item in facts}


def _build_session_facts(
    sessions: Sequence[ExecutionSession],
    *,
    execution_ids: set[UUID],
) -> tuple[RevisionSessionFactV1, ...]:
    seen: set[UUID] = set()
    facts: list[RevisionSessionFactV1] = []
    for session in sessions:
        if session.id in seen:
            raise ValidationError("revision planning context contains duplicate session")
        seen.add(session.id)
        if session.execution_id not in execution_ids:
            continue
        if session.status is not ExecutionSessionStatus.ACTIVE:
            continue
        facts.append(
            RevisionSessionFactV1(
                session_id=session.id,
                execution_id=session.execution_id,
                worker_id=session.worker_id,
                status=_enum_or_text(session.status),
                last_sequence=session.last_sequence,
                last_activity_at=session.last_activity_at,
                input_mode=session.input_mode,
                version=session.version,
                capability_snapshot=_worker_capability_mapping(session),
            )
        )
    facts.sort(key=lambda item: (str(item.execution_id), str(item.session_id)))
    return tuple(facts)


def _worker_capability_mapping(session: ExecutionSession) -> Mapping[str, object] | None:
    profile = session.capability_snapshot
    if profile is None:
        return None
    return {
        "input": profile.input.value,
        "idempotency": profile.idempotency.value,
        "resume": profile.resume.value,
        "stop": profile.stop.value,
        "sandbox": profile.sandbox.value,
        "stability": profile.stability.value,
        "context_package_versions": list(profile.context_package_versions),
    }


def _context_package_ref(
    value: ConfirmedContextPackageRefV1
    | ContextMetadata
    | ContextMaterializationV1
    | str,
) -> ConfirmedContextPackageRefV1:
    if isinstance(value, ConfirmedContextPackageRefV1):
        return value
    if isinstance(value, ContextMaterializationV1):
        value = value.metadata
    if isinstance(value, ContextMetadata):
        return ConfirmedContextPackageRefV1(
            ref=context_package_reference(value.package_id, value.package_hash),
            package_hash=value.package_hash,
            context_version=value.context_version,
        )
    if isinstance(value, str):
        return ConfirmedContextPackageRefV1(ref=value)
    raise TypeError("context package refs must be typed or stable refs")


def _normalize_plan_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    normalized = dict(payload)
    for key, sort_key in (
        ("subtasks", "key"),
        ("worker_items", "key"),
        ("milestones", "key"),
    ):
        values = normalized.get(key)
        if isinstance(values, list):
            normalized[key] = sorted(
                values,
                key=lambda item: _mapping_sort_key(item, sort_key),
            )
    for key, primary, secondary in (
        (
            "subtask_dependencies",
            "upstream_subtask_key",
            "downstream_subtask_key",
        ),
        (
            "worker_item_dependencies",
            "upstream_worker_item_key",
            "downstream_worker_item_key",
        ),
    ):
        values = normalized.get(key)
        if isinstance(values, list):
            normalized[key] = sorted(
                values,
                key=lambda item: (
                    _mapping_sort_key(item, primary),
                    _mapping_sort_key(item, secondary),
                    json.dumps(item, sort_keys=True),
                ),
            )
    return cast(
        Mapping[str, object],
        _freeze_json(normalized, "revision plan snapshot payload"),
    )


def _mapping_sort_key(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        raise ValidationError("revision plan payload collection must contain mappings")
    item = value.get(key)
    if not isinstance(item, str):
        raise ValidationError(f"revision plan payload item is missing {key}")
    return item


def _unique_by_id[T](items: Sequence[T], label: str) -> dict[UUID, T]:
    result: dict[UUID, T] = {}
    for item in items:
        item_id = getattr(item, "id", None)
        if not isinstance(item_id, UUID):
            raise TypeError(f"{label} facts must expose a UUID id")
        if item_id in result:
            raise ValidationError(f"revision planning context contains duplicate {label}")
        result[item_id] = item
    return result


def _freeze_json(value: object, field_name: str) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise ValidationError(f"{field_name} cannot contain floating point values")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{field_name} keys must be strings")
            result[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item, field_name) for item in value)
    raise ValidationError(f"{field_name} contains unsupported value")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _enum_or_text(value: object) -> str:
    result = getattr(value, "value", value)
    return _required_text(result, "enum text")


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} is required")
    return value.strip()


def _sorted_texts(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValidationError(f"{field_name} must be a sequence")
    cleaned = []
    for value in values:
        cleaned.append(_required_text(value, field_name))
    return tuple(sorted(set(cleaned)))


def _positive(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{field_name} must be positive")
    return value


def _non_negative(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{field_name} must be non-negative")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone aware")
    return value


def _uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValidationError(f"{field_name} must be a UUID")
    return value


def _optional_uuid(value: object, field_name: str) -> None:
    if value is not None:
        _uuid(value, field_name)


def _sha256(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValidationError(f"{field_name} must be a lowercase SHA-256 hash")
    return value
