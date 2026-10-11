import importlib
import importlib.util
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.catalog import ProfileCatalog
from sidera_agents.contracts import ProfileDraft, RuntimeRef
from sidera_agents.memory import MemoryAgentStore
from sqlalchemy.dialects import postgresql

from sidera_product.composition import ProductRequestAuthorizer
from sidera_product.postgres.models import ProductDefaultsRow
from sidera_product.postgres.store import PostgresProductStore
from tests.unit.test_local_session import service


class DefaultsSQLBoundary:
    def __init__(self, engine):
        self.engine = engine
        self.rows = {}
        self.writes = 0

    def session(self, engine):
        assert engine is self.engine
        owner = self

        class BoundarySession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            @contextmanager
            def begin(self):
                yield self

            def get(self, model, identity):
                assert model is ProductDefaultsRow
                return owner.rows.get(identity)

            def execute(self, statement):
                values = statement.compile(dialect=postgresql.dialect()).params
                current = owner.rows.get(values["workspace_id"])
                valid = (
                    current is None
                    if statement.is_insert
                    else current is not None and current.version == values["version_1"]
                )
                row = None
                if valid:
                    row = ProductDefaultsRow(
                        **{
                            column.key: values[column.key]
                            for column in ProductDefaultsRow.__table__.columns
                        }
                    )
                    owner.rows[row.workspace_id] = row
                    owner.writes += 1
                return SimpleNamespace(scalar_one_or_none=lambda: row)

        return BoundarySession()


def defaults_lab(settings_values, monkeypatch):
    assert importlib.util.find_spec("sidera_product.defaults") is not None, (
        "The owned defaults service is missing"
    )
    module = importlib.import_module("sidera_product.defaults")
    from sidera_product.postgres import store as store_module

    _, sessions, _, clock = service(settings_values)
    identity = sessions.identity_for_grant(
        sessions.exchange(sessions.create_bootstrap().token).session_id
    )
    host = ProductRequestAuthorizer(sessions.settings)
    host.local_sessions = sessions
    product = PostgresProductStore(
        sessions.settings.product_database_url.get_secret_value()
    )
    boundary = DefaultsSQLBoundary(product.engine)
    monkeypatch.setattr(store_module, "Session", boundary.session)
    catalog = ProfileCatalog(MemoryAgentStore(), host, lambda: clock[0])
    draft = ProfileDraft(
        name="All appointments",
        description="",
        runtime_ref=RuntimeRef(
            workspace_id=sessions.settings.workspace_id, runtime_id=uuid4()
        ),
        instructions="Pinned independently",
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )
    profile = catalog.create_profile(
        sessions.settings.workspace_id, draft, grant_id=identity.grant_id
    )
    selected = module.DefaultsService(
        settings=sessions.settings, store=product, sessions=sessions, catalog=catalog
    )
    return SimpleNamespace(
        service=selected,
        module=module,
        catalog=catalog,
        profile=profile,
        identity=identity,
        product=product,
        sessions=sessions,
        clock=clock,
        boundary=boundary,
    )


def test_defaults_empty_read_then_one_profile_can_fill_all_roles(
    settings_values, monkeypatch, no_database_connections
):
    lab = defaults_lab(settings_values, monkeypatch)
    try:
        empty = lab.service.read(lab.identity)
        assert (
            empty.version == 0
            and empty.main_profile_id
            is empty.planner_profile_id
            is empty.worker_profile_id
            is None
        )
        profile_id = lab.profile.ref.profile_id
        saved = lab.service.write(
            lab.identity,
            lab.module.DefaultsBody(
                expected_version=0,
                main_profile_id=profile_id,
                planner_profile_id=profile_id,
                worker_profile_id=profile_id,
            ),
        )
        assert (
            saved.version == 1
            and saved.main_profile_id
            == saved.planner_profile_id
            == saved.worker_profile_id
            == profile_id
        )
        assert lab.service.read(lab.identity) == saved and lab.boundary.writes == 1
        assert (
            lab.catalog.get_profile(lab.profile.ref, grant_id=lab.identity.grant_id)
            == lab.profile
        )
    finally:
        lab.product.close()


@pytest.mark.parametrize(
    "variant", ["stale", "foreign", "disabled", "revoked", "expired", "principal"]
)
def test_defaults_deny_stale_or_unavailable_selection_without_mutating_existing_pins(
    settings_values, monkeypatch, no_database_connections, variant
):
    lab = defaults_lab(settings_values, monkeypatch)
    try:
        profile_id = lab.profile.ref.profile_id
        lab.service.write(
            lab.identity,
            lab.module.DefaultsBody(
                expected_version=0,
                main_profile_id=profile_id,
                planner_profile_id=None,
                worker_profile_id=None,
            ),
        )
        identity, expected = lab.identity, 1
        if variant == "stale":
            expected = 0
        elif variant == "foreign":
            profile_id = uuid4()
        elif variant == "disabled":
            lab.catalog.set_profile_enabled(
                lab.profile.ref.workspace_id,
                profile_id,
                False,
                1,
                grant_id=identity.grant_id,
            )
        elif variant == "revoked":
            lab.sessions.revoke(identity.grant_id)
        elif variant == "expired":
            row = lab.sessions.store.sessions[identity.grant_id]
            lab.sessions.store.sessions[identity.grant_id] = replace(
                row, expires_at=lab.clock[0]
            )
        else:
            identity = replace(identity, principal_id="someone-else")
        with pytest.raises(lab.module.DefaultsError) as error:
            lab.service.write(
                identity,
                lab.module.DefaultsBody(
                    expected_version=expected,
                    main_profile_id=profile_id,
                    planner_profile_id=profile_id,
                    worker_profile_id=profile_id,
                ),
            )
        assert error.value.code in {
            "version_conflict",
            "profile_unavailable",
            "local_session_required",
        }
        assert lab.boundary.writes == 1
        original = next(iter(lab.boundary.rows.values()))
        assert (
            original.version == 1
            and original.main_profile_id == lab.profile.ref.profile_id
        )
        assert original.planner_profile_id is original.worker_profile_id is None
    finally:
        lab.product.close()
