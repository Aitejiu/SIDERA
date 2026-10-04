"""Single-process owner for native Gateway sessions and durable consumers."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sidera_control_plane.application.agent_worker_adapter import require_gateway_owner
from sidera_control_plane.config import Settings
from sidera_control_plane.gateway_worker import (
    GatewayRuntime,
    build_gateway,
)
from sidera_control_plane.handoff_worker import (
    HandoffWorkerRuntime,
    build_handoff_worker,
)
from sidera_control_plane.input_dispatcher_worker import (
    InputDispatcherRuntime,
    build_input_dispatcher,
)
from sidera_control_plane.persistence.database import Database


class GatewayOwnerAlreadyRunning(RuntimeError):
    """Another native Gateway owner already holds the database lock."""


class GatewayOwnerLock:
    """Hold one database-scoped advisory lock for the lifetime of the owner."""

    _KEY_A = 1_397_310_533
    _KEY_B = 1

    def __init__(
        self,
        database: Database,
        *,
        heartbeat_seconds: float = 1.0,
        acquire_timeout_seconds: float = 5.0,
    ) -> None:
        if heartbeat_seconds <= 0 or acquire_timeout_seconds <= 0:
            raise ValueError("Gateway owner lock timing must be positive")
        self._database = database
        self._heartbeat_seconds = heartbeat_seconds
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._acquired = False
        self._lost = False
        self._closed = False

    def acquire(self) -> bool:
        if self._closed:
            raise RuntimeError("Gateway owner lock is closed")
        if self._thread is not None:
            raise RuntimeError("Gateway owner lock acquire called twice")
        self._thread = threading.Thread(
            target=self._run,
            name="sidera-gateway-owner-lock",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(self._acquire_timeout_seconds):
            self._lost = True
            self._stop_event.set()
            self._thread.join(timeout=self._acquire_timeout_seconds)
            return False
        return self._acquired and not self._lost

    def check(self) -> bool:
        return (
            self._acquired
            and not self._lost
            and not self._closed
            and self._thread is not None
            and self._thread.is_alive()
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._heartbeat_seconds + 2.0)
            if self._thread.is_alive():
                self._lost = True
        self._database.dispose()

    def _run(self) -> None:
        connection: Any | None = None
        try:
            connection = self._database.engine.connect()
            locked = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key_a, :key_b)"),
                    {"key_a": self._KEY_A, "key_b": self._KEY_B},
                ).scalar_one()
            )
            connection.commit()
            if not locked:
                self._ready_event.set()
                return
            self._acquired = True
            self._ready_event.set()
            while not self._stop_event.wait(self._heartbeat_seconds):
                connection.execute(text("SELECT 1")).scalar_one()
                connection.commit()
            connection.execute(
                text("SELECT pg_advisory_unlock(:key_a, :key_b)"),
                {"key_a": self._KEY_A, "key_b": self._KEY_B},
            ).scalar_one()
            connection.commit()
        except SQLAlchemyError:
            self._lost = True
            self._ready_event.set()
        finally:
            if connection is not None:
                connection.close()


@dataclass(slots=True)
class GatewayOwner:
    """Own one GatewayRuntime and inject it into every native consumer."""

    gateway_runtime: GatewayRuntime
    input_runtime: InputDispatcherRuntime
    handoff_runtime: HandoffWorkerRuntime
    owner_lock: GatewayOwnerLock | None = None

    @property
    def gateway(self):
        return self.gateway_runtime.gateway

    def require_owner(self) -> None:
        if self.owner_lock is not None:
            require_gateway_owner(self.owner_lock.check)

    def close(self) -> None:
        try:
            self.gateway_runtime.close()
        finally:
            if self.owner_lock is not None:
                self.owner_lock.close()


def build_gateway_owner(settings: Settings, *, native_services=None) -> GatewayOwner:
    """Build all native consumers around exactly one GatewayRuntime."""

    lock_database = Database(settings.database_url)
    owner_lock = GatewayOwnerLock(lock_database)
    if not owner_lock.acquire():
        owner_lock.close()
        raise GatewayOwnerAlreadyRunning("gateway_owner_already_running")
    gateway_runtime: GatewayRuntime | None = None
    try:
        gateway_runtime = (
            build_gateway(settings, owner_check=owner_lock.check)
            if native_services is None
            else build_gateway(
                settings, native_services=native_services, owner_check=owner_lock.check
            )
        )
        input_runtime = build_input_dispatcher(
            settings, gateway_runtime=gateway_runtime
        )
        handoff_runtime = (
            build_handoff_worker(settings, gateway_runtime=gateway_runtime)
            if native_services is None
            else build_handoff_worker(
                settings,
                gateway_runtime=gateway_runtime,
                native_services=native_services,
            )
        )
    except BaseException:
        if gateway_runtime is not None:
            gateway_runtime.close()
        owner_lock.close()
        raise
    return GatewayOwner(
        gateway_runtime=gateway_runtime,
        input_runtime=input_runtime,
        handoff_runtime=handoff_runtime,
        owner_lock=owner_lock,
    )
