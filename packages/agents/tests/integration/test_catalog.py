from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from sidera_agents.catalog import ProfileCatalog
from sidera_agents.contracts import ProfileRevision
from sidera_agents.errors import AgentError


def _owned_scratch_engine():
    raw_url = os.environ.get("SIDERA_AGENTS_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("SIDERA_AGENTS_TEST_DATABASE_URL is unset.")

    try:
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError, SQLAlchemyError

        url = make_url(raw_url)
        engine = create_engine(url, pool_pre_ping=True, echo=False)
    except (ArgumentError, SQLAlchemyError):
        pytest.skip("The dedicated PostgreSQL test URL could not be parsed.")

    if url.get_backend_name() != "postgresql":
        pytest.skip("The dedicated test target is not PostgreSQL.")
    if (
        url.database is None
        or re.fullmatch(r"sidera_agents_test(?:_[a-z0-9_-]+)?", url.database) is None
    ):
        pytest.skip("The database name does not identify an agents scratch database.")
    if url.host not in {None, "localhost", "127.0.0.1", "::1"} and not (
        url.host and url.host.startswith("/")
    ):
        pytest.skip("The dedicated test database is not a local target.")

    safe = False
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text(
                    "SELECT current_database(), "
                    "pg_get_userbyid(datdba) = current_user "
                    "FROM pg_database WHERE datname = current_database()"
                )
            ).one()
            inspector = inspect(connection)
            schemas = set(inspector.get_schema_names())
            custom_schemas = schemas - {
                "information_schema",
                "pg_catalog",
                "public",
            }
            public_objects = (
                inspector.get_table_names(schema="public")
                + inspector.get_view_names(schema="public")
                + inspector.get_materialized_view_names(schema="public")
            )
            agents_tables = set(inspector.get_table_names(schema="agents"))
            agents_views = inspector.get_view_names(
                schema="agents"
            ) + inspector.get_materialized_view_names(schema="agents")
            owned_objects = {
                "alembic_version",
                "profile_heads",
                "profile_revisions",
                "resource_heads",
                "resource_revisions",
            }
            safe = (
                identity[0] == url.database
                and identity[1] is True
                and custom_schemas <= {"agents"}
                and not public_objects
                and agents_tables <= owned_objects
                and not agents_views
            )
    except SQLAlchemyError:
        safe = False
    if not safe:
        engine.dispose()
        pytest.skip("The database is not an owned, blank agents scratch target.")
    return engine


def _migrate(engine, command_name: str, revision: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    package_dir = Path(__file__).resolve().parents[2]
    config = Config(str(package_dir / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        if command_name == "upgrade":
            command.upgrade(config, revision or "head")
        else:
            command.downgrade(config, revision or "base")


def test_postgres_catalog_cas_persistence_and_owned_schema_migrations(
    catalog_lab,
) -> None:
    engine = _owned_scratch_engine()
    try:
        from sqlalchemy import inspect

        from sidera_agents.postgres.store import PostgresAgentStore

        _migrate(engine, "upgrade")
        expected_tables = {
            "alembic_version",
            "profile_heads",
            "profile_revisions",
            "resource_heads",
            "resource_revisions",
        }
        assert set(inspect(engine).get_table_names(schema="agents")) == expected_tables

        catalog = ProfileCatalog(
            PostgresAgentStore(engine),
            catalog_lab.authorizer,
            catalog_lab.clock,
        )
        resource = catalog.create_resource(
            catalog_lab.workspace_id,
            catalog_lab.skill_draft,
            grant_id=catalog_lab.grant_id,
        )
        draft = catalog_lab.draft.model_copy(update={"resource_refs": (resource.ref,)})
        profile = catalog.create_profile(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )
        updated_resource = catalog.revise_resource(
            resource.ref,
            resource.draft.model_copy(update={"name": "later revision"}),
            grant_id=catalog_lab.grant_id,
        )

        barrier = Barrier(3)

        def revise(instructions: str):
            barrier.wait()
            candidate = profile.draft.model_copy(update={"instructions": instructions})
            try:
                return catalog.revise_profile(
                    profile.ref, candidate, grant_id=catalog_lab.grant_id
                )
            except AgentError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_writer = pool.submit(revise, "Writer one")
            second_writer = pool.submit(revise, "Writer two")
            barrier.wait()
            outcomes = (first_writer.result(), second_writer.result())

        saved = tuple(item for item in outcomes if isinstance(item, ProfileRevision))
        conflicts = tuple(item for item in outcomes if item == "revision_conflict")
        assert len(saved) == len(conflicts) == 1

        reopened = ProfileCatalog(
            PostgresAgentStore(engine),
            catalog_lab.authorizer,
            catalog_lab.clock,
        )
        assert reopened.get_profile(
            profile.ref, grant_id=catalog_lab.grant_id
        ).draft.resource_refs == (resource.ref,)
        assert (
            reopened.get_resource(resource.ref, grant_id=catalog_lab.grant_id)
            == resource
        )
        assert (
            reopened.get_resource(updated_resource.ref, grant_id=catalog_lab.grant_id)
            == updated_resource
        )
        assert (
            reopened.list_profiles(
                catalog_lab.workspace_id, grant_id=catalog_lab.grant_id
            )[0].ref
            == saved[0].ref
        )

        _migrate(engine, "downgrade", "base")
        assert set(inspect(engine).get_table_names(schema="agents")) == {
            "alembic_version"
        }
        _migrate(engine, "upgrade")
        assert set(inspect(engine).get_table_names(schema="agents")) == expected_tables
    finally:
        engine.dispose()
