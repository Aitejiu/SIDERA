import json
import subprocess
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from sidera_agents import bootstrap
from sidera_agents.contracts import RunState, StartStatus
from sidera_control_plane.composition.native_runtime import WorkspaceSetupRequest
from sidera_control_plane.domain.result_integration import CheckSpecV1

from sidera_product.app import create_app
from sidera_product.cli import main
from sidera_product.postgres import ProductStoreError
from tests.support.product_lab import CONTROLLED_PLANNING_BUDGET, write_evidence

pytestmark = pytest.mark.integration


def repository(path: Path):
    path.mkdir(mode=0o700)
    for argv in (
        ("git", "init", "-q", str(path)),
        (
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Controlled Product",
            "-c",
            "user.email=controlled@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Owned product fixture",
        ),
    ):
        subprocess.run(argv, check=True, capture_output=True, timeout=5)
    return path


def setup_planner(lab):
    profile = lab.register_profile()
    repo = repository(lab.principal.repository)
    checks = (
        CheckSpecV1.from_mapping(
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
        ),
    )
    request = WorkspaceSetupRequest(
        workspace_id=lab.principal.workspace,
        repository_directory=repo,
        expected_version=0,
        planner_agent_ref=profile.ref,
        verification_checks=checks,
        planning_budget=CONTROLLED_PLANNING_BUDGET,
    )
    receipt = lab.runtime.control_plane.configure_workspace(
        request, grant_id=lab.principal.identity.grant_id
    )
    lab.principal.planning_selection = (
        receipt.planning_policy_ref.policy_id,
        receipt.planning_policy_ref.version,
        profile.ref,
    )
    return profile, receipt


def original_run_completed(lab, run_id):
    deadline = monotonic() + 5
    condition = Event()
    while True:
        run = lab.runtime.execution.get_run(
            lab.principal.workspace, run_id, grant_id=lab.principal.identity.grant_id
        )
        if run.state is RunState.COMPLETED:
            return run
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise AssertionError("Original successful bounded Run was not committed")
        condition.wait(min(0.01, remaining))


async def submit_goal(client, lab, profile, setup):
    command_id = str(uuid4())
    response = await client.post(
        "/api/v1/commands",
        headers={"X-Sidera-CSRF": client.cookies["sidera_csrf"]},
        json={
            "command_id": command_id,
            "workspace_id": str(lab.principal.workspace),
            "command_type": "SubmitTaskGoal",
            "target_type": "workspace",
            "target_id": None,
            "expected_version": None,
            "actor": {"type": "user", "id": lab.principal.identity.principal_id},
            "idempotency_key": command_id,
            "correlation_id": command_id,
            "payload": {
                "title": "Owned lifecycle observation",
                "objective": "Exercise only controlled native shutdown",
                "acceptance_criteria": ["owned lifecycle"],
                "requested_outputs": ["diff"],
                "context_refs": [],
                "planning_profile_id": str(setup.planning_policy_ref.policy_id),
                "planning_profile_version": setup.planning_policy_ref.version,
                "planner_agent_ref": profile.ref.model_dump(mode="json"),
            },
        },
    )
    assert response.status_code in {200, 201}, response.json()
    return response.json()


async def capture_original_planning(
    lab, app, goal, receipts, knowledge, *, browser_cookies
):
    request_id = UUID(goal["data"]["planning_request_id"])
    prefix = f"/api/v1/workspaces/{lab.principal.workspace}"
    evidence = {
        "workspace_id": str(lab.principal.workspace),
        "request_id": str(request_id),
        "goal_receipt": goal,
        "start_receipts": receipts,
        "native_call_counts": {
            name: sum(call[0] == name for call in lab.native.calls)
            for name in ("probe", "open", "prompt", "cancel", "close")
        },
        "knowledge": knowledge.facts(),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=lab.runtime.settings.local_url,
        headers={"Origin": lab.runtime.settings.web_origin},
        cookies=browser_cookies,
    ) as client:
        detail = await client.get(f"{prefix}/planning-requests/{request_id}")
        runs = await client.get(f"{prefix}/planning-requests/{request_id}/runs")
        evidence["request"] = {
            "http_status": detail.status_code,
            "data": {
                key: value
                for key, value in detail.json().items()
                if key
                in {"id", "workspace_id", "status", "version", "active_run", "error"}
            },
        }
        evidence["runs"] = {"http_status": runs.status_code, "data": runs.json()}
        evidence["packages"] = []
        if runs.status_code == 200:
            for run in runs.json()["items"]:
                package = await client.get(
                    f"{prefix}/planning-runs/{run['id']}/context-package"
                )
                evidence["packages"].append(
                    {
                        "http_status": package.status_code,
                        "data": {
                            key: value
                            for key, value in package.json().items()
                            if key
                            in {
                                "package_id",
                                "workspace_id",
                                "consumer_id",
                                "status",
                                "assembly",
                                "budget",
                                "knowledge_refs",
                                "package_hash",
                                "work_context_revision_id",
                                "work_context_content_hash",
                                "materialized_at",
                                "error",
                            }
                        },
                    }
                )
    write_evidence(lab.evidence, "planner-external-prerequisite-observation", evidence)
    return evidence


def test_empty_migrated_install_has_real_owners_no_profile_or_native_seed(
    product_lab, monkeypatch
):
    lab = product_lab
    starts = []
    real_start = lab.runtime.control_plane.start_background

    def start(*, background_identity):
        starts.append(background_identity)
        return real_start(background_identity=background_identity)

    monkeypatch.setattr(lab.runtime.control_plane, "start_background", start)
    lab.runtime.start()
    lab.runtime.start()
    assert starts == [lab.runtime.background_identity]
    assert starts[0] is lab.runtime.background_identity
    assert lab.runtime.agents.readiness(require_execution_owner=True) is True
    assert all(lab.runtime.inspect_readiness().values())
    assert (
        lab.runtime.agents.catalog.list_profiles(
            lab.principal.workspace, grant_id=lab.principal.identity.grant_id
        )
        == ()
    )
    assert lab.runtime.status()["execution"]["ready"] is False
    assert lab.native.calls == [] and lab.secrets.reads == []
    assert lab.runtime.close() is True
    assert lab.runtime.close() is True


def test_defaults_cas_stores_profile_identity_not_revision_or_run(product_lab):
    store, workspace = (
        product_lab.runtime.product_store,
        product_lab.principal.workspace,
    )
    profile_id = uuid4()
    assert store.get_defaults(workspace) is None
    created = store.write_defaults(
        workspace,
        expected_version=0,
        main_profile_id=profile_id,
        planner_profile_id=None,
        worker_profile_id=None,
    )
    assert created.version == 1 and created.main_profile_id == profile_id
    with pytest.raises(ProductStoreError, match="version_conflict"):
        store.write_defaults(
            workspace,
            expected_version=0,
            main_profile_id=None,
            planner_profile_id=None,
            worker_profile_id=None,
        )
    changed = store.write_defaults(
        workspace,
        expected_version=1,
        main_profile_id=None,
        planner_profile_id=profile_id,
        worker_profile_id=None,
    )
    assert changed.version == 2 and changed.planner_profile_id == profile_id
    assert store.get_defaults(workspace) == changed


def test_held_agents_owner_startup_rolls_back_without_releasing_foreign_owner(
    product_lab,
):
    lab = product_lab
    foreign = bootstrap.open_postgres_store(
        lab.runtime.settings.agent_database_url.get_secret_value()
    )
    owner_id = uuid4()
    try:
        assert foreign.acquire_execution_owner(owner_id) is True
        with pytest.raises(RuntimeError, match="product_startup_failed"):
            lab.runtime.start()
        assert foreign.execution_owner_is_held(owner_id) is True
        assert lab.runtime.status()["lifecycle"] == "closed"
        assert lab.native.calls == []
    finally:
        foreign.release_execution_owner(owner_id)
        foreign.close()


def test_lost_owner_readiness_uses_actual_public_owner_fact(product_lab, monkeypatch):
    lab = product_lab
    owners = []
    acquire = lab.runtime.agent_store.acquire_execution_owner

    def capture(owner):
        owners.append(owner)
        return acquire(owner)

    monkeypatch.setattr(lab.runtime.agent_store, "acquire_execution_owner", capture)
    lab.runtime.start()
    assert len(owners) == 1
    assert lab.runtime.agents.readiness(require_execution_owner=True) is True
    lab.runtime.agent_store.release_execution_owner(owners[0])
    assert lab.runtime.agents.readiness(require_execution_owner=True) is False
    assert lab.runtime.inspect_readiness()["agents"] is False
    assert lab.runtime.close() is True


def test_partial_cp_startup_failure_closes_real_shared_agents_and_stores(
    product_lab, monkeypatch
):
    lab = product_lab

    def fail(*, background_identity):
        assert background_identity is lab.runtime.background_identity
        assert lab.runtime.agents.readiness(require_execution_owner=True) is True
        raise RuntimeError("RAW_STARTUP_SECRET_SENTINEL")

    monkeypatch.setattr(lab.runtime.control_plane, "start_background", fail)
    with pytest.raises(RuntimeError, match="product_startup_failed") as error:
        lab.runtime.start()
    assert "SECRET_SENTINEL" not in str(error.value)
    assert lab.runtime.status()["lifecycle"] == "closed"
    assert lab.runtime.agents.readiness(require_execution_owner=True) is False
    assert lab.native.calls == []


def test_active_agents_call_close_timeout_retains_store_and_original_work(
    product_lab, monkeypatch
):
    lab = product_lab
    profile = lab.register_profile()
    lab.native.release.clear()
    lab.runtime.start()
    receipt = lab.runtime.execution.start(lab.request(profile))
    assert receipt.status is StartStatus.ACCEPTED
    assert lab.native.entered.wait(10)
    disposed = []
    original_dispose = lab.runtime.credential_engine.dispose

    def dispose(*args, **kwargs):
        disposed.append("credentials")
        return original_dispose(*args, **kwargs)

    monkeypatch.setattr(lab.runtime.credential_engine, "dispose", dispose)
    assert lab.runtime.close(grace_seconds=0) is False
    assert lab.runtime.status()["lifecycle"] == "cleanup_incomplete"
    assert disposed == []
    assert lab.runtime.product_store.readiness() is True
    lab.native.release.set()
    assert lab.native.closed.wait(10)
    run = original_run_completed(lab, receipt.run_id)
    assert run.input_open is False and run.active_response_id is None
    assert lab.runtime.close(grace_seconds=10) is True
    assert disposed == ["credentials"]
    assert len([call for call in lab.native.calls if call[0] == "prompt"]) == 1


@pytest.mark.anyio
async def test_browser_close_does_not_stop_real_background_cp_and_unknown_drain_is_retained(
    planning_product_lab, monkeypatch
):
    lab, knowledge = planning_product_lab
    profile, setup = setup_planner(lab)
    lab.native.release.clear()
    lab.native.close_unknown = True
    native_grants = []
    native_receipts = []
    real_start = lab.runtime.execution.start

    def start(request):
        native_grants.append(request.grant_id)
        receipt = real_start(request)
        native_receipts.append(receipt.model_dump(mode="json"))
        return receipt

    monkeypatch.setattr(lab.runtime.execution, "start", start)
    lab.runtime.start()
    app = create_app(runtime=lab.runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=lab.runtime.settings.local_url,
        headers={"Origin": lab.runtime.settings.web_origin},
    ) as client:
        bootstrap = lab.runtime.local_sessions.create_bootstrap()
        exchange = await client.post(
            "/product/v1/session/bootstrap",
            json={"token": bootstrap.token.get_secret_value()},
        )
        assert exchange.status_code == 200
        goal = await submit_goal(client, lab, profile, setup)
        assert (await client.get("/healthz")).status_code == 200
        browser_cookies = httpx.Cookies(client.cookies)
    assert client.is_closed
    entered = await anyio.to_thread.run_sync(lab.native.entered.wait, 10)
    observation = await capture_original_planning(
        lab, app, goal, native_receipts, knowledge, browser_cookies=browser_cookies
    )
    assert entered, observation
    assert observation["native_call_counts"]["prompt"] == 1
    assert len(observation["runs"]["data"]["items"]) == 1
    package = observation["packages"][0]
    assert package["http_status"] == 200
    assert package["data"]["status"] == "active"
    assert package["data"]["assembly"]["status"] == "ready"
    assert package["data"]["assembly"]["error_code"] is None
    assert package["data"]["knowledge_refs"] == []
    assert native_receipts[0]["status"] == "accepted"
    assert lab.runtime.agents.readiness(require_execution_owner=True) is True
    shared_close = []
    real_close = lab.runtime.agents.close

    def close(*, timeout):
        shared_close.append(timeout)
        return real_close(timeout=timeout)

    monkeypatch.setattr(lab.runtime.agents, "close", close)
    assert (
        await anyio.to_thread.run_sync(lambda: lab.runtime.close(grace_seconds=0.1))
        is False
    )
    write_evidence(
        lab.evidence,
        "planner-original-close-pending",
        {
            "request_id": observation["request_id"],
            "start_receipts": native_receipts,
            "status": lab.runtime.status(),
            "shared_close_count": len(shared_close),
            "shared_owner_held": lab.runtime.agents.readiness(
                require_execution_owner=True
            ),
            "product_store_ready": lab.runtime.product_store.readiness(),
        },
    )
    assert shared_close == []
    assert lab.runtime.agents.readiness(require_execution_owner=True) is True
    assert lab.runtime.product_store.readiness() is True
    lab.native.release.set()
    assert await anyio.to_thread.run_sync(lab.native.close_attempted.wait, 10)
    assert (
        await anyio.to_thread.run_sync(lambda: lab.runtime.close(grace_seconds=0.1))
        is False
    )
    write_evidence(
        lab.evidence,
        "planner-original-unknown-retained",
        {
            "request_id": observation["request_id"],
            "start_receipts": native_receipts,
            "status": lab.runtime.status(),
            "shared_close_count": len(shared_close),
            "native_calls": lab.native.calls,
            "knowledge": knowledge.facts(),
        },
    )
    assert shared_close == []
    assert lab.runtime.status()["lifecycle"] == "cleanup_incomplete"
    lab.native.observe_gone()
    assert (
        await anyio.to_thread.run_sync(lambda: lab.runtime.close(grace_seconds=10))
        is True
    )
    assert len(shared_close) == 1
    prompts = [call for call in lab.native.calls if call[0] == "prompt"]
    assert len(prompts) == 1
    assert native_grants and all(
        grant not in lab.principal.lookups for grant in native_grants
    )
    write_evidence(
        lab.evidence,
        "planner-original-confirmed-close",
        {
            "request_id": observation["request_id"],
            "start_receipts": native_receipts,
            "status": lab.runtime.status(),
            "shared_close_count": len(shared_close),
            "native_calls": lab.native.calls,
            "knowledge": knowledge.facts(),
        },
    )


def test_public_preflight_has_real_heads_no_native_or_secret_reads(product_lab, capsys):
    lab = product_lab
    assert main(["preflight", "--json"], runtime=lab.runtime) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["storage_ready"] is True
    assert result["components"] == {
        "product": True,
        "agents": True,
        "control_plane": True,
        "credentials": True,
    }
    assert "postgresql" not in output
    assert lab.native.calls == [] and lab.secrets.reads == []
