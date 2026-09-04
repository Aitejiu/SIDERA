from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import JsonValue

# Keep the storage key stable so an installed v1 plugin-data document can be
# migrated in place without losing the host-managed namespace.
STATE_KEY = "sidera_task_bridge_state_v1"
IDENTITY_FIELDS = frozenset({"command_id", "correlation_id", "idempotency_key"})
type ReserveDisposition = Literal["new", "recovered", "busy"]
type ResourceKind = Literal["planning_request", "task"]
type SourceType = Literal["akashic_private", "sidera_web_chat"]


class KeyValueStore(Protocol):
    def get(self, key: str, default: object = None) -> object: ...

    def set(self, key: str, value: object) -> None: ...


class StateConflict(RuntimeError):
    """A command or state mutation conflicts with durable plugin state."""


class StateCorrupt(RuntimeError):
    """The plugin-data document cannot be trusted or understood."""


class PrivateRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    chat_id: str
    session_key: str
    control_plane_route_id: UUID | None = None


class OutboundCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    resource_lock_key: str
    request_json: dict[str, JsonValue]
    created_at: datetime
    last_attempted_at: datetime
    attempt_count: int = Field(ge=0)


class SettledCommandAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    outcome: Literal["applied", "rejected"]
    error_code: str | None
    settled_at: datetime


class ResourceSourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_kind: ResourceKind
    resource_id: UUID
    source_type: SourceType
    source_ref: str
    bound_at: datetime


class PendingNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notice_id: str
    cursor: int = Field(ge=1)
    resource_key: str
    source_ref: str
    kind: str
    message: str
    web_url: str | None = None
    created_at: datetime
    last_attempted_at: datetime | None = None
    attempts: int = Field(default=0, ge=0)


class NotificationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    version: int | None = Field(default=None, ge=0)
    milestone_fingerprint: str | None = None
    attention_fingerprint: str | None = None


class UndeliverableAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notice_id: str
    resource_key: str
    kind: str
    reason_code: str
    recorded_at: datetime


class BridgeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    unresolved_commands: dict[str, OutboundCommand] = Field(default_factory=dict)
    source_routes: dict[str, PrivateRoute] = Field(default_factory=dict)
    settled_commands: list[SettledCommandAudit] = Field(default_factory=list)
    resource_sources: dict[str, ResourceSourceBinding] = Field(default_factory=dict)
    notification_cursor: int = Field(default=0, ge=0)
    pending_notifications: list[PendingNotification] = Field(default_factory=list)
    notification_snapshots: dict[str, NotificationSnapshot] = Field(
        default_factory=dict
    )
    undeliverable_notifications: list[UndeliverableAudit] = Field(default_factory=list)


class _LegacyBridgeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    unresolved_commands: dict[str, OutboundCommand] = Field(default_factory=dict)
    source_routes: dict[str, PrivateRoute] = Field(default_factory=dict)
    settled_commands: list[SettledCommandAudit] = Field(default_factory=list)


class BridgeStateStore:
    """Serialize all plugin-data mutations through one in-process lock."""

    def __init__(self, kv_store: KeyValueStore) -> None:
        self._kv_store = kv_store
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()

    async def snapshot(self) -> BridgeState:
        async with self._lock:
            return self._load()

    async def get_unresolved(self, command_id: UUID) -> OutboundCommand | None:
        async with self._lock:
            return self._load().unresolved_commands.get(str(command_id))

    async def list_unresolved(self) -> tuple[OutboundCommand, ...]:
        async with self._lock:
            state = self._load()
            return tuple(
                sorted(
                    state.unresolved_commands.values(),
                    key=lambda item: (item.created_at, str(item.command_id)),
                )
            )

    async def list_resource(
        self, resource_lock_key: str
    ) -> tuple[OutboundCommand, ...]:
        async with self._lock:
            state = self._load()
            return tuple(
                sorted(
                    (
                        item
                        for item in state.unresolved_commands.values()
                        if item.resource_lock_key == resource_lock_key
                    ),
                    key=lambda item: (item.created_at, str(item.command_id)),
                )
            )

    async def reserve(self, command: OutboundCommand) -> OutboundCommand:
        """Persist an exact command before the caller sends it externally."""

        async with self._lock:
            state = self._load()
            key = str(command.command_id)
            existing = state.unresolved_commands.get(key)
            if existing is not None:
                if existing != command:
                    raise StateConflict("command identity already has different state")
                return existing
            state.unresolved_commands[key] = command
            self._save(state)
            return command

    async def reserve_or_recover(
        self, command: OutboundCommand
    ) -> tuple[OutboundCommand, ReserveDisposition]:
        """Atomically reserve a command or identify an existing resource lock."""

        async with self._lock:
            state = self._load()
            key = str(command.command_id)
            existing = state.unresolved_commands.get(key)
            if existing is not None:
                if (
                    existing.resource_lock_key != command.resource_lock_key
                    or existing.request_json != command.request_json
                ):
                    raise StateConflict("command identity already has different state")
                return existing, "recovered"

            for existing in state.unresolved_commands.values():
                if existing.resource_lock_key != command.resource_lock_key:
                    continue
                if same_logical_request(existing.request_json, command.request_json):
                    return existing, "recovered"
                return existing, "busy"

            state.unresolved_commands[key] = command
            self._save(state)
            return command, "new"

    async def record_attempt(self, command_id: UUID, *, at: datetime) -> None:
        async with self._lock:
            state = self._load()
            key = str(command_id)
            existing = state.unresolved_commands.get(key)
            if existing is None:
                raise StateConflict("unresolved command does not exist")
            state.unresolved_commands[key] = existing.model_copy(
                update={
                    "last_attempted_at": at,
                    "attempt_count": existing.attempt_count + 1,
                }
            )
            self._save(state)

    async def settle(
        self,
        command_id: UUID,
        *,
        outcome: Literal["applied", "rejected"],
        error_code: str | None,
        settled_at: datetime | None = None,
    ) -> None:
        async with self._lock:
            state = self._load()
            key = str(command_id)
            if key not in state.unresolved_commands:
                raise StateConflict("cannot settle an unknown command")
            del state.unresolved_commands[key]
            state.settled_commands.append(
                SettledCommandAudit(
                    command_id=command_id,
                    outcome=outcome,
                    error_code=error_code,
                    settled_at=settled_at or datetime.now().astimezone(),
                )
            )
            state.settled_commands = state.settled_commands[-128:]
            self._save(state)

    async def settle_applied_command(
        self,
        command_id: UUID,
        *,
        planning_request_id: UUID,
        source_type: SourceType,
        source_ref: str,
        settled_at: datetime | None = None,
    ) -> None:
        """Settle a submitted brief and bind its owner in one durable write."""

        async with self._lock:
            state = self._load()
            key = str(command_id)
            if key not in state.unresolved_commands:
                raise StateConflict("cannot settle an unknown command")
            resource_key = _resource_key("planning_request", planning_request_id)
            binding = ResourceSourceBinding(
                resource_kind="planning_request",
                resource_id=planning_request_id,
                source_type=source_type,
                source_ref=source_ref,
                bound_at=settled_at or datetime.now().astimezone(),
            )
            existing = state.resource_sources.get(resource_key)
            if existing is not None and (
                existing.source_type != binding.source_type
                or existing.source_ref != binding.source_ref
            ):
                raise StateConflict("resource source binding already exists")
            if existing is None:
                state.resource_sources[resource_key] = binding
            del state.unresolved_commands[key]
            state.settled_commands.append(
                SettledCommandAudit(
                    command_id=command_id,
                    outcome="applied",
                    error_code=None,
                    settled_at=settled_at or datetime.now().astimezone(),
                )
            )
            state.settled_commands = state.settled_commands[-128:]
            self._save(state)

    async def save_route(self, source_ref: str, route: PrivateRoute) -> None:
        async with self._lock:
            state = self._load()
            existing = state.source_routes.get(source_ref)
            if existing is not None:
                if (
                    existing.channel != route.channel
                    or existing.chat_id != route.chat_id
                    or existing.session_key != route.session_key
                ):
                    raise StateConflict("source route already has different state")
                if (
                    existing.control_plane_route_id is not None
                    and existing.control_plane_route_id != route.control_plane_route_id
                ):
                    raise StateConflict("source route already has different state")
                if existing.control_plane_route_id == route.control_plane_route_id:
                    return
                route = existing.model_copy(
                    update={"control_plane_route_id": route.control_plane_route_id}
                )
            state.source_routes[source_ref] = route
            self._save(state)

    async def list_private_routes(self) -> tuple[tuple[str, PrivateRoute], ...]:
        """Return opaque private routes in stable order for Adapter bootstrap."""

        async with self._lock:
            state = self._load()
            return tuple(sorted(state.source_routes.items(), key=lambda item: item[0]))

    async def bind_resource_source(
        self,
        kind: ResourceKind,
        resource_id: UUID,
        source_type: SourceType,
        source_ref: str,
        *,
        bound_at: datetime | None = None,
    ) -> ResourceSourceBinding:
        """Bind one control-plane resource to its original user source once."""

        binding = ResourceSourceBinding(
            resource_kind=kind,
            resource_id=resource_id,
            source_type=source_type,
            source_ref=source_ref,
            bound_at=bound_at or datetime.now().astimezone(),
        )
        async with self._lock:
            state = self._load()
            key = _resource_key(kind, resource_id)
            existing = state.resource_sources.get(key)
            if existing is not None:
                if (
                    existing.resource_kind != binding.resource_kind
                    or existing.resource_id != binding.resource_id
                    or existing.source_type != binding.source_type
                    or existing.source_ref != binding.source_ref
                ):
                    raise StateConflict("resource source binding already exists")
                return existing
            state.resource_sources[key] = binding
            self._save(state)
            return binding

    async def bind_materialized_task(
        self,
        request_id: UUID,
        root_task_id: UUID,
        *,
        bound_at: datetime | None = None,
    ) -> ResourceSourceBinding:
        """Inherit a PlanningRequest owner when it materializes a root Task."""

        async with self._lock:
            state = self._load()
            request_key = _resource_key("planning_request", request_id)
            request_binding = state.resource_sources.get(request_key)
            if request_binding is None:
                raise StateConflict("planning request source binding is missing")
            binding = ResourceSourceBinding(
                resource_kind="task",
                resource_id=root_task_id,
                source_type=request_binding.source_type,
                source_ref=request_binding.source_ref,
                bound_at=bound_at or datetime.now().astimezone(),
            )
            task_key = _resource_key("task", root_task_id)
            existing = state.resource_sources.get(task_key)
            if existing is not None:
                if (
                    existing.source_type != binding.source_type
                    or existing.source_ref != binding.source_ref
                ):
                    raise StateConflict("resource source binding already exists")
                return existing
            state.resource_sources[task_key] = binding
            self._save(state)
            return binding

    async def list_private_planning_bindings(
        self,
    ) -> tuple[ResourceSourceBinding, ...]:
        async with self._lock:
            state = self._load()
            return tuple(
                sorted(
                    (
                        binding
                        for binding in state.resource_sources.values()
                        if binding.resource_kind == "planning_request"
                        and binding.source_type == "akashic_private"
                    ),
                    key=lambda item: str(item.resource_id),
                )
            )

    async def wait_for_change(self) -> None:
        """Wait for a durable state mutation without polling the KV store."""

        await self._changed.wait()
        self._changed.clear()

    async def enqueue_notification(self, notice: PendingNotification) -> None:
        async with self._lock:
            state = self._load()
            existing = next(
                (
                    item
                    for item in state.pending_notifications
                    if item.notice_id == notice.notice_id
                ),
                None,
            )
            if existing is not None:
                immutable_fields = (
                    "cursor",
                    "resource_key",
                    "source_ref",
                    "kind",
                    "message",
                    "web_url",
                )
                if any(
                    getattr(existing, field) != getattr(notice, field)
                    for field in immutable_fields
                ):
                    raise StateConflict(
                        "notification identity already has different state"
                    )
                return
            if notice.cursor < state.notification_cursor:
                raise StateConflict("notification cursor already advanced")
            state.pending_notifications.append(notice)
            state.pending_notifications.sort(
                key=lambda item: (item.cursor, item.created_at, item.notice_id)
            )
            self._save(state)

    async def list_pending_notifications(self) -> tuple[PendingNotification, ...]:
        async with self._lock:
            state = self._load()
            return tuple(state.pending_notifications)

    async def record_notification_attempt(
        self, notice_id: str, *, at: datetime
    ) -> PendingNotification:
        async with self._lock:
            state = self._load()
            for index, notice in enumerate(state.pending_notifications):
                if notice.notice_id != notice_id:
                    continue
                updated = notice.model_copy(
                    update={
                        "last_attempted_at": at,
                        "attempts": notice.attempts + 1,
                    }
                )
                state.pending_notifications[index] = updated
                self._save(state)
                return updated
            raise StateConflict("pending notification does not exist")

    async def settle_notification(
        self, notice_id: str, *, terminal: bool = True
    ) -> None:
        del terminal
        async with self._lock:
            state = self._load()
            remaining = [
                notice
                for notice in state.pending_notifications
                if notice.notice_id != notice_id
            ]
            if len(remaining) == len(state.pending_notifications):
                return
            state.pending_notifications = remaining
            self._save(state)

    async def advance_notification_cursor(self, cursor: int) -> None:
        if cursor < 0:
            raise StateConflict("notification cursor must be non-negative")
        async with self._lock:
            state = self._load()
            if cursor < state.notification_cursor:
                raise StateConflict("notification cursor cannot regress")
            if any(notice.cursor < cursor for notice in state.pending_notifications):
                raise StateConflict("notification cursor would cross pending notice")
            if cursor == state.notification_cursor:
                return
            state.notification_cursor = cursor
            self._save(state)

    async def save_notification_snapshot(
        self, resource_key: str, snapshot: NotificationSnapshot
    ) -> None:
        async with self._lock:
            state = self._load()
            state.notification_snapshots[resource_key] = snapshot
            state.notification_snapshots = dict(
                list(state.notification_snapshots.items())[-128:]
            )
            self._save(state)

    async def record_undeliverable(self, audit: UndeliverableAudit) -> None:
        async with self._lock:
            state = self._load()
            state.undeliverable_notifications.append(audit)
            state.undeliverable_notifications = state.undeliverable_notifications[-128:]
            self._save(state)

    def _load(self) -> BridgeState:
        raw = self._kv_store.get(STATE_KEY, None)
        if raw is None:
            return BridgeState()
        if not isinstance(raw, dict):
            raise StateCorrupt("task bridge state is invalid")
        try:
            schema_version = raw.get("schema_version")
            if schema_version == 1:
                legacy = _LegacyBridgeState.model_validate(raw)
                migrated = BridgeState(
                    unresolved_commands=legacy.unresolved_commands,
                    source_routes=legacy.source_routes,
                    settled_commands=legacy.settled_commands,
                )
                self._save(migrated)
                return migrated
            if schema_version != 2:
                raise StateCorrupt("unsupported task bridge state schema")
            state = BridgeState.model_validate(raw)
            notice_ids = [notice.notice_id for notice in state.pending_notifications]
            if len(notice_ids) != len(set(notice_ids)):
                raise StateCorrupt("duplicate pending notification id")
            return state
        except StateCorrupt:
            raise
        except ValidationError as exc:
            raise StateCorrupt("task bridge state is invalid") from exc

    def _save(self, state: BridgeState) -> None:
        self._kv_store.set(STATE_KEY, state.model_dump(mode="json"))
        self._changed.set()


def _resource_key(kind: ResourceKind, resource_id: UUID) -> str:
    return f"{kind}:{resource_id}"


def same_logical_request(
    left: dict[str, JsonValue], right: dict[str, JsonValue]
) -> bool:
    """Compare command intent while ignoring generated transport identities."""

    return {
        key: value for key, value in left.items() if key not in IDENTITY_FIELDS
    } == {key: value for key, value in right.items() if key not in IDENTITY_FIELDS}
