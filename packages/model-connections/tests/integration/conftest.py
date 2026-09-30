from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from sidera_model_connections.contracts import (
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    ModelConnectionRevision,
)
from sidera_model_connections.postgres import PostgresConnectionCatalog

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _owned_database() -> tuple[URL, str]:
    raw_url = os.environ.get("SIDERA_CONFIGURATION_TEST_DATABASE_URL")
    expected_name = os.environ.get("SIDERA_CFG2_OWNED_DATABASE_NAME")
    if not raw_url or not expected_name:
        raise pytest.UsageError("owned PostgreSQL test database variables are required")
    try:
        url = make_url(raw_url)
        address = ipaddress.ip_address(url.host or "")
    except ValueError:
        raise pytest.UsageError("test database URL must use a loopback IP") from None
    if (
        url.drivername != "postgresql+psycopg"
        or not address.is_loopback
        or not expected_name.startswith("sidera_cfg2_test_")
        or url.database != expected_name
    ):
        raise pytest.UsageError("test database does not match the explicitly owned target")
    return url, expected_name


def pytest_configure(config: pytest.Config) -> None:
    _owned_database()


def _alembic_config() -> Config:
    return Config(str(PACKAGE_ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def database_target() -> tuple[URL, str]:
    return _owned_database()


@pytest.fixture(scope="session")
def engine(database_target: tuple[URL, str]) -> Iterator:
    url, expected_name = database_target
    value = create_engine(url, pool_pre_ping=True)
    with value.connect() as connection:
        assert connection.exec_driver_sql("SELECT current_database()").scalar_one() == expected_name
    yield value
    value.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_schema(database_target: tuple[URL, str]) -> None:
    command.upgrade(_alembic_config(), "head")


@pytest.fixture(scope="session")
def session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def catalog(session_factory: sessionmaker[Session], credential_metadata: CredentialMetadata):
    value = PostgresConnectionCatalog(session_factory)
    value.create_env_credential(credential_metadata)
    return value


@pytest.fixture
def workspace_id() -> UUID:
    return uuid4()


@pytest.fixture
def credential_metadata(workspace_id: UUID) -> CredentialMetadata:
    return CredentialMetadata(
        ref=CredentialRef(workspace_id, uuid4()),
        alias="integration env credential",
        source="env",
        status="active",
        env_name="SIDERA_INTEGRATION_KEY",
        replaces_ref=None,
        created_at=datetime.now(UTC),
        version=1,
    )


@pytest.fixture
def connection_v1(credential_metadata: CredentialMetadata) -> ModelConnectionRevision:
    return ModelConnectionRevision(
        ref=ConnectionRef(credential_metadata.ref.workspace_id, uuid4(), 1),
        name="integration connection v1",
        provider_id="test-provider",
        protocol="openai_compatible",
        base_url="https://api.example.test/v1",
        network_scope="public",
        allowed_roles=frozenset({"worker"}),
        credential_ref=credential_metadata.ref,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def connection_v2(connection_v1: ModelConnectionRevision) -> ModelConnectionRevision:
    return ModelConnectionRevision(
        ref=ConnectionRef(
            connection_v1.ref.workspace_id,
            connection_v1.ref.connection_id,
            2,
        ),
        name="integration connection v2",
        provider_id=connection_v1.provider_id,
        protocol=connection_v1.protocol,
        base_url=connection_v1.base_url,
        network_scope=connection_v1.network_scope,
        allowed_roles=connection_v1.allowed_roles,
        credential_ref=connection_v1.credential_ref,
        created_at=datetime.now(UTC),
    )
