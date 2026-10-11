import importlib
import importlib.util
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, RLock, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sidera_agents.catalog import ProfileCatalog
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    ResourceRef,
    ResourceRevision,
    RuntimeRef,
)
from sidera_agents.errors import AgentError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from sidera_product.composition import ProductRequestAuthorizer
from sidera_product.postgres.models import ConfigurationRequestRow
from sidera_product.postgres.store import PostgresProductStore
from tests.unit.test_local_session import service


def configuration_contracts():
    assert importlib.util.find_spec("sidera_product.agents") is not None, (
        "The owned configuration request package is missing"
    )
    return importlib.import_module("sidera_product.agents.contracts")


@pytest.mark.parametrize(
    "operation", ["browser-save:1", "opaque/slash original", " x ", "x" * 128]
)
def test_configuration_result_retains_exact_operation_and_safe_refs(operation):
    module = configuration_contracts()
    ref = ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1)
    result = module.ConfigurationResult(
        operationId=operation,
        state="reconciled",
        target_ref=ref,
        result_ref=ref,
        reason_code=None,
    )
    assert (
        result.operation_id == operation
        and result.result_ref == result.target_ref == ref
    )
    assert set(result.model_dump(by_alias=True)) == {
        "operationId",
        "state",
        "target_ref",
        "result_ref",
        "reason_code",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"operationId": ""},
        {"operationId": "x" * 129},
        {"state": "applied"},
        {"grant_id": str(uuid4())},
    ],
)
def test_configuration_result_rejects_unbounded_or_execution_receipt_fields(payload):
    module = configuration_contracts()
    ref = ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1)
    values = {
        "operationId": "original",
        "state": "unknown",
        "target_ref": ref,
        "result_ref": None,
        "reason_code": None,
    }
    values.update(payload)
    with pytest.raises(ValidationError):
        module.ConfigurationResult.model_validate(values)


def test_configuration_hash_sorts_objects_preserves_lists_and_rejects_nan():
    module = configuration_contracts()
    assert module.canonical_hash({"b": 2, "a": [1, 2]}) == module.canonical_hash(
        {"a": [1, 2], "b": 2}
    )
    assert module.canonical_hash({"a": [2, 1]}) != module.canonical_hash({"a": [1, 2]})
    with pytest.raises(ValueError):
        module.canonical_hash({"a": float("nan")})


def test_configuration_journal_has_only_safe_owned_identity_and_hash_columns():
    from sidera_product.postgres.models import Base

    assert "product.configuration_requests" in Base.metadata.tables
    row = Base.metadata.tables["product.configuration_requests"]
    assert set(row.columns.keys()) == {
        "workspace_id",
        "principal_id",
        "operation_id",
        "action",
        "request_fingerprint",
        "draft_fingerprint",
        "target_ref",
        "expected_ref",
        "state",
        "version",
        "created_at",
        "updated_at",
        "reason_code",
        "result_ref",
    }
    assert not row.foreign_keys
    assert any(
        {column.key for column in constraint.columns}
        == {"workspace_id", "principal_id", "operation_id"}
        for constraint in row.constraints
    )


def test_configuration_record_rejects_draft_content_and_hides_hashes_in_result():
    module = configuration_contracts()
    when = datetime(2026, 10, 5, tzinfo=UTC)
    ref = ProfileRef(workspace_id=uuid4(), profile_id=uuid4(), revision=1)
    values = {
        "workspace_id": ref.workspace_id,
        "principal_id": "operator",
        "operation_id": "exact/original",
        "action": "profile.create",
        "request_fingerprint": "a" * 64,
        "draft_fingerprint": "b" * 64,
        "target_ref": ref,
        "expected_ref": None,
        "state": "dispatching",
        "version": 1,
        "created_at": when,
        "updated_at": when,
        "reason_code": None,
        "result_ref": None,
    }
    record = module.ConfigurationRequest.model_validate(values)
    assert record.target_ref == ref
    with pytest.raises(ValidationError):
        module.ConfigurationRequest.model_validate(
            {**values, "draft": {"instructions": "Never retained."}}
        )
    assert (
        "fingerprint"
        not in module.ConfigurationResult.from_request(record).model_dump_json()
    )


class ConfigurationSQLBoundary:
    """Only SQL I/O is controlled; the real journal elects and settles requests."""

    def __init__(self, engine):
        self.engine = engine
        self.rows = {}
        self.queries = []
        self.lock = RLock()
        self.open_sessions = 0
        self.fail_insert = False

    def session(self, engine):
        assert engine is self.engine
        owner = self

        class BoundarySession:
            def __enter__(self):
                with owner.lock:
                    owner.open_sessions += 1
                return self

            def __exit__(self, *args):
                with owner.lock:
                    owner.open_sessions -= 1

            @contextmanager
            def begin(self):
                with owner.lock:
                    yield self

            def flush(self):
                return None

            def execute(self, statement):
                values = statement.compile(dialect=postgresql.dialect()).params
                owner.queries.append(values)
                if statement.is_insert:
                    if owner.fail_insert:
                        raise SQLAlchemyError("controlled unavailable")
                    assert statement.table.name == "configuration_requests"
                    assert "ON CONFLICT" in str(
                        statement.compile(dialect=postgresql.dialect())
                    )
                    key = tuple(
                        values[name]
                        for name in ("workspace_id", "principal_id", "operation_id")
                    )
                    row = None
                    if key not in owner.rows:
                        row = ConfigurationRequestRow(
                            **{
                                column.key: values[column.key]
                                for column in ConfigurationRequestRow.__table__.columns
                            }
                        )
                        owner.rows[key] = row
                else:
                    assert (
                        statement.column_descriptions[0]["entity"]
                        is ConfigurationRequestRow
                    )
                    assert set(values) == {
                        "workspace_id_1",
                        "principal_id_1",
                        "operation_id_1",
                    }
                    key = tuple(
                        values[name + "_1"]
                        for name in ("workspace_id", "principal_id", "operation_id")
                    )
                    row = owner.rows.get(key)
                return SimpleNamespace(scalar_one_or_none=lambda: row)

        return BoundarySession()


class CatalogPersistenceBoundary:
    def __init__(self, journal):
        self.journal = journal
        self.profiles = {}
        self.resources = {}
        self.calls = []
        self.reads = []
        self.lock = RLock()
        self.write_entered = Event()
        self.release_write = Event()
        self.hold_write = False
        self.lose_reply = False
        self.read_hook = None

    def _save(self, ref, draft, fingerprint, created_at):
        assert self.journal.open_sessions == 0
        self.write_entered.set()
        if self.hold_write:
            assert self.release_write.wait(3)
        with self.lock:
            values = self.profiles if isinstance(ref, ProfileRef) else self.resources
            if ref in values:
                raise AgentError("operation_conflict", "Duplicate immutable identity.")
            row = (
                ProfileRevision(
                    ref=ref, draft=draft, config_hash=fingerprint, created_at=created_at
                )
                if isinstance(ref, ProfileRef)
                else ResourceRevision(
                    ref=ref,
                    draft=draft,
                    content_hash=fingerprint,
                    created_at=created_at,
                )
            )
            values[ref] = row
            self.calls.append(ref)
        if self.lose_reply:
            raise OSError("controlled lost response")
        return row

    def create_profile(self, workspace_id, profile_id, draft, config_hash, created_at):
        return self._save(
            ProfileRef(workspace_id=workspace_id, profile_id=profile_id, revision=1),
            draft,
            config_hash,
            created_at,
        )

    def create_resource(
        self, workspace_id, resource_id, draft, content_hash, created_at
    ):
        return self._save(
            ResourceRef(workspace_id=workspace_id, resource_id=resource_id, revision=1),
            draft,
            content_hash,
            created_at,
        )

    def _read(self, ref):
        assert self.journal.open_sessions == 0
        self.reads.append(ref)
        if self.read_hook is not None:
            self.read_hook(ref)
        values = self.profiles if isinstance(ref, ProfileRef) else self.resources
        if ref not in values:
            raise AgentError("not_found", "Immutable revision is not available.")
        return values[ref]

    get_profile = _read
    get_resource = _read

    def get_profile_head(self, workspace_id, profile_id):
        return max(
            (
                row
                for ref, row in self.profiles.items()
                if ref.workspace_id == workspace_id and ref.profile_id == profile_id
            ),
            key=lambda row: row.ref.revision,
        )

    def get_resource_head(self, workspace_id, resource_id):
        return max(
            (
                row
                for ref, row in self.resources.items()
                if ref.workspace_id == workspace_id and ref.resource_id == resource_id
            ),
            key=lambda row: row.ref.revision,
        )

    def append_profile_revision(self, ref, draft, fingerprint, created_at):
        return self._save(
            ref.model_copy(update={"revision": ref.revision + 1}),
            draft,
            fingerprint,
            created_at,
        )

    def append_resource_revision(self, ref, draft, fingerprint, created_at):
        return self._save(
            ref.model_copy(update={"revision": ref.revision + 1}),
            draft,
            fingerprint,
            created_at,
        )


def configuration_lab(settings_values, monkeypatch):
    assert (
        importlib.util.find_spec("sidera_product.agents.configuration") is not None
    ), "The real configuration service is missing"
    from sidera_product.agents import configuration, configuration_store

    _, sessions, _, clock = service(settings_values)
    identity = sessions.identity_for_grant(
        sessions.exchange(sessions.create_bootstrap().token).session_id
    )
    host = ProductRequestAuthorizer(sessions.settings)
    host.local_sessions = sessions
    product = PostgresProductStore(
        sessions.settings.product_database_url.get_secret_value()
    )
    boundary = ConfigurationSQLBoundary(product.engine)
    monkeypatch.setattr(configuration_store, "Session", boundary.session)
    journal = configuration_store.PostgresConfigurationStore(product.engine)
    persistence = CatalogPersistenceBoundary(boundary)
    catalog = ProfileCatalog(persistence, host, lambda: clock[0])

    def reopen():
        return configuration.ConfigurationService(
            settings=sessions.settings,
            store=journal,
            sessions=sessions,
            catalog=catalog,
            clock=lambda: clock[0],
        )

    draft = ProfileDraft(
        name="Reviewed Profile",
        description="",
        runtime_ref=RuntimeRef(
            workspace_id=sessions.settings.workspace_id, runtime_id=uuid4()
        ),
        instructions="Pinned instructions",
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )
    resource = CapabilityResourceDraft(
        name="Reviewed skill",
        kind="skill",
        settings={"body": "Owned guidance"},
        secret_refs=(),
    )
    return SimpleNamespace(
        configuration=reopen(),
        reopen=reopen,
        module=configuration,
        product=product,
        boundary=boundary,
        store=journal,
        catalog=catalog,
        persistence=persistence,
        sessions=sessions,
        identity=identity,
        draft=draft,
        resource=resource,
    )


@pytest.mark.parametrize("entity", ["profile", "resource"])
def test_configuration_parallel_duplicate_has_one_dispatch_and_no_catalog_lock(
    settings_values, monkeypatch, no_database_connections, entity
):
    lab = configuration_lab(settings_values, monkeypatch)
    draft = lab.draft if entity == "profile" else lab.resource
    lab.persistence.hold_write = True
    results, errors = [], []

    def winner():
        try:
            results.append(
                lab.configuration.submit(
                    lab.identity,
                    action=entity + ".create",
                    operation_id="same/operation",
                    draft=draft,
                )
            )
        except Exception as error:  # noqa: BLE001 - retain thread failures for the final assertion
            errors.append(error)

    worker = Thread(target=winner)
    try:
        worker.start()
        assert lab.persistence.write_entered.wait(3)
        duplicate = lab.reopen().submit(
            lab.identity,
            action=entity + ".create",
            operation_id="same/operation",
            draft=draft,
        )
        assert duplicate.state == "unknown"
        assert lab.boundary.open_sessions == 0
        assert len(lab.boundary.rows) == 1
        lab.persistence.release_write.set()
        worker.join(3)
        assert not worker.is_alive() and errors == []
        assert results[0].state == "succeeded"
        assert results[0].target_ref == duplicate.target_ref
        assert lab.persistence.calls == [duplicate.target_ref]
        assert (
            lab.reopen().read_original(lab.identity, "same/operation").state
            == "succeeded"
        )
    finally:
        lab.persistence.release_write.set()
        worker.join(3)
        lab.product.close()


@pytest.mark.parametrize("entity", ["profile", "resource"])
@pytest.mark.parametrize("revise", [False, True])
def test_configuration_committed_lost_reply_reconciles_exact_original_after_restart(
    settings_values, monkeypatch, no_database_connections, entity, revise
):
    lab = configuration_lab(settings_values, monkeypatch)
    draft = lab.draft if entity == "profile" else lab.resource
    expected = None
    try:
        if revise:
            initial = lab.configuration.submit(
                lab.identity,
                action=entity + ".create",
                operation_id="first",
                draft=draft,
            )
            expected = initial.result_ref
            draft = draft.model_copy(update={"name": "Reviewed next revision"})
        count = len(lab.persistence.calls)
        lab.persistence.lose_reply = True
        result = lab.configuration.submit(
            lab.identity,
            action=entity + (".revise" if revise else ".create"),
            operation_id="lost/original",
            draft=draft,
            expected_ref=expected,
        )
        assert result.state == "unknown" and result.result_ref is None
        lab.persistence.lose_reply = False
        saved = lab.reopen().read_original(lab.identity, "lost/original")
        assert saved.state == "reconciled" and saved.result_ref == result.target_ref
        assert len(lab.persistence.calls) == count + 1
        assert lab.persistence.reads[-1] == result.target_ref
        assert (
            lab.reopen().submit(
                lab.identity,
                action=entity + (".revise" if revise else ".create"),
                operation_id="lost/original",
                draft=draft,
                expected_ref=expected,
            )
            == saved
        )
        assert len(lab.persistence.calls) == count + 1
    finally:
        lab.product.close()


def test_configuration_durable_settlement_lost_after_write_is_read_only_recoverable(
    settings_values, monkeypatch, no_database_connections
):
    from sidera_product.postgres.store import ProductStoreError

    lab = configuration_lab(settings_values, monkeypatch)
    original = lab.store.settle

    def unavailable(*args, **kwargs):
        raise ProductStoreError("product_store_unavailable")

    monkeypatch.setattr(lab.store, "settle", unavailable)
    try:
        result = lab.configuration.submit(
            lab.identity,
            action="profile.create",
            operation_id="lost-journal",
            draft=lab.draft,
        )
        assert result.state == "unknown" and len(lab.persistence.calls) == 1
        monkeypatch.setattr(lab.store, "settle", original)
        saved = lab.reopen().read_original(lab.identity, "lost-journal")
        assert saved.state == "reconciled" and saved.result_ref == result.target_ref
        assert lab.persistence.calls == [result.target_ref]
    finally:
        lab.product.close()


@pytest.mark.parametrize("change", ["draft", "action", "expected"])
def test_configuration_changed_original_intent_conflicts_before_catalog_io(
    settings_values, monkeypatch, no_database_connections, change
):
    lab = configuration_lab(settings_values, monkeypatch)
    try:
        original = lab.configuration.submit(
            lab.identity,
            action="profile.create",
            operation_id="original",
            draft=lab.draft,
        )
        before = (list(lab.persistence.calls), list(lab.persistence.reads))
        action, draft, expected = "profile.create", lab.draft, None
        if change == "draft":
            draft = draft.model_copy(
                update={"instructions": "Different reviewed intent"}
            )
        elif change == "action":
            action, draft = "resource.create", lab.resource
        else:
            action, expected = "profile.revise", original.target_ref
        with pytest.raises(lab.module.ConfigurationError) as error:
            lab.reopen().submit(
                lab.identity,
                action=action,
                operation_id="original",
                draft=draft,
                expected_ref=expected,
            )
        assert error.value.code == "operation_conflict"
        assert (lab.persistence.calls, lab.persistence.reads) == before
    finally:
        lab.product.close()


def test_configuration_not_found_and_different_reserved_draft_never_resend(
    settings_values, monkeypatch, no_database_connections
):
    lab = configuration_lab(settings_values, monkeypatch)
    try:
        lab.persistence.lose_reply = True
        result = lab.configuration.submit(
            lab.identity,
            action="profile.create",
            operation_id="original",
            draft=lab.draft,
        )
        saved = lab.persistence.profiles.pop(result.target_ref)
        unknown = lab.reopen().read_original(lab.identity, "original")
        assert unknown.state == "unknown" and unknown.result_ref is None
        lab.persistence.profiles[result.target_ref] = saved.model_copy(
            update={"draft": lab.draft.model_copy(update={"name": "Different"})}
        )
        conflict = lab.reopen().read_original(lab.identity, "original")
        assert (
            conflict.state == "unknown"
            and conflict.reason_code == "configuration_write_conflict"
        )
        assert conflict.result_ref is None and lab.persistence.calls == [
            result.target_ref
        ]
    finally:
        lab.product.close()


def test_configuration_later_head_does_not_replace_original_revision(
    settings_values, monkeypatch, no_database_connections
):
    lab = configuration_lab(settings_values, monkeypatch)
    try:
        lab.persistence.lose_reply = True
        result = lab.configuration.submit(
            lab.identity,
            action="profile.create",
            operation_id="original",
            draft=lab.draft,
        )
        lab.persistence.lose_reply = False
        lab.catalog.revise_profile(
            result.target_ref,
            lab.draft.model_copy(
                update={"instructions": "A later independent revision"}
            ),
            grant_id=lab.identity.grant_id,
        )
        observed = lab.reopen().read_original(lab.identity, "original")
        assert (
            observed.state == "reconciled" and observed.result_ref == result.target_ref
        )
        assert observed.result_ref.revision == 1
        assert [ref.revision for ref in lab.persistence.calls] == [1, 2]
    finally:
        lab.product.close()


def test_configuration_stale_unknown_cannot_demote_confirmed_settlement(
    settings_values, monkeypatch, no_database_connections
):
    lab = configuration_lab(settings_values, monkeypatch)
    try:
        result = lab.configuration.submit(
            lab.identity,
            action="profile.create",
            operation_id="original",
            draft=lab.draft,
        )
        saved = lab.store.get(
            workspace_id=lab.sessions.settings.workspace_id,
            principal_id=lab.identity.principal_id,
            operation_id="original",
        )
        stale = saved.model_copy(
            update={
                "state": "dispatching",
                "version": saved.version - 1,
                "result_ref": None,
            }
        )
        late = lab.store.settle(
            stale,
            state="unknown",
            reason_code="configuration_result_unavailable",
            result_ref=None,
            now=saved.updated_at,
            observation=True,
        )
        assert late.state == "succeeded" and late.result_ref == result.result_ref
    finally:
        lab.product.close()


@pytest.mark.parametrize(
    "variant", ["revoked", "foreign_owner", "agent", "foreign_ref", "foreign_operation"]
)
def test_configuration_original_query_denies_before_catalog_reads(
    settings_values, monkeypatch, no_database_connections, variant
):
    from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

    lab = configuration_lab(settings_values, monkeypatch)
    try:
        lab.persistence.lose_reply = True
        lab.configuration.submit(
            lab.identity,
            action="profile.create",
            operation_id="original",
            draft=lab.draft,
        )
        before = (list(lab.persistence.calls), list(lab.persistence.reads))
        identity, operation = lab.identity, "original"
        if variant == "revoked":
            lab.sessions.revoke(identity.grant_id)
        elif variant == "foreign_owner":
            identity = ControlPlaneIdentity("someone-else", identity.grant_id, "user")
        elif variant == "agent":
            identity = ControlPlaneIdentity(
                identity.principal_id, identity.grant_id, "agent"
            )
        elif variant == "foreign_ref":
            row = next(iter(lab.boundary.rows.values()))
            row.target_ref = {**row.target_ref, "workspace_id": str(uuid4())}
        else:
            operation = "not-owned"
        with pytest.raises((lab.module.ConfigurationError, AgentError)):
            lab.reopen().read_original(identity, operation)
        assert (lab.persistence.calls, lab.persistence.reads) == before
    finally:
        lab.product.close()


def test_configuration_secret_invalid_draft_is_not_retained_or_echoed(
    settings_values, monkeypatch, no_database_connections
):
    lab = configuration_lab(settings_values, monkeypatch)
    sentinel = "sk-" + "0" * 46
    try:
        result = lab.configuration.submit(
            lab.identity,
            action="profile.create",
            operation_id="invalid-draft",
            draft=lab.draft.model_copy(
                update={"environment": {"OPENAI_API_KEY": sentinel}}
            ),
        )
        assert (
            result.state == "rejected" and result.reason_code == "configuration_invalid"
        )
        assert lab.persistence.calls == []
        assert sentinel not in result.model_dump_json()
        assert sentinel not in repr(lab.boundary.queries)
        for row in lab.boundary.rows.values():
            assert sentinel not in repr(
                {
                    column.key: getattr(row, column.key)
                    for column in ConfigurationRequestRow.__table__.columns
                }
            )
    finally:
        lab.product.close()


def test_configuration_journal_failure_before_dispatch_has_zero_catalog_writes(
    settings_values, monkeypatch, no_database_connections
):
    lab = configuration_lab(settings_values, monkeypatch)
    lab.boundary.fail_insert = True
    try:
        with pytest.raises(lab.module.ConfigurationError) as error:
            lab.configuration.submit(
                lab.identity,
                action="profile.create",
                operation_id="original",
                draft=lab.draft,
            )
        assert error.value.code == "product_store_unavailable"
        assert lab.persistence.calls == lab.persistence.reads == []
        assert lab.boundary.open_sessions == 0
    finally:
        lab.product.close()
