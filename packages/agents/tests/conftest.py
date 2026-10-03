import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sidera_agents.contracts import (
    AssembledInput,
    CapabilityResourceDraft,
    CapabilitySupport,
    ConfigValidation,
    NativeCancelOutcome,
    NativeCancelState,
    NativeCloseOutcome,
    NativeCloseState,
    NativeEvent,
    NativeHandle,
    NativeInspection,
    NativeOpenRequest,
    NativeOutcome,
    NativeOutcomeStatus,
    NativeProcessState,
    NativeResponseState,
    ProcessIdentity,
    ProfileDraft,
    ProtocolState,
    RuntimeCandidate,
    RuntimeDescriptor,
    RuntimeProbe,
    RuntimeRef,
    SupportStatus,
)
from sidera_agents.errors import AgentError

_SCRATCH_URL_ENV = "SIDERA_AGENTS_TEST_DATABASE_URL"
_SCRATCH_TARGET_FAILURE = (
    "The dedicated PostgreSQL test target is unsafe or unavailable."
)
_SCRATCH_CLEANUP_FAILURE = (
    "The owned PostgreSQL test target could not be safely cleaned."
)
_OWNED_TABLES = {
    "alembic_version",
    "profile_heads",
    "profile_revisions",
    "resource_heads",
    "resource_revisions",
    "runtime_installations",
    "runtime_references",
    "run_operations",
    "run_events",
    "run_inputs",
    "runs",
    "run_reservations",
}


class _ScratchTargetNotConfigured(Exception):
    pass


class _ScratchTargetUnavailable(Exception):
    pass


class _ScratchTargetCleanupFailed(Exception):
    pass


@dataclass(frozen=True)
class _OwnedAgentsPostgres:
    engine: Any = field(repr=False)
    url: Any = field(repr=False)
    lock_connection: Any = field(repr=False)
    configured_name: str

    def migrate(self, revision: str | None = None) -> None:
        _run_agents_migration(self.engine, revision)


def _is_local_host(host: object) -> bool:
    return isinstance(host, str) and (
        host.lower() in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")
    )


def _is_local_scratch_url(url: Any, environ: Mapping[str, str]) -> bool:
    query = {str(key).casefold(): value for key, value in url.query.items()}
    if (
        url.get_backend_name() != "postgresql"
        or not isinstance(url.database, str)
        or re.fullmatch(r"sidera_agents_test(?:_[a-z0-9_-]+)?", url.database) is None
        or "service" in query
        or "servicefile" in query
        or environ.get("PGSERVICE")
        or environ.get("PGSERVICEFILE")
        or environ.get("PGHOSTADDR")
    ):
        return False

    hosts: list[object] = []
    if url.host is not None:
        hosts.append(url.host)
    query_host = query.get("host")
    if query_host is not None:
        hosts.extend(
            query_host if isinstance(query_host, (tuple, list)) else (query_host,)
        )

    hostaddrs = query.get("hostaddr")
    if hostaddrs is not None:
        values = hostaddrs if isinstance(hostaddrs, (tuple, list)) else (hostaddrs,)
        if not all(_is_loopback_ip(value) for value in values):
            return False

    if hosts and not all(_is_local_host(host) for host in hosts):
        return False
    if hosts or hostaddrs is not None:
        return True

    environment_host = environ.get("PGHOST")
    return environment_host is None or _is_local_host(environment_host)


def _is_loopback_ip(value: object) -> bool:
    if not isinstance(value, str) or "%" in value:
        return False
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _scratch_database_identity(connection: Any) -> Any:
    from sqlalchemy import text

    return connection.execute(
        text(
            "SELECT current_database(), "
            "pg_get_userbyid(datdba) = current_user "
            "FROM pg_database WHERE datname = current_database()"
        )
    ).one()


def _scratch_target_facts(
    connection: Any, configured_name: str, identity: Any | None = None
) -> dict[str, object]:
    from sqlalchemy import text

    if identity is None:
        identity = _scratch_database_identity(connection)
    schemas = (
        connection.execute(
            text(
                "SELECT nspname FROM pg_namespace "
                "WHERE left(nspname, 3) <> 'pg_' "
                "AND nspname NOT IN ('information_schema', 'public')"
            )
        )
        .scalars()
        .all()
    )
    objects = connection.execute(
        text(
            "SELECT n.nspname, c.relname, "
            "CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' "
            "WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized_view' "
            "WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign_table' "
            "ELSE 'relation' END AS object_kind "
            "FROM pg_class AS c JOIN pg_namespace AS n "
            "ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('public', 'agents') "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
            "UNION ALL "
            "SELECT n.nspname, p.proname, 'function' "
            "FROM pg_proc AS p JOIN pg_namespace AS n "
            "ON n.oid = p.pronamespace "
            "WHERE n.nspname IN ('public', 'agents') "
            "UNION ALL "
            "SELECT n.nspname, t.typname, "
            "CASE t.typtype WHEN 'e' THEN 'enum' ELSE 'domain' END "
            "FROM pg_type AS t JOIN pg_namespace AS n "
            "ON n.oid = t.typnamespace "
            "WHERE n.nspname IN ('public', 'agents') "
            "AND t.typtype IN ('e', 'd')"
        )
    ).all()
    return {
        "database_name": identity[0],
        "configured_name": configured_name,
        "owner_matches": identity[1] is True,
        "custom_schemas": set(schemas),
        "objects": {tuple(row) for row in objects},
    }


def _acquire_owned_agents_pg(
    environ: Mapping[str, str],
    engine_factory: Callable[..., Any] | None = None,
) -> _OwnedAgentsPostgres:
    raw_url = environ.get(_SCRATCH_URL_ENV)
    if raw_url is None:
        raise _ScratchTargetNotConfigured

    try:
        from sqlalchemy.engine import make_url

        url = make_url(raw_url)
    except Exception:  # noqa: BLE001 - normalize URL parser details without echoing input
        raise _ScratchTargetUnavailable from None
    if not _is_local_scratch_url(url, environ):
        raise _ScratchTargetUnavailable

    if engine_factory is None:
        from sqlalchemy import create_engine

        engine_factory = create_engine

    engine = None
    connection = None
    lock_acquired = False
    try:
        from sqlalchemy import text

        engine = engine_factory(
            url,
            pool_pre_ping=True,
            echo=False,
            hide_parameters=True,
        )
        connection = engine.connect()
        identity = _scratch_database_identity(connection)
        if identity[0] != url.database or identity[1] is not True:
            raise _ScratchTargetUnavailable
        lock_acquired = (
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_class, :lock_id)"),
                {"lock_class": 1397310533, "lock_id": 2},
            ).scalar_one()
            is True
        )
        if not lock_acquired:
            raise _ScratchTargetUnavailable

        facts = _scratch_target_facts(connection, url.database, identity)
        if not _scratch_target_is_safe(facts):
            raise _ScratchTargetUnavailable
        return _OwnedAgentsPostgres(engine, url, connection, url.database)
    except Exception:  # noqa: BLE001 - sanitize driver errors and always release owned resources
        _dispose_scratch_connection(engine, connection, lock_acquired)
        raise _ScratchTargetUnavailable from None


def _dispose_scratch_connection(
    engine: Any, connection: Any, lock_acquired: bool
) -> bool:
    released = True
    if connection is not None and lock_acquired:
        try:
            from sqlalchemy import text

            released = (
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_class, :lock_id)"),
                    {"lock_class": 1397310533, "lock_id": 2},
                ).scalar_one()
                is True
            )
        except Exception:  # noqa: BLE001 - cleanup must fail closed on driver errors
            released = False
    if connection is not None:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - cleanup must fail closed on driver errors
            released = False
    if engine is not None:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 - cleanup must fail closed on driver errors
            released = False
    return released


def _run_agents_migration(engine: Any, revision: str | None) -> None:
    try:
        from alembic import command
        from alembic.config import Config

        package_dir = Path(__file__).resolve().parents[1]
        config = Config(str(package_dir / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            if revision == "base":
                command.downgrade(config, "base")
            else:
                command.upgrade(config, revision or "head")
    except Exception:  # noqa: BLE001 - hide migration/driver details from test output
        raise _ScratchTargetUnavailable from None


def _cleanup_owned_agents_pg(
    target: _OwnedAgentsPostgres,
    *,
    migration: Callable[[Any, str | None], None] | None = None,
) -> None:
    migration = migration or _run_agents_migration
    expected_objects = {("agents", name, "table") for name in _OWNED_TABLES}
    try:
        facts = _scratch_target_facts(target.lock_connection, target.configured_name)
        if (
            facts["database_name"] != target.configured_name
            or facts["owner_matches"] is not True
            or facts["custom_schemas"] - {"agents"}
            or facts["objects"] - expected_objects
        ):
            raise _ScratchTargetCleanupFailed

        object_names = {obj[1] for obj in facts["objects"]}
        if not object_names:
            return
        if "alembic_version" not in object_names:
            raise _ScratchTargetCleanupFailed
        if object_names - {"alembic_version"}:
            migration(target.engine, "base")

        after_downgrade = _scratch_target_facts(
            target.lock_connection, target.configured_name
        )
        version_object = ("agents", "alembic_version", "table")
        if (
            after_downgrade["database_name"] != target.configured_name
            or after_downgrade["owner_matches"] is not True
            or after_downgrade["custom_schemas"] - {"agents"}
            or after_downgrade["objects"] - {version_object}
        ):
            raise _ScratchTargetCleanupFailed
        if after_downgrade["objects"]:
            from sqlalchemy import text

            versions = target.lock_connection.execute(
                text("SELECT version_num FROM agents.alembic_version")
            ).all()
            if versions:
                raise _ScratchTargetCleanupFailed
            target.lock_connection.execute(text("DROP TABLE agents.alembic_version"))

        final_facts = _scratch_target_facts(
            target.lock_connection, target.configured_name
        )
        if (
            final_facts["database_name"] != target.configured_name
            or final_facts["owner_matches"] is not True
            or final_facts["custom_schemas"] - {"agents"}
            or final_facts["objects"]
        ):
            raise _ScratchTargetCleanupFailed
        target.lock_connection.commit()
    except _ScratchTargetCleanupFailed:
        raise
    except Exception:  # noqa: BLE001 - expose only the owned-target cleanup status
        raise _ScratchTargetCleanupFailed from None


def _scratch_target_is_safe(facts: dict[str, object]) -> bool:
    database_name = facts.get("database_name")
    configured_name = facts.get("configured_name")
    custom_schemas = facts.get("custom_schemas")
    objects = facts.get("objects")
    return (
        isinstance(database_name, str)
        and isinstance(configured_name, str)
        and re.fullmatch(r"sidera_agents_test(?:_[a-z0-9_-]+)?", database_name)
        is not None
        and database_name == configured_name
        and facts.get("owner_matches") is True
        and isinstance(custom_schemas, (set, frozenset))
        and custom_schemas <= {"agents"}
        and isinstance(objects, (set, frozenset, tuple, list))
        and not objects
    )


@pytest.fixture
def scratch_gate():
    return _scratch_target_is_safe


@pytest.fixture
def scratch_pg_acquire():
    return _acquire_owned_agents_pg


@pytest.fixture
def scratch_pg_cleanup():
    return _cleanup_owned_agents_pg


@pytest.fixture
def scratch_pg_release():
    return _dispose_scratch_connection


@pytest.fixture
def owned_agents_pg():
    try:
        target = _acquire_owned_agents_pg(os.environ)
    except _ScratchTargetNotConfigured:
        pytest.skip("SIDERA_AGENTS_TEST_DATABASE_URL is unset.")
    except Exception:  # noqa: BLE001 - do not echo target or driver details
        pytest.fail(_SCRATCH_TARGET_FAILURE, pytrace=False)

    cleanup_failed = False
    try:
        yield target
    finally:
        try:
            _cleanup_owned_agents_pg(target)
        except Exception:  # noqa: BLE001 - record cleanup failure without leaking driver data
            cleanup_failed = True
        released = _dispose_scratch_connection(
            target.engine, target.lock_connection, True
        )
        if cleanup_failed or not released:
            pytest.fail(_SCRATCH_CLEANUP_FAILURE, pytrace=False)


class OwnedWorkspaceAuthorizer:
    def __init__(self, workspace_id: UUID, grant_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.grant_id = grant_id
        self.calls: list[tuple[UUID, str, object]] = []

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        self.calls.append((grant_id, action, target))
        target_workspace = getattr(target, "workspace_id", target)
        if grant_id != self.grant_id or target_workspace != self.workspace_id:
            raise AgentError("permission_denied", "Access denied.")


class RuntimeLabAuthorizer:
    def __init__(self) -> None:
        self.workspace_grants: dict[UUID, UUID] = {}
        self.local_configuration_grants: set[UUID] = set()
        self.calls: list[tuple[UUID, str, object]] = []

    def grant_workspace(
        self, workspace_id: UUID, grant_id: UUID, *, local_configuration: bool = False
    ) -> None:
        self.workspace_grants[grant_id] = workspace_id
        if local_configuration:
            self.local_configuration_grants.add(grant_id)

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        self.calls.append((grant_id, action, target))
        if action == "runtime.configure":
            if grant_id in self.local_configuration_grants:
                return
            raise AgentError("permission_denied", "Access denied.")
        workspace_id = self.workspace_grants.get(grant_id)
        target_workspace = getattr(target, "workspace_id", target)
        if workspace_id is None or (
            isinstance(target_workspace, UUID) and target_workspace != workspace_id
        ):
            raise AgentError("permission_denied", "Access denied.")


@pytest.fixture
def catalog_lab():
    from sidera_agents.catalog import ProfileCatalog
    from sidera_agents.memory import MemoryAgentStore

    workspace_id = uuid4()
    grant_id = uuid4()
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    skill_draft = CapabilityResourceDraft(
        name="review",
        kind="skill",
        settings={"body": "Check tests"},
        secret_refs=(),
    )
    draft = ProfileDraft(
        name="reviewer",
        description="Reviews changes",
        runtime_ref=runtime_ref,
        instructions="Review carefully.",
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )
    authorizer = OwnedWorkspaceAuthorizer(workspace_id, grant_id)
    clock = lambda: datetime(2026, 10, 1, tzinfo=UTC)
    catalog = ProfileCatalog(
        MemoryAgentStore(),
        authorizer,
        clock,
    )
    return SimpleNamespace(
        catalog=catalog,
        workspace_id=workspace_id,
        runtime_ref=runtime_ref,
        draft=draft,
        grant_id=grant_id,
        skill_draft=skill_draft,
        authorizer=authorizer,
        clock=clock,
    )


@pytest.fixture
def runtime_lab(tmp_path):
    from sidera_agents.catalog import ProfileCatalog
    from sidera_agents.harnesses.registry import default_harness_registry
    from sidera_agents.memory import MemoryAgentStore
    from sidera_agents.runtimes import RuntimeCatalog

    workspace_id = uuid4()
    grant_id = uuid4()
    admin_grant_id = uuid4()
    authorizer = RuntimeLabAuthorizer()
    authorizer.grant_workspace(workspace_id, grant_id)
    authorizer.grant_workspace(workspace_id, admin_grant_id, local_configuration=True)
    store = MemoryAgentStore()
    clock = lambda: datetime(2026, 10, 1, tzinfo=UTC)
    registry = default_harness_registry()
    catalog = ProfileCatalog(store, authorizer, clock)
    runtimes = RuntimeCatalog(store, authorizer, registry=registry, clock=clock)
    skill_draft = CapabilityResourceDraft(
        name="review",
        kind="skill",
        settings={"body": "Check tests"},
        secret_refs=(),
    )
    executable = tmp_path / "opencode"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "for raw in sys.stdin:\n"
        "    request = json.loads(raw)\n"
        "    if request.get('method') == 'initialize':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':1,'agentCapabilities':{'mcpCapabilities':{}}}}) + '\\n')\n"
        "        sys.stdout.flush()\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return SimpleNamespace(
        store=store,
        authorizer=authorizer,
        workspace_id=workspace_id,
        grant_id=grant_id,
        admin_grant_id=admin_grant_id,
        registry=registry,
        catalog=catalog,
        runtimes=runtimes,
        skill_draft=skill_draft,
        executable=executable,
        clock=clock,
    )


class ControlledHarness:
    harness_id = "controlled"
    executable_name = "controlled"

    def __init__(self) -> None:
        self.open_calls: list[NativeOpenRequest] = []
        self.prompt_calls: list[tuple[NativeHandle, AssembledInput, UUID]] = []
        self.outcomes: dict[UUID, NativeOutcomeStatus] = {}
        self.inspections: dict[UUID, NativeInspection] = {}
        self.handles: dict[UUID, NativeHandle] = {}
        self.active_responses: dict[UUID, UUID] = {}
        self.response_lock = RLock()
        self.cancel_side_effects: list[tuple[UUID, UUID]] = []

    def describe(self) -> RuntimeDescriptor:
        return RuntimeDescriptor(
            harness_id=self.harness_id,
            display_name="Controlled test harness",
            fields=(),
            model_options=(),
            thinking_options=(),
        )

    def probe(self, candidate: RuntimeCandidate, checked_at=None) -> RuntimeProbe:
        del candidate
        checked_at = checked_at or datetime.now(UTC)
        return RuntimeProbe(
            actual_version="1.0",
            protocol_state=ProtocolState.READY,
            capabilities={
                "prompt": CapabilitySupport(
                    status=SupportStatus.SUPPORTED,
                    reason_code="supported",
                    constraints={},
                )
            },
            checked_at=checked_at,
        )

    def validate(self, profile, runtime) -> ConfigValidation:
        del profile, runtime
        return ConfigValidation(valid=True, issues=(), effective_preview=None)

    def open(self, request: NativeOpenRequest) -> NativeHandle:
        self.open_calls.append(request)
        handle = NativeHandle(
            run_id=request.run_id,
            session_id=f"session-{request.run_id}",
            process_identity=ProcessIdentity(
                pid=1,
                start_identity="controlled-process-start",
                owned_nonce=uuid4(),
            ),
            capabilities=("prompt",),
        )
        self.handles[request.run_id] = handle
        self.inspections[request.run_id] = NativeInspection(
            process_state=NativeProcessState.ALIVE,
            response_state=NativeResponseState.ACTIVE,
            reason_code=None,
        )
        return handle

    def begin_prompt(
        self, handle: NativeHandle, input: AssembledInput, response_id: UUID
    ) -> None:
        with self.response_lock:
            if handle.run_id in self.active_responses:
                raise AssertionError(
                    "controlled harness already has an active response"
                )
            self.active_responses[handle.run_id] = response_id
            self.prompt_calls.append((handle, input, response_id))
            self.inspections[handle.run_id] = NativeInspection(
                process_state=NativeProcessState.ALIVE,
                response_state=NativeResponseState.ACTIVE,
                reason_code=None,
            )

    def finish_prompt(
        self,
        handle: NativeHandle,
        response_id: UUID,
        status: NativeOutcomeStatus,
    ) -> NativeOutcome:
        with self.response_lock:
            if self.active_responses.get(handle.run_id) == response_id:
                self.active_responses.pop(handle.run_id, None)
            response_state = (
                NativeResponseState.IDLE
                if status
                in {
                    NativeOutcomeStatus.COMPLETED,
                    NativeOutcomeStatus.CANCELLED,
                    NativeOutcomeStatus.FAILED,
                }
                else NativeResponseState.UNKNOWN
            )
            self.inspections[handle.run_id] = NativeInspection(
                process_state=NativeProcessState.ALIVE,
                response_state=response_state,
                reason_code=None,
            )
        return NativeOutcome(
            response_id=response_id,
            status=status,
            reason_code=None,
            observed_model=None,
            usage=None,
        )

    def prompt(
        self, handle: NativeHandle, input: AssembledInput, response_id: UUID
    ) -> NativeOutcome:
        self.begin_prompt(handle, input, response_id)
        status = self.outcomes.get(handle.run_id, NativeOutcomeStatus.COMPLETED)
        return self.finish_prompt(handle, response_id, status)

    def read_events(
        self, handle: NativeHandle, after_cursor: int, limit: int = 100
    ) -> tuple[NativeEvent, ...]:
        del handle, after_cursor, limit
        return ()

    def cancel(self, handle: NativeHandle, response_id: UUID) -> NativeCancelOutcome:
        with self.response_lock:
            if self.active_responses.get(handle.run_id) != response_id:
                return NativeCancelOutcome(
                    response_id=response_id,
                    status=NativeCancelState.FAILED,
                    reason_code="response_not_current",
                )
        return NativeCancelOutcome(
            response_id=response_id,
            status=NativeCancelState.UNSUPPORTED,
            reason_code="cancel_not_supported",
        )

    def inspect(self, handle: NativeHandle) -> NativeInspection:
        return self.inspections[handle.run_id]

    def close(self, handle: NativeHandle) -> NativeCloseOutcome:
        self.inspections[handle.run_id] = NativeInspection(
            process_state=NativeProcessState.GONE,
            response_state=NativeResponseState.IDLE,
            reason_code=None,
        )
        return NativeCloseOutcome(
            status=NativeCloseState.CLOSED,
            reason_code=None,
        )

    def finish(self, run_id: UUID) -> None:
        self.inspections[run_id] = NativeInspection(
            process_state=NativeProcessState.ALIVE,
            response_state=NativeResponseState.IDLE,
            reason_code=None,
        )


@pytest.fixture
def exercise_background_parallel(monkeypatch):
    from threading import Event

    from sidera_agents.contracts import RunState, StartStatus

    def exercise(lab):
        requests = (
            lab.request(text="first background run"),
            lab.request(text="second background run"),
        )
        starts = tuple(lab.execution.start(request) for request in requests)
        assert all(start.status is StartStatus.ACCEPTED for start in starts)
        entered = {start.run_id: Event() for start in starts}
        release = {start.run_id: Event() for start in starts}

        def gated_prompt(handle, assembled, response_id):
            lab.native.begin_prompt(handle, assembled, response_id)
            entered[handle.run_id].set()
            if not release[handle.run_id].wait(timeout=4):
                raise RuntimeError("background response was not released")
            return lab.native.finish_prompt(
                handle, response_id, NativeOutcomeStatus.COMPLETED
            )

        monkeypatch.setattr(lab.native, "prompt", gated_prompt)
        try:
            lab.service.start_background(interval_seconds=0.001)
            for start in sorted(starts, key=lambda item: str(item.run_id)):
                assert entered[start.run_id].wait(timeout=1)
            assert len(lab.native.open_calls) == 2
            for request, start in zip(requests, starts, strict=True):
                assert (
                    lab.execution.get_run(
                        lab.workspace_id, start.run_id, grant_id=lab.grant_id
                    ).state
                    is RunState.RUNNING
                )
                assert lab.execution.start(request) == start
                assert (
                    lab.execution.get_start(
                        lab.workspace_id, request.operation_id, grant_id=lab.grant_id
                    ).receipt
                    == start
                )
            installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
            assert installation.active_count == 2
            assert installation.session_count == 2
            lab.service.poll_once()
        finally:
            for gate in release.values():
                gate.set()
            lab.service.close(timeout=3)

        for start in starts:
            snapshot = lab.execution.get_run(
                lab.workspace_id, start.run_id, grant_id=lab.grant_id
            )
            assert snapshot.state is RunState.COMPLETED
            events = lab.store.get_lifecycle_events(lab.workspace_id, start.run_id)
            assert [event.sequence for event in events] == list(
                range(1, snapshot.last_event_sequence + 1)
            )
        installation = lab.store.get_runtime_entry(lab.runtime.ref).installation
        assert installation.active_count == 0
        assert installation.session_count == 0
        assert len(lab.native.open_calls) == 2
        assert len(lab.native.prompt_calls) == 2
        return tuple(start.run_id for start in starts)

    return exercise


@pytest.fixture
def admission_lab_factory(tmp_path):
    services = []

    def build(
        store,
        *,
        runtime_max_active=1,
        runtime_max_sessions=2,
        profile_max_active=1,
    ):
        try:
            from sidera_agents.bootstrap import create_agent_service
        except ModuleNotFoundError as error:
            raise AssertionError(
                f"Agent service bootstrap is not implemented ({error.name})."
            ) from None
        from sidera_agents.catalog import ProfileCatalog
        from sidera_agents.harnesses.registry import HarnessRegistry
        from sidera_agents.ports import DenyContextSourceReader, DenySecretReader
        from sidera_agents.runtimes import RuntimeCatalog

        workspace_id = uuid4()
        grant_id = uuid4()
        admin_grant_id = uuid4()
        authorizer = RuntimeLabAuthorizer()
        authorizer.grant_workspace(workspace_id, grant_id)
        authorizer.grant_workspace(
            workspace_id, admin_grant_id, local_configuration=True
        )
        clock = lambda: datetime(2026, 10, 2, tzinfo=UTC)
        native = ControlledHarness()
        registry = HarnessRegistry((native,))
        catalog = ProfileCatalog(store, authorizer, clock)
        runtimes = RuntimeCatalog(store, authorizer, registry=registry, clock=clock)
        executable = tmp_path / f"controlled-{uuid4().hex}"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "for raw in sys.stdin:\n"
            "    request = json.loads(raw)\n"
            "    if request.get('method') == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':1,'agentCapabilities':{'mcpCapabilities':{}}}}) + '\\n')\n"
            "        sys.stdout.flush()\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        runtime = runtimes.register(
            workspace_id,
            native.harness_id,
            str(executable),
            max_active=runtime_max_active,
            max_sessions=runtime_max_sessions,
            grant_id=grant_id,
        )
        profile = catalog.create_profile(
            workspace_id,
            ProfileDraft(
                name="admission test",
                description="Synthetic admission profile",
                runtime_ref=runtime.ref,
                instructions="Follow the test request.",
                resource_refs=(),
                model_id=None,
                thinking=None,
                max_active=profile_max_active,
                environment={},
                secret_refs=(),
                native_settings={},
            ),
            grant_id=grant_id,
        )
        service = create_agent_service(
            store,
            registry,
            authorizer,
            DenyContextSourceReader(),
            DenySecretReader(),
            clock,
        )
        services.append(service)
        service.poll_once()

        def request(*, operation_id=None, policy="bounded", **changes):
            from sidera_agents.contracts import (
                ExecutionWorkspace,
                RunPolicy,
                RunStart,
            )

            values = {
                "contract_version": 1,
                "operation_id": operation_id or uuid4(),
                "profile_ref": profile.ref,
                "grant_id": grant_id,
                "policy": RunPolicy(policy),
                "instructions": (),
                "text": "Review the synthetic change.",
                "context": (),
                "tools": (),
                "workspace": ExecutionWorkspace(
                    directory=str(tmp_path),
                    writable=True,
                    artifact_directory=None,
                ),
                "required_capabilities": ("prompt",),
            }
            values.update(changes)
            return RunStart(**values)

        return SimpleNamespace(
            store=store,
            service=service,
            execution=service.execution,
            native=native,
            registry=registry,
            authorizer=authorizer,
            catalog=catalog,
            runtimes=runtimes,
            executable=executable,
            workspace_id=workspace_id,
            grant_id=grant_id,
            admin_grant_id=admin_grant_id,
            runtime=runtime,
            profile=profile,
            clock=clock,
            request=request,
            pump=service.poll_once,
        )

    yield build
    close_failed = False
    for service in services:
        try:
            service.close()
        except Exception:  # noqa: BLE001 - continue closing all services, then fail closed
            close_failed = True
    if close_failed:
        pytest.fail("An admission service did not close safely.", pytrace=False)


@pytest.fixture
def admission_lab(admission_lab_factory):
    from sidera_agents.memory import MemoryAgentStore

    return admission_lab_factory(MemoryAgentStore())
