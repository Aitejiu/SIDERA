from __future__ import annotations

import math
from importlib.resources import files
from pathlib import Path
from threading import Event, RLock
from time import monotonic
from typing import TYPE_CHECKING, Protocol

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import AnyHttpUrl
from sidera_agents.api import AgentExecution
from sidera_agents.bootstrap import AgentService
from sidera_agents.contracts import JsonValue
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneIdentity,
    ControlPlaneRuntime,
)
from sidera_control_plane_client import (
    ActorRef,
    ControlPlaneClient,
    ControlPlaneClientConfig,
)
from sidera_model_connections.native_access import NativeCredentialReader
from sidera_model_connections.ports import SecretStore
from sidera_model_connections.postgres import PostgresConnectionCatalog
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .auth.grants import ProductGrants, ToolConnectionResolver
from .auth.local_session import LocalSessions
from .config import ProductSettings
from .postgres import PostgresProductStore
from .postgres.authority_store import PostgresAuthorityStore

if TYPE_CHECKING:
    from .agents.configuration import ConfigurationService
    from .chat.service import ChatService
    from .composition import (
        AgentSecretReader,
        AuthorityRouter,
        ProductRequestAuthorizer,
        SourceRouter,
    )
    from .defaults import DefaultsService


class OwnedAgentStore(Protocol):
    def close(self) -> None: ...


def credential_alembic_config_path() -> Path:
    path = Path(str(files("sidera_model_connections"))) / "alembic.ini"
    if not path.is_file():
        raise RuntimeError("credential_migration_resources_unavailable")
    return path


def credential_schema_ready(engine: Engine) -> bool:
    config = Config(str(credential_alembic_config_path()))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    required = {
        "credentials",
        "connections",
        "connection_revisions",
        "credential_writes",
        "credential_events",
    }
    try:
        with engine.connect() as connection:
            if not required.issubset(
                inspect(connection).get_table_names(schema="sidera_configuration")
            ):
                return False
            actual = set(
                connection.execute(
                    text("SELECT version_num FROM sidera_configuration.alembic_version")
                ).scalars()
            )
            return actual == expected
    except SQLAlchemyError:
        return False


class ProductRuntime:
    def __init__(
        self,
        *,
        settings: ProductSettings,
        product_store: PostgresProductStore,
        authority_store: PostgresAuthorityStore,
        local_sessions: LocalSessions,
        grants: ProductGrants,
        chat: ChatService,
        configuration: ConfigurationService,
        defaults: DefaultsService,
        task_tool_connections: ToolConnectionResolver,
        agent_store: OwnedAgentStore,
        credential_engine: Engine,
        credential_catalog: PostgresConnectionCatalog,
        secret_store: SecretStore,
        credential_reader: NativeCredentialReader,
        secret_reader: AgentSecretReader,
        agents: AgentService,
        control_plane: ControlPlaneRuntime,
        authority: AuthorityRouter,
        sources: SourceRouter,
        request_authorizer: ProductRequestAuthorizer,
        background_identity: ControlPlaneIdentity,
    ) -> None:
        self.settings = settings
        self.product_store = product_store
        self.authority_store = authority_store
        self.local_sessions = local_sessions
        self.grants = grants
        self.chat = chat
        self.configuration = configuration
        self.defaults = defaults
        self.task_tool_connections = task_tool_connections
        self.agent_store = agent_store
        self.credential_engine = credential_engine
        self.credential_catalog = credential_catalog
        self.secret_store = secret_store
        self.credential_reader = credential_reader
        self.secret_reader = secret_reader
        self.agents = agents
        self.execution: AgentExecution = agents.execution
        self.control_plane = control_plane
        self.authority = authority
        self.sources = sources
        self.request_authorizer = request_authorizer
        self.background_identity = background_identity
        self._lock = RLock()
        self._shutdown_requested = Event()
        self._state = "constructed"
        self._admission = False
        self._execution_available = False
        self._availability_generation = 0
        self._cp_closed = False
        self._chat_closed = False
        self._agents_closed = False
        self._agent_store_closed = False
        self._credential_store_closed = False
        self._product_store_closed = False
        self._cleanup_code: str | None = None

    def inspect_readiness(self) -> dict[str, bool]:
        if self._shutdown_requested.is_set() or self._state in {
            "closing",
            "cleanup_incomplete",
            "closed",
        }:
            return {
                name: False
                for name in ("product", "agents", "control_plane", "credentials")
            }
        try:
            return {
                "product": self.product_store.readiness(),
                "agents": self.agents.readiness(
                    require_execution_owner=self._state == "ready"
                ),
                "control_plane": self.control_plane.readiness(),
                "credentials": credential_schema_ready(self.credential_engine),
            }
        except Exception:  # noqa: BLE001 - readiness never exposes driver or credential details
            return {
                name: False
                for name in ("product", "agents", "control_plane", "credentials")
            }

    def start(self) -> None:
        with self._lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("product_closed")
            if self._state == "ready":
                return
            if self._state != "constructed":
                raise RuntimeError("product_closed")
            if (
                self.authority.control_plane is None
                or self.sources.control_plane is None
            ):
                raise RuntimeError("product_readers_unavailable")
            try:
                if not all(self.inspect_readiness().values()):
                    raise RuntimeError("schema_not_ready")
                self._state = "starting"
                if self._shutdown_requested.is_set():
                    raise RuntimeError("product_closed")
                self.chat.activate()
                self.agents.start_background(
                    interval_seconds=self.settings.agent_poll_interval_seconds
                )
                if self._shutdown_requested.is_set():
                    raise RuntimeError("product_closed")
                if not self.agents.readiness(require_execution_owner=True):
                    raise RuntimeError("execution_owner_unavailable")
                self.control_plane.start_background(
                    background_identity=self.background_identity
                )
                if self._shutdown_requested.is_set():
                    raise RuntimeError("product_closed")
                if not self.control_plane.readiness():
                    raise RuntimeError("control_plane_unavailable")
                runtimes = self.agents.runtimes.list_runtimes(
                    self.settings.workspace_id,
                    grant_id=self.background_identity.grant_id,
                )
                if self._shutdown_requested.is_set():
                    raise RuntimeError("product_closed")
                self._execution_available = any(
                    runtime.installed and runtime.enabled for runtime in runtimes
                )
                self._admission = True
                self._state = "ready"
            except Exception:  # noqa: BLE001 - partial startup is rolled back through public owners
                self._admission = False
                self._state = "closing"
                self._close_locked(monotonic() + self.settings.close_grace_seconds)
                raise RuntimeError("product_startup_failed") from None

    def close(self, *, grace_seconds: float | None = None) -> bool:
        grace = (
            self.settings.close_grace_seconds
            if grace_seconds is None
            else grace_seconds
        )
        if not math.isfinite(grace) or not 0 <= grace <= 300:
            raise ValueError("A finite bounded close grace is required")
        deadline = monotonic() + grace
        self._shutdown_requested.set()
        self._admission = False
        self.chat.begin_close()
        if not self._lock.acquire(timeout=max(0.0, deadline - monotonic())):
            return False
        try:
            return self._close_locked(deadline)
        finally:
            self._lock.release()

    def _close_locked(self, deadline: float) -> bool:
        self._shutdown_requested.set()
        self.chat.begin_close()
        if self._state == "closed":
            return True
        self._state = "closing"
        self._execution_available = False
        try:
            if not self._cp_closed:
                if (
                    self.control_plane.close(
                        grace_seconds=max(0.0, deadline - monotonic())
                    )
                    is not True
                ):
                    self._state = "cleanup_incomplete"
                    self._cleanup_code = "control_plane_cleanup_pending"
                    return False
                self._cp_closed = True
            if not self._chat_closed:
                if (
                    self.chat.close(grace_seconds=max(0.0, deadline - monotonic()))
                    is not True
                ):
                    self._state = "cleanup_incomplete"
                    self._cleanup_code = "conversation_cleanup_pending"
                    return False
                self._chat_closed = True
            if not self._agents_closed:
                self.agents.close(timeout=max(0.0, deadline - monotonic()))
                self._agents_closed = True
            if not self._agent_store_closed:
                self.agent_store.close()
                self._agent_store_closed = True
            if not self._credential_store_closed:
                self.credential_engine.dispose()
                self._credential_store_closed = True
            if not self._product_store_closed:
                self.grants.close()
                self.product_store.close()
                self._product_store_closed = True
        except Exception:  # noqa: BLE001 - uncertain close retains remaining owners and supports retry
            self._state = "cleanup_incomplete"
            self._cleanup_code = "product_cleanup_pending"
            return False
        self._cleanup_code = None
        self._state = "closed"
        return True

    def require_write_admission(self) -> None:
        if self._shutdown_requested.is_set() or not self._admission:
            raise AgentError(
                "product_unavailable", "Product write admission is unavailable."
            )

    def refresh_execution_availability(self) -> bool:
        with self._lock:
            if (
                self._shutdown_requested.is_set()
                or not self._admission
                or self._state != "ready"
            ):
                return False
            self._availability_generation += 1
            observation = self._availability_generation
        try:
            runtimes = self.agents.runtimes.list_runtimes(
                self.settings.workspace_id,
                grant_id=self.background_identity.grant_id,
            )
            available = any(
                runtime.installed and runtime.enabled for runtime in runtimes
            )
        except Exception:  # noqa: BLE001 - metadata failure is unavailable, never optimistic admission
            available = False
        with self._lock:
            if (
                self._shutdown_requested.is_set()
                or not self._admission
                or self._state != "ready"
            ):
                return False
            if observation == self._availability_generation:
                self._execution_available = available
            return self._execution_available

    def status(self) -> dict[str, JsonValue]:
        execution_ready = (
            not self._shutdown_requested.is_set()
            and self._admission
            and self._state == "ready"
            and self._execution_available
        )
        return {
            "lifecycle": self._state,
            "cleanup": {
                "complete": self._state == "closed",
                "code": self._cleanup_code,
            },
            "execution": {"ready": execution_ready},
            "knowledge": {
                "available": False,
                "configured": self.settings.knowledge_base_url is not None,
            },
            "main_agent": {
                "available": execution_ready,
                "code": None if execution_ready else "product_unavailable",
            },
            "tools": {"available": False, "code": "local_session_required"},
            "workspace_id": str(self.settings.workspace_id),
            "setup": {"configured": None, "code": "local_session_required"},
        }

    def new_control_plane_client(self) -> ControlPlaneClient:
        return ControlPlaneClient(
            ControlPlaneClientConfig(
                control_plane_url=AnyHttpUrl(self.settings.local_url),
                workspace_id=self.settings.workspace_id,
                actor=ActorRef(type="user", id=self.settings.local_principal_id),
            ),
            transport=httpx.ASGITransport(app=self.control_plane.app),
        )
