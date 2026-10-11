from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from sidera_agents.catalog import ProfileCatalog
from sidera_agents.contracts import ProfileRevision
from sidera_agents.errors import AgentError


def _public_catalog_service(catalog_lab, engine):
    from sidera_agents.bootstrap import create_agent_service
    from sidera_agents.harnesses.registry import HarnessRegistry
    from sidera_agents.ports import DenyContextSourceReader, DenySecretReader
    from sidera_agents.postgres.store import PostgresAgentStore

    store = PostgresAgentStore(engine)
    service = create_agent_service(
        store,
        HarnessRegistry(()),
        catalog_lab.authorizer,
        DenyContextSourceReader(),
        DenySecretReader(),
        catalog_lab.clock,
    )
    return service


@pytest.mark.parametrize("kind", ["profile", "resource"])
def test_postgres_public_status_survives_reopen_and_reads_current_identity(
    catalog_lab, owned_agents_pg, kind
):
    service = _public_catalog_service(catalog_lab, owned_agents_pg.engine)
    service.migrate()
    try:
        catalog = service.catalog
        draft = catalog_lab.draft if kind == "profile" else catalog_lab.skill_draft
        first = getattr(catalog, f"create_{kind}")(
            catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
        )
        status = getattr(catalog, f"get_{kind}_status")(
            first.ref, grant_id=catalog_lab.grant_id
        )
        assert (status.enabled, status.version) == (True, 1)
        getattr(catalog, f"set_{kind}_enabled")(
            catalog_lab.workspace_id,
            getattr(first.ref, f"{kind}_id"),
            False,
            1,
            grant_id=catalog_lab.grant_id,
        )
        second = getattr(catalog, f"revise_{kind}")(
            first.ref,
            draft.model_copy(update={"name": "after disable"}),
            grant_id=catalog_lab.grant_id,
        )
        reopened = _public_catalog_service(catalog_lab, owned_agents_pg.engine)
        try:
            for ref in (first.ref, second.ref, first.ref):
                current = getattr(reopened.catalog, f"get_{kind}_status")(
                    ref, grant_id=catalog_lab.grant_id
                )
                assert (current.enabled, current.version) == (False, 2)
                assert getattr(current, f"{kind}_id") == getattr(
                    first.ref, f"{kind}_id"
                )
            assert (
                getattr(reopened.catalog, f"get_{kind}")(
                    first.ref, grant_id=catalog_lab.grant_id
                )
                == first
            )
            missing_ref = first.ref.model_copy(update={"revision": 99})
            with pytest.raises(AgentError) as missing:
                getattr(reopened.catalog, f"get_{kind}_status")(
                    missing_ref, grant_id=catalog_lab.grant_id
                )
            assert missing.value.code == "not_found"
            assert reopened.readiness() is True
            assert reopened.readiness(require_execution_owner=True) is False
        finally:
            reopened.close()
    finally:
        service.close()


def test_postgres_public_readiness_requires_schema_head_without_mutating_empty_store(
    catalog_lab, owned_agents_pg
):
    from sqlalchemy import event, inspect

    engine = owned_agents_pg.engine
    service = _public_catalog_service(catalog_lab, engine)
    before = inspect(engine).get_table_names(schema="agents")
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert service.readiness() is False
        assert service.readiness() is False
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements and set(statements) == {"SELECT"}
    assert inspect(engine).get_table_names(schema="agents") == before
    try:
        service.migrate()
        assert service.readiness() is True
        owned_agents_pg.migrate("base")
        owned_agents_pg.migrate("0003_agent_runs_operations")
        assert service.readiness() is False
        service.migrate()
        assert service.readiness() is True
        owned_agents_pg.migrate("base")
        assert service.readiness() is False
        service.migrate()
        assert service.readiness() is True
    finally:
        service.close()


def test_postgres_public_readiness_rejects_missing_required_table_at_current_head(
    catalog_lab, owned_agents_pg
):
    from sidera_agents.postgres.models import RunEventRow

    engine = owned_agents_pg.engine
    service = _public_catalog_service(catalog_lab, engine)
    service.migrate()
    try:
        assert service.readiness() is True
        table = RunEventRow.__table__
        table.drop(engine)
        try:
            assert service.readiness() is False
        finally:
            table.create(engine)
        assert service.readiness() is True
    finally:
        service.close()


def test_postgres_public_readiness_uses_packaged_migration_resources(
    catalog_lab, owned_agents_pg, tmp_path, monkeypatch
):
    import shutil
    from pathlib import Path

    from sidera_agents.postgres import store

    source = Path(__file__).resolve().parents[2]
    package = tmp_path / "installed" / "sidera_agents"
    package.mkdir(parents=True)
    shutil.copyfile(source / "alembic.ini", package / "alembic.ini")
    shutil.copytree(source / "migrations", package / "migrations")
    monkeypatch.setattr(store, "files", lambda name: package)
    service = _public_catalog_service(catalog_lab, owned_agents_pg.engine)
    try:
        assert service.readiness() is False
        service.migrate()
        assert service.readiness() is True
        (package / "alembic.ini").write_text(
            "[alembic]\nscript_location = %(here)s/empty_migrations\n",
        )
        (package / "empty_migrations" / "versions").mkdir(parents=True)
        assert service.readiness() is False
    finally:
        service.close()


def test_postgres_public_readiness_preserves_run_rows_events_and_counters(
    owned_agents_pg, admission_lab_factory
):
    from sqlalchemy import event

    from sidera_agents.postgres.store import PostgresAgentStore

    owned_agents_pg.migrate()
    store = PostgresAgentStore(owned_agents_pg.engine)
    lab = admission_lab_factory(store)
    receipt = lab.execution.start(lab.request())
    lab.pump()
    snapshot = store.get_run_snapshot(lab.workspace_id, receipt.run_id)
    handle = store.get_native_handle(lab.workspace_id, receipt.run_id)
    events = store.get_lifecycle_events(lab.workspace_id, receipt.run_id)
    installation = store.get_runtime_entry(lab.runtime.ref).installation
    native_counts = (len(lab.native.open_calls), len(lab.native.prompt_calls))
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(owned_agents_pg.engine, "before_cursor_execute", capture)
    try:
        for _ in range(3):
            assert lab.service.readiness() is True
            assert lab.service.readiness(require_execution_owner=True) is True
    finally:
        event.remove(owned_agents_pg.engine, "before_cursor_execute", capture)
    assert statements and set(statements) == {"SELECT"}
    assert store.get_run_snapshot(lab.workspace_id, receipt.run_id) == snapshot
    assert store.get_native_handle(lab.workspace_id, receipt.run_id) == handle
    assert store.get_lifecycle_events(lab.workspace_id, receipt.run_id) == events
    assert store.get_runtime_entry(lab.runtime.ref).installation == installation
    assert (
        (len(lab.native.open_calls), len(lab.native.prompt_calls))
        == native_counts
        == (1, 1)
    )


def test_postgres_catalog_cas_persistence_and_owned_schema_migrations(
    catalog_lab, owned_agents_pg
) -> None:
    engine = owned_agents_pg.engine
    from sqlalchemy import inspect

    from sidera_agents.postgres.store import PostgresAgentStore

    owned_agents_pg.migrate()
    expected_tables = {
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
        reopened.get_resource(resource.ref, grant_id=catalog_lab.grant_id) == resource
    )
    assert (
        reopened.get_resource(updated_resource.ref, grant_id=catalog_lab.grant_id)
        == updated_resource
    )
    assert (
        reopened.list_profiles(catalog_lab.workspace_id, grant_id=catalog_lab.grant_id)[
            0
        ].ref
        == saved[0].ref
    )

    owned_agents_pg.migrate("base")
    assert set(inspect(engine).get_table_names(schema="agents")) == {"alembic_version"}
    owned_agents_pg.migrate()
    assert set(inspect(engine).get_table_names(schema="agents")) == expected_tables


def test_postgres_profiles_with_reversed_refs_preserve_order_without_deadlock(
    catalog_lab, owned_agents_pg
) -> None:
    from sqlalchemy import create_engine

    from sidera_agents.postgres.store import PostgresAgentStore

    owned_agents_pg.migrate()
    concurrent_engine = create_engine(
        owned_agents_pg.url,
        echo=False,
        hide_parameters=True,
        connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=10000"},
    )
    try:
        catalog = ProfileCatalog(
            PostgresAgentStore(concurrent_engine),
            catalog_lab.authorizer,
            catalog_lab.clock,
        )
        first = catalog.create_resource(
            catalog_lab.workspace_id,
            catalog_lab.skill_draft,
            grant_id=catalog_lab.grant_id,
        )
        second = catalog.create_resource(
            catalog_lab.workspace_id,
            catalog_lab.skill_draft.model_copy(update={"name": "review-second"}),
            grant_id=catalog_lab.grant_id,
        )
        forward_refs = (first.ref, second.ref)
        reverse_refs = tuple(reversed(forward_refs))
        barrier = Barrier(3)

        def create_profile(resource_refs):
            barrier.wait(timeout=5.0)
            draft = catalog_lab.draft.model_copy(
                update={"resource_refs": resource_refs}
            )
            return catalog.create_profile(
                catalog_lab.workspace_id, draft, grant_id=catalog_lab.grant_id
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            forward = pool.submit(create_profile, forward_refs)
            reverse = pool.submit(create_profile, reverse_refs)
            barrier.wait(timeout=5.0)
            forward_profile = forward.result(timeout=20.0)
            reverse_profile = reverse.result(timeout=20.0)

        assert forward_profile.draft.resource_refs == forward_refs
        assert reverse_profile.draft.resource_refs == reverse_refs
    finally:
        concurrent_engine.dispose()


def test_postgres_row_reads_normalize_non_utc_session_timestamps(
    catalog_lab, owned_agents_pg
) -> None:
    from sqlalchemy import create_engine, select, text

    from sidera_agents.postgres.models import ProfileRevisionRow, ResourceRevisionRow
    from sidera_agents.postgres.store import PostgresAgentStore

    owned_agents_pg.migrate()
    timezone_engine = create_engine(
        owned_agents_pg.url,
        echo=False,
        hide_parameters=True,
        connect_args={
            "options": "-c timezone=Asia/Shanghai -c statement_timeout=10000"
        },
    )
    try:
        catalog = ProfileCatalog(
            PostgresAgentStore(timezone_engine),
            catalog_lab.authorizer,
            catalog_lab.clock,
        )
        resource = catalog.create_resource(
            catalog_lab.workspace_id,
            catalog_lab.skill_draft,
            grant_id=catalog_lab.grant_id,
        )
        profile = catalog.create_profile(
            catalog_lab.workspace_id,
            catalog_lab.draft.model_copy(update={"resource_refs": (resource.ref,)}),
            grant_id=catalog_lab.grant_id,
        )

        with timezone_engine.connect() as connection:
            assert (
                connection.execute(text("SHOW TimeZone")).scalar_one()
                == "Asia/Shanghai"
            )
            raw_profile_time = connection.execute(
                select(ProfileRevisionRow.created_at).where(
                    ProfileRevisionRow.workspace_id == profile.ref.workspace_id,
                    ProfileRevisionRow.profile_id == profile.ref.profile_id,
                    ProfileRevisionRow.revision == profile.ref.revision,
                )
            ).scalar_one()
            raw_resource_time = connection.execute(
                select(ResourceRevisionRow.created_at).where(
                    ResourceRevisionRow.workspace_id == resource.ref.workspace_id,
                    ResourceRevisionRow.resource_id == resource.ref.resource_id,
                    ResourceRevisionRow.revision == resource.ref.revision,
                )
            ).scalar_one()
        assert raw_profile_time.utcoffset() == timedelta(hours=8)
        assert raw_resource_time.utcoffset() == timedelta(hours=8)

        reopened = ProfileCatalog(
            PostgresAgentStore(timezone_engine),
            catalog_lab.authorizer,
            catalog_lab.clock,
        )
        assert (
            reopened.get_profile(profile.ref, grant_id=catalog_lab.grant_id).created_at
            == profile.created_at
        )
        assert (
            reopened.get_resource(
                resource.ref, grant_id=catalog_lab.grant_id
            ).created_at
            == resource.created_at
        )
    finally:
        timezone_engine.dispose()
