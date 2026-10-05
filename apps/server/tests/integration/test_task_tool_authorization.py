import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from uuid import UUID, uuid4

import httpx
import pytest
from sidera_agents.contracts import ProfileDraft
from sidera_agents.errors import AgentError
from sidera_control_plane.domain.result_integration import CheckSpecV1

from sidera_product.app import create_app
from tests.integration.test_product_lifecycle import repository
from tests.support.product_lab import CONTROLLED_PLANNING_BUDGET, write_evidence

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def goal_envelope(lab, *, actor_type="user", actor_id=None):
    workspace = lab.runtime.settings.workspace_id
    command = uuid4()
    return {
        "command_id": str(command),
        "command_type": "SubmitTaskGoal",
        "workspace_id": str(workspace),
        "actor": {
            "type": actor_type,
            "id": actor_id or lab.runtime.settings.local_principal_id,
        },
        "target_type": "workspace",
        "target_id": None,
        "expected_version": None,
        "idempotency_key": str(command),
        "correlation_id": str(uuid4()),
        "payload": {
            "title": "Owned authorization boundary",
            "objective": "No command may bypass the current session.",
            "constraints": [],
            "acceptance_criteria": ["Authorization runs before command mutation."],
            "requested_outputs": ["An authorized real receipt"],
            "context_refs": [],
            "planning_profile_id": str(uuid4()),
            "planning_profile_version": 1,
            "planner_agent_ref": {
                "workspace_id": str(workspace),
                "profile_id": str(uuid4()),
                "revision": 1,
            },
        },
    }


async def test_actual_atomic_bootstrap_has_one_winner_and_reopens(product_lab):
    lab = product_lab
    sessions = lab.runtime.local_sessions
    bootstrap = sessions.create_bootstrap()

    def exchange():
        from sidera_product.auth.local_session import LocalSessionError

        try:
            return sessions.exchange(bootstrap.token)
        except LocalSessionError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(exchange) for _ in range(2)]
        outcomes = [future.result(timeout=5) for future in futures]
    successful = [outcome for outcome in outcomes if not isinstance(outcome, str)]
    assert len(successful) == 1
    assert [outcome for outcome in outcomes if isinstance(outcome, str)] == [
        "bootstrap_consumed"
    ]
    issued = successful[0]
    from sidera_product.auth.local_session import LocalSessions

    reopened = LocalSessions(lab.runtime.settings, lab.runtime.authority_store)
    identity = reopened.identity_for_grant(issued.session_id)
    assert identity.grant_id == issued.session_id
    assert identity.actor_type == "user"
    assert identity.principal_id == lab.runtime.settings.local_principal_id
    reopened.revoke(issued.session_id)
    assert sessions.identity_for_grant(issued.session_id) is None
    assert not lab.native.calls
    assert not lab.secrets.reads


async def test_actual_cookie_configures_exact_public_setup_without_native_execution(
    product_lab,
):
    lab = product_lab
    app = create_app(runtime=lab.runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=lab.runtime.settings.local_url,
        headers={"Origin": lab.runtime.settings.web_origin},
    ) as client:
        bootstrap = lab.runtime.local_sessions.create_bootstrap()
        exchanged = await client.post(
            "/product/v1/session/bootstrap",
            json={"token": bootstrap.token.get_secret_value()},
        )
        assert exchanged.status_code == 200
        grant = UUID((await client.get("/product/v1/session")).json()["session_id"])
        selected_runtime = lab.runtime.agents.runtimes.register(
            lab.principal.workspace,
            lab.native.harness_id,
            sys.executable,
            max_active=2,
            max_sessions=2,
            grant_id=grant,
        )
        profile = lab.runtime.agents.catalog.create_profile(
            lab.principal.workspace,
            ProfileDraft(
                name="Cookie-authorized Planner",
                description="Owned N18 setup fixture",
                runtime_ref=selected_runtime.ref,
                instructions="Only explicit controlled work.",
                resource_refs=(),
                model_id=None,
                thinking=None,
                max_active=2,
                environment={},
                secret_refs=(),
                native_settings={},
            ),
            grant_id=grant,
        )
        assert (
            lab.runtime.agents.catalog.get_profile(profile.ref, grant_id=grant)
            == profile
        )
        repo = repository(lab.principal.repository)
        check = CheckSpecV1.from_mapping(
            {
                "key": "controlled",
                "version": 1,
                "argv": ["explicit-owned-check"],
                "cwd": ".",
                "environment_ref": "controlled",
                "prepare_argvs": [],
                "timeout_seconds": 10,
                "success_exit_codes": [0],
                "report_paths": [],
                "required": True,
            }
        )
        body = {
            "repository_directory": str(repo),
            "expected_version": 0,
            "planner_agent_ref": profile.ref.model_dump(mode="json"),
            "verification_checks": [check.to_mapping()],
            "planning_budget": asdict(CONTROLLED_PLANNING_BUDGET),
        }
        headers = {"X-Sidera-CSRF": client.cookies["sidera_csrf"]}
        denied = await client.post(
            f"/product/v1/workspaces/{uuid4()}/setup", json=body, headers=headers
        )
        assert denied.status_code == 403
        reply = await client.post(
            f"/product/v1/workspaces/{lab.principal.workspace}/setup",
            json=body,
            headers=headers,
        )
        assert reply.status_code == 200, reply.json()
        saved = reply.json()
        assert saved["workspace_id"] == str(lab.principal.workspace)
        assert saved["version"] == 1 and saved["status"] == "created"
        unchanged = await client.post(
            f"/product/v1/workspaces/{lab.principal.workspace}/setup",
            json={**body, "expected_version": 1},
            headers=headers,
        )
        assert unchanged.status_code == 200, unchanged.json()
        assert unchanged.json()["status"] == "unchanged"
        assert unchanged.json()["planning_policy_ref"] == saved["planning_policy_ref"]
        write_evidence(
            lab.evidence,
            "cookie-authorized-public-setup",
            {
                "workspace_id": str(lab.principal.workspace),
                "session_id": str(grant),
                "planner_ref": profile.ref.model_dump(mode="json"),
                "setup": saved,
                "foreign_status": denied.status_code,
                "native_counts": {
                    name: sum(call[0] == name for call in lab.native.calls)
                    for name in ("probe", "open", "prompt", "close")
                },
            },
        )
    assert [call[0] for call in lab.native.calls] == ["probe"]
    assert not lab.secrets.reads


async def test_cookie_csrf_and_forged_actor_gate_actual_mounted_cp(product_lab):
    lab = product_lab
    app = create_app(runtime=lab.runtime)
    origin = lab.runtime.settings.web_origin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=lab.runtime.settings.local_url,
        headers={"Origin": origin},
    ) as client:
        anonymous = await client.get(
            "/api/v1/tasks", params={"workspace_id": str(lab.principal.workspace)}
        )
        assert anonymous.status_code == 401
        bootstrap = lab.runtime.local_sessions.create_bootstrap()
        reply = await client.post(
            "/product/v1/session/bootstrap",
            json={"token": bootstrap.token.get_secret_value()},
        )
        assert reply.status_code == 200
        for name in ("sidera_local_session", "sidera_csrf"):
            cookie = next(
                cookie for cookie in client.cookies.jar if cookie.name == name
            )
            assert cookie.domain_specified is False
            assert cookie.path == "/"
            assert cookie.secure is False
        cookies = reply.headers.get_list("set-cookie")
        assert "HttpOnly" in next(
            value for value in cookies if value.startswith("sidera_local_session=")
        )
        assert all("SameSite=strict" in value for value in cookies)
        before = await client.get(
            "/api/v1/tasks", params={"workspace_id": str(lab.principal.workspace)}
        )
        assert before.status_code == 200
        denied = await client.post("/api/v1/commands", json=goal_envelope(lab))
        assert denied.status_code == 403
        assert json.loads(denied.content)["detail"]["code"] == "csrf_required"
        csrf = client.cookies["sidera_csrf"]
        forged = await client.post(
            "/api/v1/commands",
            json=goal_envelope(lab, actor_type="agent"),
            headers={"X-Sidera-CSRF": csrf},
        )
        assert forged.status_code == 403
        after = await client.get(
            "/api/v1/tasks", params={"workspace_id": str(lab.principal.workspace)}
        )
        assert after.json() == before.json()
        session_reply = await client.get("/product/v1/session")
        assert session_reply.status_code == 200
        cookie_grant = UUID(session_reply.json()["session_id"])
        assert (
            lab.runtime.agents.catalog.list_profiles(
                lab.principal.workspace, grant_id=cookie_grant
            )
            == ()
        )
        with pytest.raises(AgentError, match="permission_denied"):
            lab.runtime.agents.catalog.list_profiles(uuid4(), grant_id=cookie_grant)
        with pytest.raises(AgentError, match="permission_denied"):
            lab.runtime.authority.require(
                cookie_grant, "run.start", lab.principal.workspace
            )
        logout = await client.post(
            "/product/v1/session/logout", headers={"X-Sidera-CSRF": csrf}
        )
        assert logout.status_code == 200
        assert (await client.get("/product/v1/session")).status_code == 401
    assert not lab.native.calls
    assert not lab.secrets.reads


async def test_cookie_reads_exact_public_setup_without_repository_io(
    product_lab, monkeypatch
):
    lab = product_lab
    app = create_app(runtime=lab.runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=lab.runtime.settings.local_url,
        headers={"Origin": lab.runtime.settings.web_origin},
    ) as client:
        issue = lab.runtime.local_sessions.create_bootstrap()
        assert (
            await client.post(
                "/product/v1/session/bootstrap",
                json={"token": issue.token.get_secret_value()},
            )
        ).status_code == 200
        path = f"/product/v1/workspaces/{lab.principal.workspace}/setup"
        empty = await client.get(path)
        assert empty.status_code == 200 and empty.json() is None
        selected = lab.register_profile()
        repo = repository(lab.principal.repository)
        grant = UUID((await client.get("/product/v1/session")).json()["session_id"])
        from sidera_control_plane.composition.native_runtime import (
            WorkspaceSetupRequest,
        )

        check = CheckSpecV1.from_mapping(
            {
                "key": "owned-read",
                "version": 1,
                "argv": ["explicit-owned-check"],
                "cwd": ".",
                "environment_ref": "controlled",
                "prepare_argvs": [],
                "timeout_seconds": 10,
                "success_exit_codes": [0],
                "report_paths": [],
                "required": True,
            }
        )
        receipt = lab.runtime.control_plane.configure_workspace(
            WorkspaceSetupRequest(
                lab.principal.workspace,
                repo,
                0,
                CONTROLLED_PLANNING_BUDGET,
                selected.ref,
                verification_checks=(check,),
            ),
            grant_id=grant,
        )

        def forbidden(*args, **kwargs):
            pytest.fail("Saved setup read called Git or repository preparation")

        monkeypatch.setattr(
            "sidera_control_plane.composition.native_setup.subprocess.run", forbidden
        )
        read = await client.get(path)
        assert read.status_code == 200, read.json()
        body = read.json()
        assert body["workspace_id"] == str(lab.principal.workspace)
        assert body["version"] == receipt.version
        assert body["repository_ref"] == receipt.repository_ref
        assert body["planning_policy_ref"] == {
            "policy_id": str(receipt.planning_policy_ref.policy_id),
            "version": receipt.planning_policy_ref.version,
        }
        assert body["planner_agent_ref"] == selected.ref.model_dump(mode="json")
        assert (
            await client.get(f"/product/v1/workspaces/{uuid4()}/setup")
        ).status_code == 403
        write_evidence(lab.evidence, "cookie-public-setup-read", body)
    assert all(call[0] == "probe" for call in lab.native.calls)


async def test_cookie_cancels_new_queued_request_with_exact_selected_planner(
    product_lab,
):
    lab = product_lab
    app = create_app(runtime=lab.runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=lab.runtime.settings.local_url,
        headers={"Origin": lab.runtime.settings.web_origin},
    ) as client:
        issue = lab.runtime.local_sessions.create_bootstrap()
        exchange = await client.post(
            "/product/v1/session/bootstrap",
            json={"token": issue.token.get_secret_value()},
        )
        assert exchange.status_code == 200
        grant = UUID(exchange.json()["session_id"])
        selected_runtime = lab.runtime.agents.runtimes.register(
            lab.principal.workspace,
            lab.native.harness_id,
            sys.executable,
            max_active=1,
            max_sessions=2,
            grant_id=grant,
        )
        profiles = tuple(
            lab.runtime.agents.catalog.create_profile(
                lab.principal.workspace,
                ProfileDraft(
                    name=name,
                    description="New queued N18 policy selection",
                    runtime_ref=selected_runtime.ref,
                    instructions="Controlled explicit requests only.",
                    resource_refs=(),
                    model_id=None,
                    thinking=None,
                    max_active=1,
                    environment={},
                    secret_refs=(),
                    native_settings={},
                ),
                grant_id=grant,
            )
            for name in ("Saved policy A", "Explicit selected B")
        )
        repo = repository(lab.principal.repository)
        check = CheckSpecV1.from_mapping(
            {
                "key": "queued-selection",
                "version": 1,
                "argv": ["explicit-owned-check"],
                "cwd": ".",
                "environment_ref": "controlled",
                "prepare_argvs": [],
                "timeout_seconds": 10,
                "success_exit_codes": [0],
                "report_paths": [],
                "required": True,
            }
        )
        csrf = {"X-Sidera-CSRF": client.cookies["sidera_csrf"]}
        path = f"/product/v1/workspaces/{lab.principal.workspace}/setup"
        configured = await client.post(
            path,
            headers=csrf,
            json={
                "repository_directory": str(repo),
                "expected_version": 0,
                "planner_agent_ref": profiles[0].ref.model_dump(mode="json"),
                "verification_checks": [check.to_mapping()],
                "planning_budget": asdict(CONTROLLED_PLANNING_BUDGET),
            },
        )
        assert configured.status_code == 200, configured.json()
        setup = (await client.get(path)).json()
        envelope = goal_envelope(lab)
        envelope["payload"].update(
            {
                "planning_profile_id": setup["planning_policy_ref"]["policy_id"],
                "planning_profile_version": setup["planning_policy_ref"]["version"],
                "planner_agent_ref": profiles[1].ref.model_dump(mode="json"),
            }
        )
        submitted = await client.post("/api/v1/commands", json=envelope, headers=csrf)
        assert submitted.status_code in {200, 201}, submitted.json()
        goal = submitted.json()
        request_id = goal["data"]["planning_request_id"]
        prefix = f"/api/v1/workspaces/{lab.principal.workspace}"
        detail_path = f"{prefix}/planning-requests/{request_id}"
        before = (await client.get(detail_path)).json()
        runs = (await client.get(detail_path + "/runs")).json()["items"]
        write_evidence(
            lab.evidence,
            "cookie-new-queued-before-cancel",
            {
                "goal": goal,
                "original_request": before,
                "initial_runs": runs,
                "setup": setup,
            },
        )
        assert before["status"] == "submitted" and len(runs) == 1
        assert runs[0]["status"] == "queued"
        assert runs[0]["planner_agent_ref"] == profiles[1].ref.model_dump(mode="json")
        assert before["profile"]["planner_agent_ref"] == profiles[0].ref.model_dump(
            mode="json"
        )
        assert (await client.get(path)).json() == setup
        assert setup["planner_agent_ref"] == profiles[0].ref.model_dump(mode="json")
        command_id = str(uuid4())
        cancellation = {
            "command_id": command_id,
            "command_type": "CancelPlanningRequest",
            "workspace_id": str(lab.principal.workspace),
            "target_type": "planning_request",
            "target_id": request_id,
            "expected_version": before["version"],
            "actor": {"type": "user", "id": lab.runtime.settings.local_principal_id},
            "idempotency_key": command_id,
            "correlation_id": command_id,
            "payload": {},
        }
        denied = await client.post(
            "/api/v1/commands", json=cancellation, headers={"X-Sidera-CSRF": "wrong"}
        )
        assert denied.status_code == 403
        forged = await client.post(
            "/api/v1/commands",
            json={
                **cancellation,
                "actor": {
                    "type": "agent",
                    "id": lab.runtime.settings.local_principal_id,
                },
            },
            headers=csrf,
        )
        assert forged.status_code == 403
        assert (await client.get(detail_path)).json() == before
        reply = await client.post("/api/v1/commands", json=cancellation, headers=csrf)
        assert reply.status_code == 200, reply.json()
        after = (await client.get(detail_path)).json()
        assert (
            after["status"] == "cancelled" and after["version"] == before["version"] + 1
        )
        for invalid_headers, invalid_body in (
            ({"X-Sidera-CSRF": "wrong"}, cancellation),
            (
                csrf,
                {
                    **cancellation,
                    "actor": {
                        "type": "agent",
                        "id": lab.runtime.settings.local_principal_id,
                    },
                },
            ),
        ):
            assert (
                await client.post(
                    "/api/v1/commands", json=invalid_body, headers=invalid_headers
                )
            ).status_code == 403
        from datetime import timedelta

        lab.runtime.local_sessions.clock = lambda: (
            issue.expires_at
            + timedelta(seconds=lab.runtime.settings.local_session_ttl_seconds)
        )
        assert (
            await client.post("/api/v1/commands", json=cancellation, headers=csrf)
        ).status_code == 401
        write_evidence(
            lab.evidence,
            "cookie-new-queued-cancellation",
            {
                "workspace_id": str(lab.principal.workspace),
                "goal": goal,
                "original_request": before,
                "initial_run": runs[0],
                "cancel_receipt": reply.json(),
                "cancelled_request": after,
                "policy_unchanged": (
                    setup["planner_agent_ref"]
                    == profiles[0].ref.model_dump(mode="json")
                ),
                "native_calls": [call[0] for call in lab.native.calls],
            },
        )
    assert [call[0] for call in lab.native.calls] == ["probe"]
    assert not lab.secrets.reads
