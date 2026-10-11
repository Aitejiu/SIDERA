import importlib.util
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import URL


def guard_module():
    assert importlib.util.find_spec("tests.support.product_targets") is not None, (
        "N17 integration must guard all four explicitly selected targets"
    )
    from tests.support import product_targets

    return product_targets


def selection():
    allocation = "sidera-native-harness:product:n17:20990101:synthetic"
    values = {
        "SIDERA_PRODUCT_TEST_ALLOCATION_ID": allocation,
        "SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY": "/private/tmp/inert-product-test-socket",
        "SIDERA_PRODUCT_TEST_PORT": "5432",
    }
    for component, dbkind in (
        ("product", "product"),
        ("agents", "agents"),
        ("cp", "native_cp"),
        ("credentials", "cfg2"),
    ):
        prefix = "SIDERA_PRODUCT_TEST_" + component.upper() + "_"
        owner = "n17_" + component + "_synthetic"
        database = "sidera_" + dbkind + "_test_n17_synthetic"
        values.update(
            {
                prefix + "DATABASE_URL": URL.create(
                    "postgresql+psycopg",
                    username=owner,
                    database=database,
                    query={
                        "host": values["SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY"],
                        "port": "5432",
                    },
                ).render_as_string(hide_password=False),
                prefix + "EXPECTED_DATABASE": database,
                prefix + "EXPECTED_OWNER": owner,
                prefix + "EXPECTED_MARKER": allocation + ":" + component,
            }
        )
    return values


def test_absent_selection_is_not_run_but_partial_is_error():
    guard = guard_module()
    assert guard.select_targets({}) is None
    with pytest.raises(guard.ProductTargetError, match="selection_required"):
        guard.select_targets({"SIDERA_PRODUCT_TEST_PORT": "5432"})


def test_all_four_targets_are_exact_and_fixed_order():
    guard = guard_module()
    targets = guard.select_targets(selection())
    assert [target.component for target in targets] == [
        "product",
        "agents",
        "cp",
        "credentials",
    ]
    assert [target.lock_index for target in targets] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "change",
    [
        {"SIDERA_PRODUCT_TEST_CP_EXPECTED_OWNER": "n17_alloc_admin"},
        {"SIDERA_PRODUCT_TEST_CP_EXPECTED_DATABASE": "production"},
        {"SIDERA_PRODUCT_TEST_CP_EXPECTED_MARKER": "WRONG_MARKER_SECRET_SENTINEL"},
        {"SIDERA_PRODUCT_TEST_PORT": "5433"},
        {"SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY": "untrusted.example"},
        {"SIDERA_PRODUCT_TEST_SOCKET_DIRECTORY": "/private/tmp/../untrusted"},
        {"SIDERA_PRODUCT_TEST_CP_DATABASE_URL": "INVALID_DSN_SECRET_SENTINEL"},
        {"PGSERVICE": "HOSTILE_SERVICE_SECRET_SENTINEL"},
    ],
)
def test_unsafe_configured_selection_fails_without_echo_or_connection(change):
    guard = guard_module()
    with pytest.raises(guard.ProductTargetError) as error:
        guard.select_targets({**selection(), **change})
    assert "SECRET_SENTINEL" not in str(error.value)


def test_role_marker_and_object_checks_fail_closed():
    guard = guard_module()
    target = guard.select_targets(selection())[0]
    identity = {
        "database_name": target.database,
        "current_user": target.owner,
        "owner": target.owner,
        "marker": target.marker,
        "unix_connection": True,
        "login": True,
        "superuser": False,
        "create_role": False,
        "create_database": False,
        "replication": False,
        "bypass_rls": False,
        "privileged_memberships": False,
    }
    guard.verify_identity(target, identity)
    for name, value in (
        ("owner", "another_owner"),
        ("marker", "another_marker"),
        ("superuser", True),
        ("privileged_memberships", True),
        ("unix_connection", False),
        ("login", False),
    ):
        with pytest.raises(guard.ProductTargetError, match="identity_refused"):
            guard.verify_identity(target, {**identity, name: value})


def test_partial_guard_failure_releases_only_acquired_resources(monkeypatch):
    guard = guard_module()
    targets = guard.select_targets(selection())
    events = []

    class Owned:
        def __init__(self, target):
            self.target = target

        def release(self):
            events.append(("release", self.target.component))
            return True

    def acquire(target):
        events.append(("acquire", target.component))
        if target.component == "cp":
            raise guard.ProductTargetError("controlled_guard_denied")
        return Owned(target)

    monkeypatch.setattr(guard, "acquire_target", acquire)
    with pytest.raises(guard.ProductTargetError, match="controlled_guard_denied"):
        guard.acquire_guards(targets)
    assert events == [
        ("acquire", "product"),
        ("acquire", "agents"),
        ("acquire", "cp"),
        ("release", "agents"),
        ("release", "product"),
    ]


@pytest.mark.parametrize("operation", ["row_fingerprints", "inspect"])
@pytest.mark.parametrize("fails", [False, True])
def test_guard_reader_ends_transaction_without_releasing_session_guard(
    monkeypatch, operation, fails
):
    module = guard_module()
    target = module.select_targets(selection())[0]

    class Result:
        def mappings(self):
            return self

        def one(self):
            return {}

        def scalars(self):
            return ['{"owned":"original"}']

    class Reader:
        dialect = postgresql.dialect()
        transaction_active = False
        session_guard_owned = True
        closed = False
        rollbacks = 0

        def rollback(self):
            self.transaction_active = False
            self.rollbacks += 1

        def execute(self, statement):
            self.transaction_active = True
            return Result()

    reader = Reader()

    def inventory(connection, selected):
        assert connection is reader and selected is target
        reader.transaction_active = True
        if fails:
            raise module.ProductTargetError("controlled_reader_failure")
        return {
            "relations": [
                {"schema": "product", "name": "browser_sessions", "kind": "r"}
            ]
        }

    monkeypatch.setattr(module, "verify_identity", lambda *_: None)
    monkeypatch.setattr(module, "verify_objects", inventory)
    owned = module.TargetGuard(target, SimpleNamespace(), reader, {})
    if fails:
        with pytest.raises(
            module.ProductTargetError, match="controlled_reader_failure"
        ):
            getattr(owned, operation)()
    else:
        result = getattr(owned, operation)()
        assert result
    assert reader.transaction_active is False
    assert reader.rollbacks >= 1
    assert reader.session_guard_owned is True
    assert reader.closed is False
    assert owned.active is True


@pytest.mark.parametrize(
    "failure", ["head", "fingerprint", "migration", "interrupt", "readback"]
)
def test_selected_fixture_releases_guards_on_early_failure(
    monkeypatch, tmp_path, failure
):
    from tests.integration import conftest as integration

    events = []

    class Owned:
        def __init__(self, component):
            self.target = SimpleNamespace(component=component)
            heads = {
                "product": "0001_product_defaults_sessions",
                "agents": "0004_inputs_events",
                "cp": "0039_hard_only_input_selections",
                "credentials": "0001_connections_credentials",
            }
            self.initial = {"inventory": {"heads": [heads[component]]}}
            self.reads = 0

        def row_fingerprints(self):
            self.reads += 1
            if self.target.component == "product" and (
                (failure == "fingerprint" and self.reads == 1)
                or (failure == "readback" and self.reads == 2)
            ):
                raise RuntimeError("controlled_early_failure")
            return {"original": {"rows": 1, "sha256": "preserved"}}

        def inspect(self):
            return {"idle": {"other_clients": 0}}

        def release(self):
            events.append(("release", self.target.component))
            return True

    guards = [
        Owned(component) for component in ("product", "agents", "cp", "credentials")
    ]
    if failure == "head":
        guards[0].initial["inventory"]["heads"] = ["wrong_head"]

    def close():
        events.append(("close", "owner"))
        return True

    lab = SimpleNamespace(
        runtime=SimpleNamespace(inspect_readiness=lambda: {"product": True}),
        safe_close=close,
    )

    def migrate(runtime):
        if failure == "migration":
            raise RuntimeError("controlled_early_failure")
        if failure == "interrupt":
            raise KeyboardInterrupt()

    monkeypatch.setattr(integration, "_retained_incomplete", [])
    monkeypatch.setattr(integration, "select_targets", lambda _: object())
    monkeypatch.setattr(integration, "acquire_guards", lambda _: guards)
    monkeypatch.setattr(integration, "build_lab", lambda *_: lab)
    monkeypatch.setattr(integration, "migrate_runtime", migrate)
    monkeypatch.setattr(integration, "write_evidence", lambda *_: None)
    factory = SimpleNamespace(mktemp=lambda _: tmp_path)
    expected = (
        AssertionError
        if failure == "head"
        else KeyboardInterrupt
        if failure == "interrupt"
        else RuntimeError
    )
    fixture = integration.selected_product_targets.__wrapped__(factory)
    with pytest.raises(expected):
        next(fixture)
    releases = [event for event in events if event[0] == "release"]
    assert releases == [
        ("release", component)
        for component in ("credentials", "cp", "agents", "product")
    ]
    if failure in {"migration", "interrupt", "readback"}:
        assert events.index(("close", "owner")) < events.index(releases[0])


def test_selected_fixture_retains_guards_for_unconfirmed_early_owner_close(
    monkeypatch, tmp_path
):
    from tests.integration import conftest as integration

    released = []
    guards = [
        SimpleNamespace(
            target=SimpleNamespace(component=component),
            initial={"inventory": {"heads": [head]}},
            row_fingerprints=lambda: {},
            release=lambda: released.append(True) or True,
        )
        for component, head in (
            ("product", "0001_product_defaults_sessions"),
            ("agents", "0004_inputs_events"),
            ("cp", "0039_hard_only_input_selections"),
            ("credentials", "0001_connections_credentials"),
        )
    ]
    retained = []
    lab = SimpleNamespace(
        runtime=SimpleNamespace(inspect_readiness=lambda: {"product": True}),
        safe_close=lambda: False,
    )
    monkeypatch.setattr(integration, "_retained_incomplete", retained)
    monkeypatch.setattr(integration, "select_targets", lambda _: object())
    monkeypatch.setattr(integration, "acquire_guards", lambda _: guards)
    monkeypatch.setattr(integration, "build_lab", lambda *_: lab)
    monkeypatch.setattr(integration, "migrate_runtime", lambda _: None)
    monkeypatch.setattr(integration, "write_evidence", lambda *_: None)
    fixture = integration.selected_product_targets.__wrapped__(
        SimpleNamespace(mktemp=lambda _: tmp_path)
    )
    with pytest.raises(pytest.fail.Exception, match="cleanup incomplete"):
        next(fixture)
    assert released == []
    assert retained == [lab, guards]
