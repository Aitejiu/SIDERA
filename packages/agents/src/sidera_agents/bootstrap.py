from __future__ import annotations

from collections.abc import Iterable
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from .catalog import AgentStore, ProfileCatalog
from .errors import AgentError
from .observations import RunObservations
from .ports import (
    ContextSourceReader,
    ExecutionAuthorizer,
    HarnessAdapter,
    SecretReader,
    ToolConnectionReader,
)
from .runs import RunExecution
from .runtimes import RuntimeCatalog

if TYPE_CHECKING:
    from .harnesses.registry import HarnessRegistry


class AgentService:
    def __init__(
        self,
        store: Any,
        registry: Any,
        authorizer: ExecutionAuthorizer,
        sources: ContextSourceReader,
        secrets: SecretReader,
        clock,
        tool_connections: ToolConnectionReader | None,
    ) -> None:
        self._store = store
        self._owner_id: UUID = uuid4()
        self._owner_claimed = False
        self._lifecycle_lock = Lock()
        self._poll_lock = Lock()
        self._closing = False
        self._stop = Event()
        self._thread: Thread | None = None
        self.catalog = ProfileCatalog(store, authorizer, clock)
        self.runtimes = RuntimeCatalog(
            store, authorizer, registry=registry, clock=clock
        )
        self.execution = RunExecution(
            store,
            registry,
            authorizer,
            sources,
            secrets,
            self.runtimes,
            self._owner_id,
            tool_connections,
        )
        self.observations = RunObservations(self.execution, store)

    def migrate(self) -> None:
        migrate = getattr(self._store, "migrate", None)
        if migrate is not None:
            migrate()

    def readiness(self, *, require_execution_owner: bool = False) -> bool:
        """Point-in-time storage/optional owner evidence, not an execution grant."""
        readiness = getattr(self._store, "readiness", None)
        try:
            ready = True if readiness is None else bool(readiness())
        except Exception:
            if not require_execution_owner:
                raise
            return False
        if not ready or not require_execution_owner:
            return ready
        with self._lifecycle_lock:
            if (
                self._closing
                or not self._owner_claimed
                or (self._thread is not None and not self._thread.is_alive())
            ):
                return False
            try:
                return self._store.execution_owner_is_held(self._owner_id) is True
            except Exception:  # noqa: BLE001 - owner evidence fails closed without driver details
                return False

    def poll_once(self) -> int:
        return self._poll_once(background=False)

    def _poll_once(self, *, background: bool) -> int:
        with self._poll_lock:
            with self._lifecycle_lock:
                if self._closing:
                    raise AgentError(
                        "shutdown_pending",
                        "The execution service is closing.",
                    )
                if not self._owner_claimed:
                    if not self._store.acquire_execution_owner(self._owner_id):
                        raise AgentError(
                            "execution_owner_unavailable",
                            "The execution owner lock is unavailable.",
                        )
                    self._owner_claimed = True
                elif not self._store.execution_owner_is_held(self._owner_id):
                    raise AgentError(
                        "execution_owner_unavailable",
                        "The execution owner lock is not held.",
                    )
            return self.execution.poll_once(_background=background)

    def start_background(self, *, interval_seconds: float = 0.25) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        with self._lifecycle_lock:
            if self._closing:
                raise AgentError(
                    "shutdown_pending", "The execution service is closing."
                )
            if self._thread is not None and self._thread.is_alive():
                return
            if self._owner_claimed:
                if not self._store.execution_owner_is_held(self._owner_id):
                    raise AgentError(
                        "execution_owner_unavailable",
                        "The execution owner lock is not held.",
                    )
            elif not self._store.acquire_execution_owner(self._owner_id):
                raise AgentError(
                    "execution_owner_unavailable",
                    "The execution owner lock is unavailable.",
                )
            self._owner_claimed = True
            self._stop.clear()
            self._thread = Thread(
                target=self._background_loop,
                args=(interval_seconds,),
                name="sidera-agent-execution",
                daemon=True,
            )
            self._thread.start()

    def _background_loop(self, interval_seconds: float) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once(background=True)
            except AgentError as error:
                if error.code in {"execution_owner_unavailable", "shutdown_pending"}:
                    return
            self._stop.wait(interval_seconds)

    def close(self, *, timeout: float = 5.0) -> None:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = monotonic() + timeout
        with self._lifecycle_lock:
            self._closing = True
            self._stop.set()
            self.execution.begin_close()
            thread = self._thread
        if thread is current_thread():
            raise AgentError(
                "shutdown_pending",
                "The execution worker cannot release its own owner lock.",
            )
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - monotonic()))
            if thread.is_alive():
                raise AgentError(
                    "shutdown_pending",
                    "The execution worker has not stopped; owner lock retained.",
                )
        if not self._poll_lock.acquire(timeout=max(0.0, deadline - monotonic())):
            raise AgentError(
                "shutdown_pending",
                "A service poll has not stopped; owner lock retained.",
            )
        try:
            if not self.execution.wait_until(deadline):
                raise AgentError(
                    "shutdown_pending",
                    "Native work or admission has not stopped; owner lock retained.",
                )
            with self._lifecycle_lock:
                if self._owner_claimed:
                    try:
                        self._store.release_execution_owner(self._owner_id)
                    except Exception as error:
                        raise AgentError(
                            "shutdown_pending",
                            "The execution owner lock release is unconfirmed.",
                        ) from error
                    self._owner_claimed = False
        finally:
            self._poll_lock.release()


def create_agent_service(
    store: Any,
    registry: Any,
    authorizer: ExecutionAuthorizer,
    sources: ContextSourceReader,
    secrets: SecretReader,
    clock,
    tool_connections: ToolConnectionReader | None = None,
) -> AgentService:
    return AgentService(
        store,
        registry,
        authorizer,
        sources,
        secrets,
        clock,
        tool_connections,
    )


def open_memory_store() -> AgentStore:
    """Create an explicit ephemeral store; never a persistence fallback."""
    from .memory import MemoryAgentStore

    return MemoryAgentStore()


def create_harness_registry(
    harnesses: Iterable[HarnessAdapter] | None = None,
) -> HarnessRegistry:
    """Select exactly the supplied trusted adapters, or the lazy built-in default."""
    from .harnesses.registry import HarnessRegistry, default_harness_registry

    if harnesses is None:
        return default_harness_registry()
    adapters = tuple(harnesses)
    identities: set[str] = set()
    for adapter in adapters:
        identity = getattr(adapter, "harness_id", None)
        executable = getattr(adapter, "executable_name", None)
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or identity in identities
            or not isinstance(executable, str)
            or not executable.strip()
        ):
            raise ValueError("Harness identities must be unique and non-empty strings.")
        if any(
            not callable(getattr(adapter, method, None))
            for method in (
                "describe",
                "probe",
                "validate",
                "open",
                "prompt",
                "read_events",
                "cancel",
                "inspect",
                "close",
            )
        ):
            raise ValueError(
                "A trusted harness must implement the full native contract."
            )
        identities.add(identity)
    return HarnessRegistry(adapters)


def open_postgres_store(database_url: str):
    from sqlalchemy import create_engine

    from .postgres.store import PostgresAgentStore

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        echo=False,
        hide_parameters=True,
    )
    return PostgresAgentStore(engine, owns_engine=True)
