import hashlib
import importlib
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from starlette.requests import Request

from sidera_product.config import ProductSettings


def session_module():
    assert importlib.util.find_spec("sidera_product.auth") is not None, (
        "N18 must provide real local-session authority"
    )
    return importlib.import_module("sidera_product.auth.local_session")


class MemorySessionStore:
    """Unit-only store boundary; actual atomic SQL is covered by guarded PG tests."""

    def __init__(self):
        self.bootstraps = {}
        self.sessions = {}
        self.lock = Lock()

    def save_bootstrap(self, record):
        self.bootstraps[record.token_digest] = record

    def exchange_bootstrap(self, digest, session, *, now):
        with self.lock:
            bootstrap = self.bootstraps.get(digest)
            if bootstrap is None:
                return "missing"
            if bootstrap.consumed_at is not None:
                return "consumed"
            if bootstrap.expires_at <= now:
                return "expired"
            self.bootstraps[digest] = replace(bootstrap, consumed_at=now)
            self.sessions[session.session_id] = session
            return "accepted"

    def find_session(self, *, token_digest=None, session_id=None):
        if session_id is not None:
            return self.sessions.get(session_id)
        return next(
            (row for row in self.sessions.values() if row.token_digest == token_digest),
            None,
        )

    def revoke_session(self, session_id, *, now):
        row = self.sessions.get(session_id)
        if row is not None:
            self.sessions[session_id] = replace(row, revoked_at=now)


def service(settings_values):
    module = session_module()
    settings = ProductSettings.model_validate(settings_values)
    store = MemorySessionStore()
    clock = [datetime(2026, 10, 5, tzinfo=UTC)]
    sessions = module.LocalSessions(settings, store, clock=lambda: clock[0])
    return module, sessions, store, clock


def request(settings, issue=None, *, method="GET", csrf=True, **extra):
    headers = {"host": "127.0.0.1:18080"}
    if issue is not None:
        headers["cookie"] = (
            "sidera_local_session="
            + issue.token.get_secret_value()
            + "; sidera_csrf="
            + issue.csrf.get_secret_value()
        )
    if method == "POST":
        headers["origin"] = settings.web_origin
        if csrf and issue is not None:
            headers["x-sidera-csrf"] = issue.csrf.get_secret_value()
    headers.update(extra)
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/commands",
            "headers": [
                (key.encode(), value.encode()) for key, value in headers.items()
            ],
            "query_string": b"",
        }
    )


def test_bootstrap_exchange_stores_only_digests_and_consumes_original(settings_values):
    module, sessions, store, clock = service(settings_values)
    bootstrap = sessions.create_bootstrap()
    assert len(bootstrap.token.get_secret_value()) >= 43
    assert bootstrap.expires_at == clock[0] + timedelta(seconds=300)
    issued = sessions.exchange(bootstrap.token)
    assert issued.expires_at == clock[0] + timedelta(seconds=43200)
    row = store.sessions[issued.session_id]
    assert (
        row.token_digest
        == hashlib.sha256(issued.token.get_secret_value().encode()).hexdigest()
    )
    assert (
        row.csrf_digest
        == hashlib.sha256(issued.csrf.get_secret_value().encode()).hexdigest()
    )
    for secret in (bootstrap.token, issued.token, issued.csrf):
        assert secret.get_secret_value() not in repr(store.bootstraps)
        assert secret.get_secret_value() not in repr(store.sessions)
        assert secret.get_secret_value() not in repr(bootstrap)
        assert secret.get_secret_value() not in repr(issued)
    with pytest.raises(module.LocalSessionError) as error:
        sessions.exchange(bootstrap.token)
    assert error.value.code == "bootstrap_consumed"
    assert len(store.sessions) == 1


@pytest.mark.parametrize("variant", ["wrong", "empty", "expired"])
def test_invalid_bootstrap_does_not_create_a_session(settings_values, variant):
    module, sessions, store, clock = service(settings_values)
    bootstrap = sessions.create_bootstrap()
    token = bootstrap.token
    if variant == "wrong":
        token = SecretStr("WRONG_BOOTSTRAP_SECRET_SENTINEL")
    elif variant == "empty":
        token = SecretStr("")
    else:
        clock[0] = bootstrap.expires_at
    with pytest.raises(module.LocalSessionError) as error:
        sessions.exchange(token)
    assert error.value.code == (
        "bootstrap_expired" if variant == "expired" else "bootstrap_invalid"
    )
    assert "SECRET_SENTINEL" not in str(error.value)
    assert store.sessions == {}


def test_cookie_identity_restart_and_revocation_keep_exact_session(settings_values):
    module, sessions, store, _ = service(settings_values)
    issued = sessions.exchange(sessions.create_bootstrap().token)
    identity = sessions.authenticate_request(request(sessions.settings, issued))
    assert identity.actor_type == "user"
    assert identity.principal_id == settings_values["local_principal_id"]
    assert identity.grant_id == issued.session_id
    reopened = module.LocalSessions(sessions.settings, store, clock=sessions.clock)
    assert reopened.identity_for_grant(issued.session_id) == identity
    assert reopened.authenticate_request(request(sessions.settings, issued)) == identity
    sessions.revoke(issued.session_id)
    assert reopened.identity_for_grant(issued.session_id) is None
    assert reopened.authenticate_request(request(sessions.settings, issued)) is None


@pytest.mark.parametrize("variant", ["expired", "legacy", "foreign", "principal"])
def test_noncurrent_session_metadata_does_not_authenticate(settings_values, variant):
    _, sessions, store, clock = service(settings_values)
    issued = sessions.exchange(sessions.create_bootstrap().token)
    row = store.sessions[issued.session_id]
    if variant == "expired":
        clock[0] = issued.expires_at
    elif variant == "legacy":
        store.sessions[issued.session_id] = replace(row, csrf_digest=None)
    elif variant == "foreign":
        store.sessions[issued.session_id] = replace(row, workspace_id=uuid4())
    else:
        store.sessions[issued.session_id] = replace(row, principal_id="another-user")
    assert sessions.authenticate_request(request(sessions.settings, issued)) is None
    assert sessions.identity_for_grant(issued.session_id) is None


@pytest.mark.parametrize(
    "variant", ["missing", "wrong", "other_session", "origin", "host", "dual"]
)
def test_cookie_write_requires_exact_session_csrf_origin_and_host(
    settings_values, variant
):
    module, sessions, _, _ = service(settings_values)
    issued = sessions.exchange(sessions.create_bootstrap().token)
    other = sessions.exchange(sessions.create_bootstrap().token)
    extras = {}
    if variant == "wrong":
        extras["x-sidera-csrf"] = "WRONG_CSRF_SECRET_SENTINEL"
    elif variant == "other_session":
        extras["x-sidera-csrf"] = other.csrf.get_secret_value()
    elif variant == "origin":
        extras["origin"] = "http://attacker.example"
    elif variant == "host":
        extras["host"] = "attacker.example"
    elif variant == "dual":
        extras["authorization"] = "Bearer FORGED_SECRET_SENTINEL"
    with pytest.raises(module.LocalSessionError) as error:
        sessions.authenticate_request(
            request(
                sessions.settings,
                issued,
                method="POST",
                csrf=variant != "missing",
                **extras,
            )
        )
    assert error.value.status_code == 403
    assert "SECRET_SENTINEL" not in str(error.value)
    assert (
        sessions.authenticate_request(
            request(sessions.settings, issued, method="POST")
        ).grant_id
        == issued.session_id
    )


def test_anonymous_uuid_or_csrf_alone_never_authenticates(settings_values):
    _, sessions, _, _ = service(settings_values)
    for headers in (
        {},
        {"authorization": "Bearer " + str(uuid4())},
        {"cookie": "sidera_csrf=CSRF_ONLY_SENTINEL"},
    ):
        assert (
            sessions.authenticate_request(request(sessions.settings, **headers)) is None
        )


def test_bootstrap_file_is_exclusive_private_and_never_stdout(
    settings_values, tmp_path
):
    module, sessions, _, _ = service(settings_values)
    bootstrap = sessions.create_bootstrap()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "bootstrap.json"
    assert module.write_bootstrap_file(bootstrap, output=output) == output
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {
        "token": bootstrap.token.get_secret_value()
    }
    with pytest.raises(module.LocalSessionError):
        module.write_bootstrap_file(bootstrap, output=output)
    assert json.loads(output.read_text())["token"] == bootstrap.token.get_secret_value()


@pytest.mark.parametrize("variant", ["readable", "symlink", "relative", "repository"])
def test_unsafe_bootstrap_output_is_rejected_without_overwrite(
    settings_values, tmp_path, variant
):
    module, sessions, _, _ = service(settings_values)
    bootstrap = sessions.create_bootstrap()
    directory = tmp_path / "output"
    directory.mkdir(mode=0o700)
    target = directory / "bootstrap.json"
    if variant == "readable":
        directory.chmod(0o755)
    elif variant == "symlink":
        preserved = directory / "preserved"
        preserved.write_text("PRESERVED_FILE")
        target.symlink_to(preserved)
    elif variant == "relative":
        target = Path("relative-bootstrap.json")
    else:
        (directory / ".git").mkdir()
    with pytest.raises(module.LocalSessionError) as error:
        module.write_bootstrap_file(bootstrap, output=target)
    assert error.value.code == "bootstrap_output_unsafe"
    if variant == "symlink":
        assert preserved.read_text() == "PRESERVED_FILE"
    elif variant != "relative":
        assert not target.exists()


def test_local_authority_construction_reuses_engine_without_connecting(
    settings_values, no_database_connections
):
    module = session_module()
    from sidera_product.postgres import PostgresProductStore
    from sidera_product.postgres.authority_store import PostgresAuthorityStore

    product = PostgresProductStore(str(settings_values["product_database_url"]))
    authority = PostgresAuthorityStore(product.engine)
    module.LocalSessions(ProductSettings.model_validate(settings_values), authority)
    assert authority.engine is product.engine
    product.close()


def test_bootstrap_output_rejects_physical_repository_via_intermediate_alias(
    settings_values, tmp_path
):
    module, sessions, _, _ = service(settings_values)
    repository = tmp_path / "synthetic-repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    private = repository / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "outside-alias"
    alias.symlink_to(repository, target_is_directory=True)
    lexical = alias / "private" / "bootstrap.json"
    assert not lexical.parent.is_symlink()
    with pytest.raises(module.LocalSessionError) as error:
        module.write_bootstrap_file(sessions.create_bootstrap(), output=lexical)
    assert error.value.code == "bootstrap_output_unsafe"
    assert not lexical.exists()
    assert not (private / "bootstrap.json").exists()


def test_bootstrap_output_rejects_hidden_physical_repository_ancestor(
    settings_values, tmp_path
):
    module, sessions, _, _ = service(settings_values)
    repository = tmp_path / "synthetic-repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    inside = repository / "inside"
    inside.mkdir()
    private = inside / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "outside-descendant-alias"
    alias.symlink_to(inside, target_is_directory=True)
    lexical = alias / "private" / "bootstrap.json"
    assert not lexical.parent.is_symlink()
    assert not (alias / ".git").exists()
    with pytest.raises(module.LocalSessionError) as error:
        module.write_bootstrap_file(sessions.create_bootstrap(), output=lexical)
    assert error.value.code == "bootstrap_output_unsafe"
    assert not lexical.exists()
    assert not (private / "bootstrap.json").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("variant", ["allowed", "disallowed", "preflight"])
async def test_actual_auth_error_cors_and_preflight_preserve_boundary(
    settings_values, no_database_connections, variant
):
    import httpx

    from sidera_product.app import create_app
    from sidera_product.composition import build_product_runtime

    _, sessions, store, _ = service(settings_values)
    runtime = build_product_runtime(sessions.settings)
    runtime.local_sessions = sessions
    runtime.request_authorizer.local_sessions = sessions
    issue = sessions.exchange(sessions.create_bootstrap().token)
    before = (dict(store.bootstraps), dict(store.sessions))
    origin = (
        "http://attacker.example"
        if variant == "disallowed"
        else sessions.settings.web_origin
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime=runtime)),
            base_url=sessions.settings.local_url,
            headers={"Origin": origin},
            cookies={"sidera_local_session": issue.token.get_secret_value()},
        ) as client:
            if variant == "preflight":
                reply = await client.options(
                    "/api/v1/commands",
                    headers={
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "content-type,x-sidera-csrf",
                    },
                )
                assert reply.status_code == 200
                assert "X-Sidera-CSRF" in reply.headers["access-control-allow-headers"]
            else:
                reply = await client.post("/api/v1/commands", json={"not": "a command"})
                assert reply.status_code == 403
                assert reply.json()["detail"]["code"] == (
                    "origin_denied" if variant == "disallowed" else "csrf_required"
                )
            if variant == "disallowed":
                assert "access-control-allow-origin" not in reply.headers
            else:
                assert reply.headers["access-control-allow-origin"] == origin
                assert reply.headers["access-control-allow-credentials"] == "true"
        assert (store.bootstraps, store.sessions) == before
    finally:
        assert runtime.close(grace_seconds=0) is True


def test_actual_composition_routes_cookie_grant_to_finite_catalog_scope(
    settings_values, no_database_connections, monkeypatch
):
    from sidera_agents.errors import AgentError

    from sidera_product.composition import build_product_runtime

    module, sessions, _, _ = service(settings_values)
    runtime = build_product_runtime(sessions.settings)
    runtime.local_sessions = sessions
    runtime.request_authorizer.local_sessions = sessions
    issue = sessions.exchange(sessions.create_bootstrap().token)
    grant_reads = []

    def find_grant(**selection):
        assert selection == {"grant_id": issue.session_id}
        grant_reads.append(selection)
        return None

    monkeypatch.setattr(runtime.authority_store, "find_grant", find_grant)
    try:
        assert (
            runtime.request_authorizer.authenticate_request(
                request(sessions.settings, issue)
            ).grant_id
            == issue.session_id
        )
        runtime.authority.require(
            issue.session_id, "profile.list", sessions.settings.workspace_id
        )
        with pytest.raises(AgentError, match="permission_denied"):
            runtime.authority.require(issue.session_id, "profile.list", uuid4())
        assert grant_reads == []
        sessions.revoke(issue.session_id)
        with pytest.raises(AgentError, match="permission_denied"):
            runtime.authority.require(
                issue.session_id, "profile.list", sessions.settings.workspace_id
            )
        assert grant_reads == [{"grant_id": issue.session_id}]
    finally:
        assert runtime.close(grace_seconds=0) is True


@pytest.mark.parametrize(
    "variant", ["foreign", "action", "expired", "revoked", "missing", "background"]
)
def test_local_session_router_never_expands_or_revives_catalog_authority(
    settings_values, variant
):
    from sidera_agents.contracts import ProfileRef
    from sidera_agents.errors import AgentError

    from sidera_product.composition import AuthorityRouter, ProductRequestAuthorizer

    _, sessions, _, clock = service(settings_values)
    host = ProductRequestAuthorizer(sessions.settings)
    host.local_sessions = sessions
    router = AuthorityRouter(host)
    issued = sessions.exchange(sessions.create_bootstrap().token)
    ref = ProfileRef(
        workspace_id=sessions.settings.workspace_id, profile_id=uuid4(), revision=1
    )
    router.require(issued.session_id, "profile.read", ref)
    grant, action, target = issued.session_id, "profile.read", ref
    if variant == "foreign":
        target = ref.model_copy(update={"workspace_id": uuid4()})
    elif variant == "action":
        action = "run.start"
    elif variant == "expired":
        clock[0] = issued.expires_at
    elif variant == "revoked":
        sessions.revoke(issued.session_id)
    elif variant == "missing":
        grant = uuid4()
    else:
        grant, action, target = (
            host.background_identity.grant_id,
            "profile.create",
            sessions.settings.workspace_id,
        )
        router.require(grant, "profile.list", target)
    with pytest.raises(AgentError, match="permission_denied"):
        router.require(grant, action, target)


def local_policy_fixture(settings_values, command_type="SendExecutionInput"):
    from sidera_control_plane.application.request_authority import (
        ControlPlaneRequestTarget,
    )
    from sidera_control_plane.domain.commands import CommandEnvelope

    from sidera_product.composition import ProductRequestAuthorizer

    _, sessions, store, clock = service(settings_values)
    host = ProductRequestAuthorizer(sessions.settings)
    host.local_sessions = sessions
    issue = sessions.exchange(sessions.create_bootstrap().token)
    command_id = uuid4()
    command = CommandEnvelope(
        id=command_id,
        workspace_id=sessions.settings.workspace_id,
        command_type=command_type,
        target_type="execution",
        actor_type="user",
        actor_id=sessions.settings.local_principal_id,
        target_id=uuid4(),
        payload={},
        expected_version=1,
        idempotency_key=str(command_id),
        correlation_id=uuid4(),
    )
    target = ControlPlaneRequestTarget(
        sessions.settings.workspace_id,
        sessions.settings.local_principal_id,
        "POST",
        "/api/v1/commands",
        (),
        (),
        command,
    )
    return host, sessions, store, clock, issue, target


@pytest.mark.parametrize(
    "command",
    [
        "SendExecutionInput",
        "UpdatePendingExecutionInput",
        "CancelExecutionInput",
        "RetryBlockedExecutionInput",
        "PrepareProfileHandoff",
        "CancelWorkerHandoff",
        "RecoverWorkerHandoff",
        "RetryBlockedPlanningRun",
        "ConfirmProfileHandoff",
    ],
)
def test_local_policy_matches_exact_existing_command_names(settings_values, command):
    host, _, _, _, issue, target = local_policy_fixture(settings_values, command)
    host.require(issue.session_id, "control_plane.command:" + command, target)


def test_local_policy_planning_write_requires_the_exact_typed_target(settings_values):
    from sidera_agents.contracts import ProfileRef
    from sidera_agents.errors import AgentError
    from sidera_control_plane.application.planning_commands import PlanningCommandTarget

    host, sessions, _, _, issue, _ = local_policy_fixture(settings_values)
    target = PlanningCommandTarget(
        sessions.settings.workspace_id,
        sessions.settings.local_principal_id,
        uuid4(),
        uuid4(),
        1,
        ProfileRef(
            workspace_id=sessions.settings.workspace_id, profile_id=uuid4(), revision=1
        ),
        None,
        (),
    )
    host.require(issue.session_id, "planning.write", target)
    for invalid in (
        sessions.settings.workspace_id,
        replace(target, workspace_id=uuid4()),
        replace(target, principal_id="forged-user"),
        replace(
            target,
            planner_agent_ref=target.planner_agent_ref.model_copy(
                update={"workspace_id": uuid4()}
            ),
        ),
    ):
        with pytest.raises(AgentError, match="permission_denied"):
            host.require(issue.session_id, "planning.write", invalid)


@pytest.mark.parametrize(
    "variant",
    [
        "unknown",
        "wrong_action",
        "foreign",
        "command_workspace",
        "principal",
        "actor_id",
        "agent_label",
        "revoked",
        "expired",
        "missing",
        "system",
        "agent_identity",
    ],
)
def test_local_policy_never_converts_untrusted_scope_into_user_authority(
    settings_values, variant
):
    from sidera_agents.errors import AgentError
    from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

    host, sessions, _, clock, issue, target = local_policy_fixture(settings_values)
    grant, action = issue.session_id, "control_plane.command:SendExecutionInput"
    if variant == "unknown":
        target = replace(
            target, command=replace(target.command, command_type="UnknownCommand")
        )
        action = "control_plane.command:UnknownCommand"
    elif variant == "wrong_action":
        action = "control_plane.command:CancelExecutionInput"
    elif variant == "foreign":
        target = replace(target, workspace_id=uuid4())
    elif variant == "command_workspace":
        target = replace(target, command=replace(target.command, workspace_id=uuid4()))
    elif variant == "principal":
        target = replace(target, principal_id="forged-user")
    elif variant == "actor_id":
        target = replace(
            target, command=replace(target.command, actor_id="forged-user")
        )
    elif variant == "agent_label":
        target = replace(target, command=replace(target.command, actor_type="agent"))
    elif variant == "revoked":
        sessions.revoke(issue.session_id)
    elif variant == "expired":
        clock[0] = issue.expires_at
    elif variant == "missing":
        grant = uuid4()
    elif variant == "system":
        grant = host.background_identity.grant_id
    else:
        identity = ControlPlaneIdentity(
            sessions.settings.local_principal_id, issue.session_id, "agent"
        )
        with pytest.raises(AgentError, match="permission_denied"):
            host._require_local_user(identity, action, target)
        return
    with pytest.raises(AgentError, match="permission_denied"):
        host.require(grant, action, target)


def test_cli_bootstrap_writes_private_file_without_native_owner(
    settings_values, tmp_path, monkeypatch, no_database_connections, capsys
):
    from sidera_product import cli
    from sidera_product.postgres import PostgresProductStore
    from sidera_product.postgres.authority_store import PostgresAuthorityStore

    settings = ProductSettings.model_validate(settings_values)
    directory = tmp_path / "private-cli"
    directory.mkdir(mode=0o700)
    output = directory / "bootstrap.json"
    saved = []
    closed = []
    original_close = PostgresProductStore.close

    def close(store):
        closed.append(store)
        original_close(store)

    monkeypatch.setattr(
        cli,
        "build_product_runtime",
        lambda *_: pytest.fail("Bootstrap constructed native owners"),
    )
    monkeypatch.setattr(PostgresProductStore, "readiness", lambda _: True)
    monkeypatch.setattr(PostgresProductStore, "close", close)
    monkeypatch.setattr(
        PostgresAuthorityStore, "save_bootstrap", lambda _, record: saved.append(record)
    )
    assert (
        cli.main(["session", "bootstrap", "--output", str(output)], settings=settings)
        == 0
    )
    raw = json.loads(output.read_text())["token"]
    stdout = capsys.readouterr().out
    assert raw not in stdout
    assert raw not in repr(saved)
    assert len(saved) == len(closed) == 1
    assert saved[0].token_digest == hashlib.sha256(raw.encode()).hexdigest()
    assert json.loads(stdout)["output"] == str(output)
    assert output.stat().st_mode & 0o777 == 0o600


def test_cli_bootstrap_requires_schema_readiness_without_implicit_migration(
    settings_values, tmp_path, monkeypatch, no_database_connections, capsys
):
    from sidera_product import cli
    from sidera_product.postgres import PostgresProductStore
    from sidera_product.postgres.authority_store import PostgresAuthorityStore

    monkeypatch.setattr(PostgresProductStore, "readiness", lambda _: False)
    monkeypatch.setattr(
        PostgresProductStore,
        "migrate",
        lambda _: pytest.fail("Bootstrap migrated implicitly"),
    )
    monkeypatch.setattr(
        PostgresAuthorityStore,
        "save_bootstrap",
        lambda *_: pytest.fail("Unready schema wrote authority"),
    )
    assert (
        cli.main(
            ["session", "bootstrap", "--output", str(tmp_path / "unused")],
            settings=ProductSettings.model_validate(settings_values),
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "schema_not_ready"


@pytest.mark.parametrize("outcome", ["applied", "consumed", "missing_csrf"])
def test_cli_workspace_uses_live_bootstrap_cookie_and_exact_setup_codecs(
    settings_values, tmp_path, monkeypatch, no_database_connections, capsys, outcome
):
    import httpx

    from sidera_product import cli

    module, sessions, _, _ = service(settings_values)
    private = tmp_path / "live-cli-private"
    private.mkdir(mode=0o700)
    issue = sessions.create_bootstrap()
    bootstrap_file = module.write_bootstrap_file(
        issue, output=private / "bootstrap.json"
    )
    requests = []
    profile = {
        "workspace_id": str(sessions.settings.workspace_id),
        "profile_id": str(uuid4()),
        "revision": 3,
    }
    checks = [
        {
            "key": "owned-check",
            "version": 1,
            "argv": ["explicit-test-check"],
            "cwd": ".",
            "environment_ref": "controlled",
            "prepare_argvs": [],
            "timeout_seconds": 10,
            "success_exit_codes": [0],
            "report_paths": [],
            "required": True,
        }
    ]

    def handle(request):
        requests.append(request)
        assert request.headers["Origin"] == sessions.settings.web_origin
        if request.url.path == "/product/v1/session/bootstrap":
            assert json.loads(request.content) == {
                "token": issue.token.get_secret_value()
            }
            if outcome == "consumed":
                return httpx.Response(
                    401, json={"detail": {"code": "bootstrap_consumed"}}
                )
            headers = [
                (
                    "Set-Cookie",
                    "sidera_local_session=LIVE_SESSION_SENTINEL; HttpOnly; Path=/; SameSite=Strict",
                )
            ]
            if outcome != "missing_csrf":
                headers.append(
                    (
                        "Set-Cookie",
                        "sidera_csrf=LIVE_CSRF_SENTINEL; Path=/; SameSite=Strict",
                    )
                )
            return httpx.Response(
                200, json={"session_id": str(uuid4())}, headers=headers
            )
        assert (
            request.url.path
            == f"/product/v1/workspaces/{sessions.settings.workspace_id}/setup"
        )
        assert request.headers["X-Sidera-CSRF"] == "LIVE_CSRF_SENTINEL"
        assert "sidera_local_session=LIVE_SESSION_SENTINEL" in request.headers["cookie"]
        body = json.loads(request.content)
        assert body["planner_agent_ref"] == profile
        assert body["verification_checks"] == checks
        assert body["expected_version"] == 0
        return httpx.Response(
            200,
            json={
                "workspace_id": str(sessions.settings.workspace_id),
                "version": 1,
                "status": "created",
            },
        )

    real_client = httpx.Client

    def client(**options):
        assert options["trust_env"] is False
        return real_client(**options, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(
        cli,
        "build_product_runtime",
        lambda *_: pytest.fail("Live setup constructed second native owner"),
    )
    result = cli.main(
        [
            "workspace",
            "configure",
            "--workspace",
            str(sessions.settings.workspace_id),
            "--repository",
            str(tmp_path / "repository"),
            "--planner-profile",
            json.dumps(profile),
            "--verification-checks",
            json.dumps(checks),
            "--bootstrap-file",
            str(bootstrap_file),
        ],
        settings=sessions.settings,
    )
    assert result == (0 if outcome == "applied" else 1)
    assert len(requests) == (2 if outcome == "applied" else 1)
    output = capsys.readouterr()
    assert issue.token.get_secret_value() not in output.out + output.err
    assert "SENTINEL" not in output.out + output.err
    if outcome != "applied":
        assert json.loads(output.err)["error"]["code"] == (
            "bootstrap_consumed" if outcome == "consumed" else "local_session_required"
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "variant",
    [
        "empty",
        "extra",
        "number",
        "long_token",
        "oversize",
        "invalid_utf8",
        "host",
        "origin",
    ],
)
async def test_actual_bootstrap_http_bounds_and_origin_gate_precede_store_write(
    settings_values, no_database_connections, variant
):
    import httpx

    from sidera_product.app import create_app
    from sidera_product.composition import build_product_runtime

    _, sessions, store, _ = service(settings_values)
    runtime = build_product_runtime(sessions.settings)
    runtime.local_sessions = sessions
    runtime.request_authorizer.local_sessions = sessions
    headers = {"Origin": sessions.settings.web_origin}
    body = b'{"token":"BOOTSTRAP_HTTP_SECRET_SENTINEL"}'
    if variant == "empty":
        body = b'{"token":""}'
    elif variant == "extra":
        body = b'{"token":"BOOTSTRAP_HTTP_SECRET_SENTINEL","actor":"user"}'
    elif variant == "number":
        body = b'{"token":123}'
    elif variant == "long_token":
        body = json.dumps({"token": "x" * 1025}).encode()
    elif variant == "oversize":
        body = b" " * 4097
    elif variant == "invalid_utf8":
        body = b"\xff"
    elif variant == "host":
        headers["Host"] = "attacker.example"
    else:
        headers["Origin"] = "http://attacker.example"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime=runtime)),
            base_url=sessions.settings.local_url,
        ) as client:
            reply = await client.post(
                "/product/v1/session/bootstrap", content=body, headers=headers
            )
            assert reply.status_code == (403 if variant in {"host", "origin"} else 422)
            assert "SECRET_SENTINEL" not in reply.text
            assert "set-cookie" not in reply.headers
        assert store.sessions == store.bootstraps == {}
    finally:
        assert runtime.close(grace_seconds=0) is True


@pytest.mark.parametrize(
    "field,valid,invalid",
    [
        ("bootstrap_ttl_seconds", "300", [0, 3601, True]),
        ("local_session_ttl_seconds", "43200", [0, 604801, True]),
        ("tool_grant_ttl_seconds", "86400", [0, 604801, True]),
    ],
)
def test_session_ttls_use_explicit_json_scalars_and_strict_bounds(
    settings_values, field, valid, invalid, monkeypatch
):
    monkeypatch.setenv("SIDERA_PRODUCT_" + field.upper(), valid)
    assert getattr(ProductSettings.model_validate(settings_values), field) == int(valid)
    for value in invalid:
        with pytest.raises(ValidationError):
            ProductSettings.model_validate({**settings_values, field: value})
