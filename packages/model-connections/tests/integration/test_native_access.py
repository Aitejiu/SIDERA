from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.native_access import (
    NativeCredentialAccessPermission,
    NativeCredentialAccessRequest,
    NativeCredentialReader,
)
from sidera_model_connections.postgres import PostgresConnectionCatalog
from tests.fakes import FakeSecretStore

SYNTHETIC_SECRET = "synthetic-native-access-secret"
THREAD_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class NativeAccessDatabase:
    session_factory: sessionmaker[Session] = field(repr=False)
    url: URL = field(repr=False)
    name: str


@pytest.fixture
def native_access_database(
    session_factory: sessionmaker[Session],
    database_target: tuple[URL, str],
) -> NativeAccessDatabase:
    url, name = database_target
    return NativeAccessDatabase(session_factory, url, name)


class ExactPermissionReader:
    def __init__(self, request: NativeCredentialAccessRequest) -> None:
        self.permission = NativeCredentialAccessPermission(request=request)

    def authorize(
        self, request: NativeCredentialAccessRequest
    ) -> NativeCredentialAccessPermission:
        if request != self.permission.request:
            raise ConfigurationError("runtime_binding_denied")
        return self.permission


class RecordingSecretStore(FakeSecretStore):
    def __init__(self, record: Callable[[str], None]) -> None:
        super().__init__()
        self._record = record

    def read(self, ref):
        self._record("source_read")
        return super().read(ref)


class ObservedCatalog:
    def __init__(
        self,
        catalog: PostgresConnectionCatalog,
        snapshot_ready: Event,
        resume_reader: Event,
    ) -> None:
        self._catalog = catalog
        self._snapshot_ready = snapshot_ready
        self._resume_reader = resume_reader

    def __getattr__(self, name: str):
        return getattr(self._catalog, name)

    def get_credential(self, ref):
        metadata = self._catalog.get_credential(ref)
        self._pause_reader()
        return metadata

    @contextmanager
    def credential_read_lease(self, ref):
        with self._catalog.credential_read_lease(ref) as metadata:
            self._pause_reader()
            yield metadata

    def _pause_reader(self) -> None:
        self._snapshot_ready.set()
        if not self._resume_reader.wait(THREAD_TIMEOUT_SECONDS):
            raise TimeoutError("test did not release the paused credential reader")


def test_revocation_waits_for_active_metadata_read_lease(
    native_access_database: NativeAccessDatabase,
    workspace_id: UUID,
) -> None:
    markers: list[str] = []
    markers_lock = Lock()

    def record(marker: str) -> None:
        with markers_lock:
            markers.append(marker)

    secret_store = RecordingSecretStore(record)
    catalog = PostgresConnectionCatalog(native_access_database.session_factory)
    managed = CredentialService(catalog, secret_store).save_managed(
        workspace_id,
        uuid4(),
        "synthetic native access credential",
        SecretStr(SYNTHETIC_SECRET),
    )
    secret_store.calls.clear()
    ref = managed.credential.ref
    request = NativeCredentialAccessRequest(
        workspace_id=workspace_id,
        credential_ref=ref,
        scope_id=uuid4(),
        destination="SERVICE_API_KEY",
    )

    snapshot_ready = Event()
    resume_reader = Event()
    reader = NativeCredentialReader(
        ObservedCatalog(catalog, snapshot_ready, resume_reader),
        ExactPermissionReader(request),
        secret_store,
        {},
    )

    assert native_access_database.name == "sidera_cfg2_test_native_access"
    application_name = f"sidera-r02-revoke-{uuid4().hex}"
    revoke_engine = create_engine(
        native_access_database.url,
        connect_args={"application_name": application_name},
        pool_pre_ping=True,
    )
    monitor_engine = create_engine(native_access_database.url, pool_pre_ping=True)
    revoke_catalog = PostgresConnectionCatalog(
        sessionmaker(revoke_engine, expire_on_commit=False)
    )
    revoke_service = CredentialService(revoke_catalog, secret_store)

    reader_results: list[SecretStr] = []
    reader_errors: list[Exception] = []
    revoke_errors: list[Exception] = []
    revoke_done = Event()

    def read_credential() -> None:
        try:
            reader_results.append(reader.read(request))
        except Exception as error:  # noqa: BLE001
            reader_errors.append(error)

    def revoke_credential() -> None:
        try:
            revoke_service.revoke(ref, expected_version=managed.credential.version)
            record("revoke_complete")
        except Exception as error:  # noqa: BLE001
            revoke_errors.append(error)
        finally:
            revoke_done.set()

    reader_thread = Thread(target=read_credential, daemon=True)
    revoke_thread = Thread(target=revoke_credential, daemon=True)
    reader_thread.start()
    revoke_started = False
    lock_wait_observed = False
    try:
        assert snapshot_ready.wait(THREAD_TIMEOUT_SECONDS), (
            "reader did not reach its active metadata snapshot"
        )
        revoke_thread.start()
        revoke_started = True

        deadline = monotonic() + THREAD_TIMEOUT_SECONDS
        while monotonic() < deadline:
            if revoke_done.is_set():
                pytest.fail("revocation committed during the active metadata snapshot")
            with monitor_engine.connect() as connection:
                lock_wait_observed = (
                    connection.execute(
                        text(
                            "SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name = :application_name "
                            "AND wait_event_type = 'Lock' LIMIT 1"
                        ),
                        {"application_name": application_name},
                    ).scalar_one_or_none()
                    is not None
                )
            if lock_wait_observed:
                break
            sleep(0.01)
        assert lock_wait_observed, "revocation did not wait on a PostgreSQL row lock"
    finally:
        resume_reader.set()
        reader_thread.join(THREAD_TIMEOUT_SECONDS)
        if revoke_started:
            revoke_thread.join(THREAD_TIMEOUT_SECONDS)
        monitor_engine.dispose()
        revoke_engine.dispose()

    assert not reader_thread.is_alive(), "credential reader thread did not finish"
    assert not revoke_thread.is_alive(), "credential revoker thread did not finish"
    assert reader_errors == []
    assert revoke_errors == []
    assert len(reader_results) == 1 and isinstance(reader_results[0], SecretStr)
    assert lock_wait_observed
    assert catalog.get_credential(ref).status == "revoked"
    assert markers.index("source_read") < markers.index("revoke_complete")
    assert [action for action, _ref in secret_store.calls] == [
        "read",
        "delete_owned",
    ]
