import asyncio
import json
import socket
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from time import monotonic
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, SecretStr
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    CapabilitySupport,
    ExecutionWorkspace,
    NativeEvent,
    NativeInspection,
    NativeOutcome,
    ProfileDraft,
    RunStart,
    RunState,
    RunStop,
    StartStatus,
    StopStatus,
    ToolBinding,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.application.agent_planner_adapter import AgentPlannerAdapter
from sidera_control_plane.application.planner_contract import _proposal_shape_hint
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.result_integration import CheckSpecV1
from sqlalchemy import text

from sidera_product.app import create_app
from sidera_product.auth.grants import ProductGrants
from sidera_product.auth.local_session import CSRF_COOKIE, CSRF_HEADER, LocalSessions
from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings
from sidera_product.postgres.authority_store import (
    PostgresAuthorityStore,
    ToolReceiptRecord,
)
from tests.integration.test_product_lifecycle import repository
from tests.support.controlled_knowledge import OwnedKnowledgeServer
from tests.support.controlled_native import ControlledHarness, ControlledSecretStore
from tests.support.product_lab import CONTROLLED_PLANNING_BUDGET, write_evidence

pytestmark = pytest.mark.integration

ROLE_TOOLS = {
    "main": {
        "sidera_list_profiles",
        "sidera_submit_goal",
        "sidera_get_planning",
        "sidera_get_task",
        "sidera_list_tasks",
        "sidera_request_revision",
    },
    "planner": {"sidera_get_planning", "sidera_get_task"},
    "worker": {"sidera_get_task", "sidera_report_progress"},
}
FUNCTIONAL_SECONDS = 180


def owned_verification_check() -> CheckSpecV1:
    return CheckSpecV1.from_mapping(
        {
            "key": "owned-check",
            "version": 1,
            "argv": [sys.executable, "-c", "pass"],
            "cwd": ".",
            "environment_ref": "owned",
            "prepare_argvs": [],
            "timeout_seconds": 10,
            "success_exit_codes": [0],
            "report_paths": [],
            "required": True,
        }
    )


async def sdk_exchange(connection, *, name=None, arguments=None):
    headers = {
        key: value.get_secret_value() for key, value in connection.headers.items()
    }
    async with (
        streamablehttp_client(
            connection.endpoint_or_argv,
            headers=headers,
            timeout=20,
            sse_read_timeout=20,
        ) as (read, write, _),
        ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=20)
        ) as session,
    ):
        await session.initialize()
        if name is None:
            return {tool.name for tool in (await session.list_tools()).tools}
        return await session.call_tool(name, arguments or {})


def sdk_call(connection, name, arguments=None):
    result = asyncio.run(sdk_exchange(connection, name=name, arguments=arguments))
    assert not result.isError, result.content
    assert isinstance(result.structuredContent, dict), result.content
    return result.structuredContent


def controlled_proposal():
    body = _proposal_shape_hint(6)
    body.pop("usage")
    body.pop("knowledge_candidates")
    proposal = body["proposal"]
    proposal["root_task"].update(
        title="Trusted tool progress", goal_summary="Observe unverified progress"
    )
    for intent in proposal["execution_intents"]:
        intent["adapter_preferences"] = []
    proposal["collaboration_policy_request"]["input_groups"][0]["requirements"][
        "required_evidence_refs"
    ] = []
    spec = proposal["integration_spec"]
    spec["checks"][0]["key"] = "owned-check"
    spec["criteria"][0]["check_keys"] = ["owned-check"]
    return json.dumps(body, separators=(",", ":"))


class ToolJourneyHarness(ControlledHarness):
    harness_id = "controlled-task-tools"

    def __init__(self):
        super().__init__()
        self.main_release, self.worker_release = Event(), Event()
        self.main_entered, self.worker_entered = Event(), Event()
        self.connections, self.roles, self.open_facts, self.native_events = (
            {},
            {},
            [],
            {},
        )
        self.worker_handle = None
        self.runtime = None

    def probe(self, candidate, checked_at):
        observed = super().probe(candidate, checked_at)
        return observed.model_copy(
            update={
                "capabilities": {
                    **observed.capabilities,
                    "mcp": CapabilitySupport(
                        status="supported",
                        reason_code="controlled-task-tool-sdk",
                        constraints={},
                    ),
                },
            }
        )

    def open(self, request):
        handle = super().open(request)
        connections = [
            tool for tool in request.tools if tool.name == "SIDERA Task Tools"
        ]
        assert connections and all(tool == connections[0] for tool in connections)
        connection = connections[0]
        names = asyncio.run(sdk_exchange(connection))
        role = next(role for role, expected in ROLE_TOOLS.items() if names == expected)
        token = SecretStr(
            connection.headers["Authorization"]
            .get_secret_value()
            .removeprefix("Bearer ")
        )
        verified = self.runtime.grants.verify(token)
        assert (
            verified.run.run_id == request.run_id
            and verified.run.state is RunState.STARTING
        )
        assert verified.record.role == role
        denied = asyncio.run(sdk_exchange(connection, name=next(iter(sorted(names)))))
        assert denied.isError
        with self.lock:
            self.connections[request.run_id], self.roles[request.run_id] = (
                connection,
                role,
            )
            self.open_facts.append(
                {
                    "run_id": str(request.run_id),
                    "role": role,
                    "profile_ref": verified.record.profile_ref.model_dump(mode="json"),
                    "state": verified.run.state.value,
                    "tools": sorted(names),
                    "starting_call_denied": denied.isError,
                    "cp_link_run_id": None
                    if verified.scope is None or verified.scope.run_id is None
                    else str(verified.scope.run_id),
                    "execution_version": None
                    if verified.scope is None
                    else verified.scope.execution_version,
                }
            )
        return handle

    def prompt(self, handle, assembled_input, response_id):
        with self.lock:
            role = self.roles[handle.run_id]
            self.calls.append(("prompt", handle.run_id, response_id))
            self.inspections[handle.run_id] = NativeInspection(
                process_state="alive",
                response_state="active",
                reason_code=None,
            )
        if role == "main":
            self.main_entered.set()
            assert self.main_release.wait(FUNCTIONAL_SECONDS)
        elif role == "planner":
            self.emit(handle, response_id, controlled_proposal())
        else:
            self.worker_handle = handle
            self.worker_entered.set()
            assert self.worker_release.wait(FUNCTIONAL_SECONDS)
        with self.lock:
            self.inspections[handle.run_id] = NativeInspection(
                process_state="alive",
                response_state="idle",
                reason_code=None,
            )
        return NativeOutcome(
            response_id=response_id,
            status="completed",
            reason_code=None,
            observed_model=None,
            usage=None,
        )

    def emit(self, handle, response_id, message):
        with self.lock:
            events = self.native_events.setdefault(handle.run_id, [])
            events.append(
                NativeEvent(
                    cursor=len(events) + 1,
                    native_id="owned-message-" + str(len(events) + 1),
                    kind="progress.updated",
                    response_id=response_id,
                    operation_id=None,
                    occurred_at=datetime.now(UTC),
                    data={"update_type": "agent_message", "text": message},
                )
            )

    def read_events(self, handle, after_cursor, limit=100):
        with self.lock:
            return tuple(
                event
                for event in self.native_events.get(handle.run_id, ())
                if event.cursor > after_cursor
            )[:limit]

    def release_all(self):
        self.main_release.set()
        self.worker_release.set()
        self.release.set()


class LoopbackProduct:
    def __init__(self, app, listener):
        self.app, self.listener = app, listener
        self.errors = []
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=listener.getsockname()[1],
                lifespan="on",
                access_log=False,
                log_level="critical",
                timeout_graceful_shutdown=10,
            )
        )
        self.thread = Thread(target=self._run, name="n18-owned-product-http")

    def _run(self):
        try:
            self.server.run(sockets=[self.listener])
        except Exception as error:  # noqa: BLE001 - retained and asserted after owned thread joins
            self.errors.append(type(error).__name__)

    def start(self):
        self.thread.start()
        deadline, condition = monotonic() + 10, Event()
        while not self.server.started:
            assert self.thread.is_alive() and not self.errors, self.errors
            assert monotonic() < deadline, "Owned product HTTP startup did not complete"
            condition.wait(0.01)

    def close(self):
        self.server.should_exit = True
        if self.thread.ident is not None:
            self.thread.join(15)
        self.listener.close()
        return not self.thread.is_alive() and not self.errors


def guarded_rows(guard, table, fields, *, where="TRUE", parameters=None):
    guard.connection.rollback()
    try:
        assert (
            guard.connection.execute(
                text("SELECT current_setting('transaction_read_only')")
            ).scalar_one()
            == "on"
        )
        owned = {
            (row["schema"], row["name"])
            for row in guard.initial["inventory"]["relations"]
            if row["kind"] in {"r", "p"}
        }
        schema, name = table.split(".")
        assert (schema, name) in owned
        rows = guard.connection.execute(
            text(
                f'SELECT to_jsonb(original) FROM "{schema}"."{name}" AS original WHERE {where}'
            ),
            parameters or {},
        ).scalars()
        return [{key: row.get(key) for key in fields} for row in rows]
    finally:
        guard.connection.rollback()


def account_outstanding(guards, evidence, name):
    cp, agents = guards["cp"], guards["agents"]
    facts = {
        "read_only": True,
        "requests": guarded_rows(
            cp,
            "public.planning_requests",
            (
                "id",
                "workspace_id",
                "status",
                "version",
                "planning_profile_id",
                "planning_profile_version",
            ),
            where="status IN ('submitted','planning','waiting_for_user','proposal_ready')",
        ),
        "runs": guarded_rows(
            cp,
            "public.planning_runs",
            (
                "id",
                "workspace_id",
                "planning_request_id",
                "status",
                "error_code",
                "planner_agent_ref",
                "operation_id",
                "version",
                "queued_at",
                "started_at",
                "finished_at",
            ),
            where="status IN ('queued','running','retry_wait')",
        ),
        "planning_links": guarded_rows(
            cp,
            "public.planning_agent_links",
            (
                "workspace_id",
                "planning_run_id",
                "authority_id",
                "start_operation_id",
                "run_id",
                "status",
                "reason_code",
                "event_cursor",
                "closed",
            ),
            where="NOT closed",
        ),
        "worker_links": guarded_rows(
            cp,
            "public.agent_execution_links",
            (
                "workspace_id",
                "execution_id",
                "authority_id",
                "start_operation_id",
                "run_id",
                "status",
                "reason_code",
                "event_cursor",
            ),
            where="status NOT IN ('closed','succeeded','failed','rejected')",
        ),
        "assemblies": guarded_rows(
            cp,
            "public.context_assembly_requests",
            (
                "id",
                "workspace_id",
                "consumer_id",
                "status",
                "error_code",
                "attempt",
                "next_attempt_at",
                "request_fingerprint",
                "input_selection_id",
                "input_selection_hash",
                "work_context_revision_id",
                "created_at",
                "updated_at",
            ),
            where="status NOT IN ('ready','failed','cancelled') OR consumer_id IN (SELECT id FROM public.planning_runs WHERE status IN ('queued','running','retry_wait'))",
        ),
        "packages": guarded_rows(
            cp,
            "public.context_packages",
            (
                "id",
                "workspace_id",
                "consumer_id",
                "status",
                "work_context_revision_id",
                "package_hash",
                "created_at",
                "updated_at",
            ),
            where="consumer_id IN (SELECT id FROM public.planning_runs WHERE status IN ('queued','running','retry_wait'))",
        ),
        "native_runs": guarded_rows(
            agents,
            "agents.runs",
            (
                "run_id",
                "workspace_id",
                "operation_id",
                "profile_id",
                "profile_revision",
                "state",
                "terminal_state",
                "terminal_reason_code",
            ),
            where="state IN ('starting','running','closing','unknown')",
        ),
    }
    write_evidence(evidence, name, facts)
    return facts


@dataclass
class TaskToolLab:
    runtime: object
    native: ToolJourneyHarness
    knowledge: OwnedKnowledgeServer
    server: LoopbackProduct
    guards: dict
    directory: Path
    evidence: Path | None
    facts: dict = field(default_factory=dict)
    main_stop: RunStop | None = None
    main_stop_submitted: bool = False
    main_stop_confirmed: bool = False

    def stop_original_main(self, *, grace_seconds=10):
        request = self.main_stop
        if request is None or self.main_stop_confirmed:
            return True
        self.facts["main_stop_request"] = request.model_dump(mode="json")
        observations = self.facts.setdefault("main_stop_observations", [])
        deadline, condition = monotonic() + grace_seconds, Event()
        if not self.main_stop_submitted:
            self.main_stop_submitted = True
            self.facts["main_stop_submitted"] = True
            try:
                receipt = self.runtime.execution.stop(request)
                self.facts["main_stop_receipt"] = receipt.model_dump(mode="json")
            except Exception as error:  # noqa: BLE001 - uncertain Stop is query-only
                self.facts["main_stop_error_code"] = (
                    error.code
                    if isinstance(error, AgentError)
                    else "stop_outcome_unknown"
                )
        while True:
            try:
                receipt = self.runtime.execution.get_stop(
                    request.workspace_id,
                    request.run_id,
                    request.operation_id,
                    grant_id=request.grant_id,
                )
                observations.append(receipt.model_dump(mode="json"))
                if (
                    receipt.operation_id == request.operation_id
                    and receipt.run_id == request.run_id
                    and receipt.status is StopStatus.STOPPED
                ):
                    self.main_stop_confirmed = True
                    self.facts["main_stop_confirmed"] = True
                    return True
            except Exception as error:  # noqa: BLE001 - retain original cleanup on uncertainty
                self.facts["main_stop_query_error_code"] = (
                    error.code
                    if isinstance(error, AgentError)
                    else "stop_outcome_unknown"
                )
            if monotonic() >= deadline:
                self.facts["main_stop_confirmed"] = False
                return False
            condition.wait(0.02)

    def wait_for(self, read, ready, stage):
        deadline, condition = monotonic() + FUNCTIONAL_SECONDS, Event()
        while True:
            value = read()
            if ready(value):
                self.facts["last_ready_stage"] = stage
                self.facts["last_ready_observation"] = (
                    value.model_dump(mode="json")
                    if isinstance(value, BaseModel)
                    else value
                )
                return value
            if monotonic() >= deadline:
                self.facts["failed_stage"], self.facts["last_observation"] = (
                    stage,
                    value,
                )
                write_evidence(self.evidence, "task-tools-stage-failure", self.facts)
                raise AssertionError(
                    "Original task-tool stage did not complete: " + stage
                )
            condition.wait(0.02)


@pytest.fixture
def task_tool_lab(
    selected_product_targets, tmp_path, retain_incomplete_product_work, monkeypatch
):
    targets, selected_guards, evidence = selected_product_targets
    guards = {guard.target.component: guard for guard in selected_guards}
    outstanding = account_outstanding(
        guards, evidence, "n18-original-work-before-tool-background"
    )
    listener = knowledge = native = runtime = server = lab = None
    cleanup_facts = {"outstanding_before_background": outstanding}
    source_root = Path(__file__).resolve().parents[4]
    original_halt = AgentPlannerAdapter._halt

    def retain_safe_halt(adapter, planner_input, link, code, usage):
        if code == "native_observation_unknown" and str(
            link.workspace_id
        ) == cleanup_facts.get("workspace_id"):
            errors = cleanup_facts.setdefault("native_observation_failures", [])
            if len(errors) < 4:
                error = sys.exception()
                frames = []
                trace = None if error is None else error.__traceback__
                while trace is not None:
                    filename = Path(trace.tb_frame.f_code.co_filename).resolve()
                    if filename.is_relative_to(source_root):
                        if len(frames) == 12:
                            frames.pop(0)
                        frames.append(
                            {
                                "filename": str(filename.relative_to(source_root)),
                                "function": trace.tb_frame.f_code.co_name,
                                "line": trace.tb_lineno,
                            }
                        )
                    trace = trace.tb_next
                errors.append(
                    {
                        "planning_run_id": str(link.planning_run_id),
                        "start_operation_id": str(link.start_operation_id),
                        "native_run_id": None
                        if link.run_id is None
                        else str(link.run_id),
                        "exception_class": None
                        if error is None
                        else type(error).__name__,
                        "safe_code": error.code
                        if isinstance(error, (AgentError, DomainError))
                        else None,
                        "frames": frames,
                    }
                )
        return original_halt(adapter, planner_input, link, code, usage)

    monkeypatch.setattr(AgentPlannerAdapter, "_halt", retain_safe_halt)
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port, workspace = listener.getsockname()[1], uuid4()
        cleanup_facts["workspace_id"] = str(workspace)
        knowledge = OwnedKnowledgeServer(workspace)
        knowledge.start()
        by_component = {target.component: target for target in targets}
        native, secrets = ToolJourneyHarness(), ControlledSecretStore()
        settings = ProductSettings(
            **{
                field: by_component[component].url.render_as_string(hide_password=False)
                for field, component in (
                    ("product_database_url", "product"),
                    ("agent_database_url", "agents"),
                    ("cp_database_url", "cp"),
                    ("credential_database_url", "credentials"),
                )
            },
            deployment_namespace=UUID("898937f8-0a89-51ea-bfee-563b8524f4ad"),
            workspace_id=workspace,
            local_principal_id="owned-task-tool-user",
            bind_host="127.0.0.1",
            port=port,
            web_origin=f"http://127.0.0.1:{port}",
            workspace_root=tmp_path / "workspaces",
            artifact_root=tmp_path / "artifacts",
            agent_poll_interval_seconds=0.01,
            knowledge_base_url=knowledge.url,
            knowledge_token=knowledge.token,
        )
        settings.workspace_root.mkdir(mode=0o700)
        settings.artifact_root.mkdir(mode=0o700)
        runtime = build_product_runtime(
            settings, harnesses=(native,), secret_store=secrets
        )
        native.runtime = runtime
        app = create_app(runtime=runtime)
        server = LoopbackProduct(app, listener)
        lab = TaskToolLab(
            runtime,
            native,
            knowledge,
            server,
            guards,
            tmp_path,
            evidence,
            cleanup_facts,
        )
        yield lab
    finally:
        complete = True
        main_complete = installation_complete = durable_complete = True
        if runtime is not None:
            native.release_all()
            native.observe_gone()
            main_complete = lab is None or lab.stop_original_main()
            complete = main_complete and runtime.close(grace_seconds=10)
        server_complete = complete and (server is None or server.close())
        knowledge_complete = server_complete and (
            knowledge is None or knowledge.close()
        )
        if complete and server_complete and server is None and listener is not None:
            listener.close()
        if "owned_worker_identity" in cleanup_facts:
            identity = cleanup_facts["owned_worker_identity"]
            selected = {
                "workspace": UUID(identity["workspace_id"]),
                "execution": UUID(identity["execution_id"]),
            }
            durable_complete = False
            saved = {"read_only": True, "identity": identity, "tables": {}}
            cleanup_facts["owned_worker_after_cleanup"] = saved
            try:
                for name, fields in (
                    (
                        "executions",
                        (
                            "id",
                            "status",
                            "version",
                            "active_lease_id",
                            "assigned_worker_id",
                            "last_fencing_token",
                            "stop_requested",
                        ),
                    ),
                    (
                        "execution_sessions",
                        ("id", "status", "version", "last_sequence"),
                    ),
                    (
                        "assignment_leases",
                        (
                            "id",
                            "worker_id",
                            "status",
                            "version",
                            "fencing_token",
                            "expires_at",
                            "renewed_at",
                        ),
                    ),
                    (
                        "agent_execution_links",
                        (
                            "authority_id",
                            "start_operation_id",
                            "run_id",
                            "binding_version",
                            "status",
                            "event_cursor",
                        ),
                    ),
                    (
                        "gateway_operations",
                        (
                            "id",
                            "operation_id",
                            "session_id",
                            "lease_id",
                            "fencing_token",
                            "status",
                            "attempt_count",
                            "version",
                            "result_ref",
                            "failure_code",
                        ),
                    ),
                ):
                    identity_column = "id" if name == "executions" else "execution_id"
                    where = f"workspace_id=:workspace AND {identity_column}=:execution"
                    if name == "gateway_operations":
                        where += " AND operation_type='stop_session'"
                    saved["tables"][name] = guarded_rows(
                        guards["cp"],
                        "public." + name,
                        fields,
                        where=where,
                        parameters=selected,
                    )
                saved["tables"]["outbox_messages"] = guarded_rows(
                    guards["cp"],
                    "public.outbox_messages",
                    (
                        "id",
                        "event_id",
                        "topic",
                        "attempts",
                        "claimed_until",
                        "published_at",
                    ),
                    where=(
                        "EXISTS (SELECT 1 FROM public.events AS event "
                        "WHERE event.id=original.event_id AND event.workspace_id=:workspace "
                        "AND event.aggregate_type='execution' AND event.aggregate_id=:execution "
                        "AND event.event_type='ExecutionStopRequested')"
                    ),
                    parameters=selected,
                )
                rows = saved["tables"]
                executions, sessions, leases, links, stops = (
                    rows[name]
                    for name in (
                        "executions",
                        "execution_sessions",
                        "assignment_leases",
                        "agent_execution_links",
                        "gateway_operations",
                    )
                )
                original_lease = next(
                    (row for row in leases if row["id"] == identity["lease_id"]), None
                )
                durable_complete = (
                    len(executions) == 1
                    and executions[0]["active_lease_id"] is None
                    and executions[0]["last_fencing_token"] == identity["fencing_token"]
                    and identity.get("worker_id") is not None
                    and executions[0]["assigned_worker_id"] == identity["worker_id"]
                    and bool(sessions)
                    and all(row["status"] != "active" for row in sessions)
                    and all(row["status"] != "active" for row in leases)
                    and original_lease is not None
                    and original_lease["worker_id"] == identity["worker_id"]
                    and original_lease["fencing_token"] == identity["fencing_token"]
                    and len(links) == 1
                    and links[0]["run_id"] == identity["run_id"]
                    and links[0]["start_operation_id"] == identity["start_operation_id"]
                    and links[0]["binding_version"] == identity["binding_version"]
                    and links[0]["authority_id"] == identity.get("authority_id")
                    and (not rows["outbox_messages"] or bool(stops))
                    and all(
                        row["status"] == "succeeded"
                        and row["lease_id"] == identity["lease_id"]
                        and row["fencing_token"] == identity["fencing_token"]
                        and any(
                            session["id"] == row["session_id"]
                            and session["status"] == "closed"
                            for session in sessions
                        )
                        for row in stops
                    )
                )
            finally:
                saved["durable_settlement_confirmed"] = durable_complete
                write_evidence(
                    evidence, "task-tools-owned-worker-after-cleanup", cleanup_facts
                )
                if not durable_complete:
                    retain_incomplete_product_work((lab, selected_guards))
        if complete and lab is not None and "runtime_registration" in lab.facts:
            registered_ref = lab.facts["runtime_registration"]["ref"]
            references = guarded_rows(
                guards["agents"],
                "agents.runtime_references",
                ("installation_key",),
                where="workspace_id=:workspace AND runtime_id=:runtime",
                parameters={
                    "workspace": UUID(registered_ref["workspace_id"]),
                    "runtime": UUID(registered_ref["runtime_id"]),
                },
            )
            assert len(references) == 1
            selected = {"installation": references[0]["installation_key"]}
            installations = guarded_rows(
                guards["agents"],
                "agents.runtime_installations",
                (
                    "installation_key",
                    "harness_id",
                    "max_active",
                    "max_sessions",
                    "active_count",
                    "session_count",
                ),
                where="installation_key=:installation",
                parameters=selected,
            )
            reservations = guarded_rows(
                guards["agents"],
                "agents.run_reservations",
                ("run_id", "workspace_id", "profile_id", "active", "session"),
                where="installation_key=:installation",
                parameters=selected,
            )
            installation_complete = (
                len(installations) == 1
                and installations[0]["harness_id"] == "controlled-task-tools"
                and installations[0]["max_active"] == 1
                and installations[0]["max_sessions"] == 2
                and installations[0]["active_count"] == 0
                and installations[0]["session_count"] == 0
                and all(
                    not row["active"] and not row["session"] for row in reservations
                )
            )
            cleanup_facts["owned_installation_after_cleanup"] = {
                "read_only": True,
                "installations": installations,
                "reservations": reservations,
                "no_active_or_session_holder": installation_complete,
            }
        cleanup_facts.update(
            main_cleanup_complete=main_complete,
            runtime_close_complete=complete,
            durable_worker_cleanup_complete=durable_complete,
            http_thread_closed=server_complete,
            http_errors=[] if server is None else server.errors,
            knowledge_closed=knowledge_complete,
            knowledge=None if knowledge is None else knowledge.facts(),
            native_calls=[] if native is None else native.calls,
            open_facts=[] if native is None else native.open_facts,
        )
        write_evidence(evidence, "task-tool-journey-final", cleanup_facts)
        if not (
            complete
            and server_complete
            and knowledge_complete
            and installation_complete
            and durable_complete
        ):
            retain_incomplete_product_work(
                (lab, selected_guards)
                if lab is not None
                else (runtime, native, server, knowledge, listener, selected_guards)
            )
        assert (
            complete
            and server_complete
            and knowledge_complete
            and installation_complete
            and durable_complete
        ), "Original tool journey cleanup pending"


@contextmanager
def browser_session(lab):
    issue = lab.runtime.local_sessions.create_bootstrap()
    with httpx.Client(
        base_url=lab.runtime.settings.local_url,
        trust_env=False,
        timeout=20,
        headers={"Origin": lab.runtime.settings.web_origin},
    ) as client:
        exchanged = client.post(
            "/product/v1/session/bootstrap",
            json={"token": issue.token.get_secret_value()},
        )
        assert exchanged.status_code == 200, exchanged.json()
        client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
        yield client, UUID(exchanged.json()["session_id"])


def get_json(client, path):
    reply = client.get(path)
    assert reply.status_code == 200, reply.json()
    return reply.json()


def goal_arguments():
    return {
        "request_id": str(uuid4()),
        "title": "Owned trusted task tools",
        "objective": "Exercise only actual trusted business facts",
        "constraints": [],
        "acceptance_criteria": ["acceptance:one"],
        "requested_outputs": ["diff"],
        "context_refs": [],
    }


class LoseOriginalReply(httpx.AsyncBaseTransport):
    def __init__(
        self, app, *, read_original_receipt: Callable[[], ToolReceiptRecord | None]
    ):
        self.inner = httpx.ASGITransport(app=app)
        self.read_original_receipt = read_original_receipt
        self.original_command, self.posts, self.reads = None, [], []
        self.lost_reply = False
        self.hide_original_status = True

    async def handle_async_request(self, request):
        if self.original_command is None and (
            (request.method == "POST" and request.url.path == "/api/v1/commands")
            or (request.method == "GET" and "/commands/" in request.url.path)
        ):
            receipt = await anyio.to_thread.run_sync(self.read_original_receipt)
            assert isinstance(receipt, ToolReceiptRecord)
            command_id = str(receipt.command_id)
            if self.original_command is None:
                self.original_command = command_id
            else:
                assert self.original_command == command_id
        if request.method == "POST" and request.url.path == "/api/v1/commands":
            command_id = json.loads(request.content)["command_id"]
            self.posts.append(command_id)
            lose_reply = command_id == self.original_command and not self.lost_reply
            response = await self.inner.handle_async_request(request)
            if lose_reply and not self.lost_reply:
                assert response.status_code in {200, 201}
                self.lost_reply = True
                await response.aclose()
                raise httpx.ReadError("Owned reply lost after commit", request=request)
            return response
        if (
            request.method == "GET"
            and self.original_command is not None
            and request.url.path.endswith("/commands/" + self.original_command)
        ):
            self.reads.append(self.original_command)
            if self.hide_original_status:
                raise httpx.ReadError(
                    "Owned original status not yet observable", request=request
                )
        return await self.inner.handle_async_request(request)


def profile_draft(runtime_ref, resource_ref, name):
    return ProfileDraft(
        name=name,
        description="Same generic Profile, assignment-scoped rights",
        runtime_ref=runtime_ref,
        instructions="Use only the current authorized task tools.",
        resource_refs=(resource_ref,),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )


def command_body(
    lab, kind, payload, *, target=None, version=None, actor="user", command_id=None
):
    command_id = str(uuid4()) if command_id is None else command_id
    return {
        "command_id": command_id,
        "workspace_id": str(lab.runtime.settings.workspace_id),
        "command_type": kind,
        "target_type": "planning_request" if target else "workspace",
        "target_id": target,
        "expected_version": version,
        "actor": {"type": actor, "id": lab.runtime.settings.local_principal_id},
        "idempotency_key": command_id,
        "correlation_id": command_id,
        "payload": payload,
    }


def business_facts(lab, verified):
    scope = verified.scope
    assert scope is not None
    cp = lab.guards["cp"]
    selected = {"workspace": scope.workspace_id}
    facts = {}
    for name, table, identity, fields in (
        (
            "execution",
            "executions",
            scope.execution_id,
            (
                "id",
                "workspace_id",
                "status",
                "version",
                "worker_item_id",
                "assigned_worker_id",
            ),
        ),
        (
            "worker_item",
            "worker_items",
            scope.worker_item_id,
            ("id", "workspace_id", "status", "version", "root_task_id"),
        ),
        (
            "root",
            "tasks",
            scope.root_task_id,
            ("id", "workspace_id", "status", "version"),
        ),
        (
            "lease",
            "assignment_leases",
            scope.lease_id,
            (
                "id",
                "workspace_id",
                "execution_id",
                "worker_id",
                "status",
                "fencing_token",
            ),
        ),
    ):
        facts[name] = guarded_rows(
            cp,
            "public." + table,
            fields,
            where="workspace_id=:workspace AND id=:identity",
            parameters={**selected, "identity": identity},
        )
    facts["link"] = guarded_rows(
        cp,
        "public.agent_execution_links",
        (
            "workspace_id",
            "execution_id",
            "authority_id",
            "binding_version",
            "start_operation_id",
            "run_id",
            "status",
            "event_cursor",
        ),
        where="workspace_id=:workspace AND execution_id=:execution",
        parameters={**selected, "execution": scope.execution_id},
    )
    run = lab.runtime.execution.get_run(
        scope.workspace_id, verified.run.run_id, grant_id=scope.native_grant_id
    )
    facts["native"] = {
        "run_id": str(run.run_id),
        "state": run.state.value,
        "response_id": str(run.active_response_id),
        "sequence": run.last_event_sequence,
    }
    return facts


def observation_is_current(facts, *, after_cursor=None):
    links = facts["link"]
    if len(links) != 1:
        return False
    cursor, sequence = links[0]["event_cursor"], facts["native"]["sequence"]
    return (
        sequence > 0
        and cursor == sequence
        and (after_cursor is None or cursor > after_cursor)
    )


def test_official_loopback_tools_create_real_goal_and_nonterminal_progress(
    task_tool_lab,
    monkeypatch,
):
    lab = task_tool_lab
    runtime, workspace = lab.runtime, lab.runtime.settings.workspace_id
    lab.server.start()
    prefix = f"/api/v1/workspaces/{workspace}"
    with browser_session(lab) as (browser, session_id):
        registered = runtime.agents.runtimes.register(
            workspace,
            lab.native.harness_id,
            sys.executable,
            max_active=1,
            max_sessions=2,
            grant_id=session_id,
        )
        lab.facts["runtime_registration"] = {
            "harness_id": registered.harness_id,
            "ref": registered.ref.model_dump(mode="json"),
            "limits": registered.limits,
            "version": registered.version,
        }
        assert registered.harness_id == "controlled-task-tools"
        assert registered.limits == {"max_active": 1, "max_sessions": 2}
        resource = runtime.agents.catalog.create_resource(
            workspace,
            CapabilityResourceDraft(
                name="Explicit owned task tools",
                kind="mcp",
                settings={"connection_id": str(runtime.grants.connection_id)},
                secret_refs=(),
            ),
            grant_id=session_id,
        )
        policy_profile = runtime.agents.catalog.create_profile(
            workspace,
            profile_draft(registered.ref, resource.ref, "Configured policy A"),
            grant_id=session_id,
        )
        profile = runtime.agents.catalog.create_profile(
            workspace,
            profile_draft(registered.ref, resource.ref, "Selected generic B"),
            grant_id=session_id,
        )
        repo = repository(runtime.settings.workspace_root / "repository")
        setup_reply = browser.post(
            f"/product/v1/workspaces/{workspace}/setup",
            json={
                "repository_directory": str(repo),
                "expected_version": 0,
                "planner_agent_ref": policy_profile.ref.model_dump(mode="json"),
                "planning_budget": {
                    key: getattr(CONTROLLED_PLANNING_BUDGET, key)
                    for key in (
                        "max_context_bytes",
                        "max_tokens",
                        "max_cost_microunits",
                        "max_runtime_seconds",
                        "max_revisions",
                        "max_nodes",
                        "max_concurrency",
                    )
                },
                "verification_checks": [owned_verification_check().to_mapping()],
            },
        )
        assert setup_reply.status_code == 200, setup_reply.json()
        setup = get_json(browser, f"/product/v1/workspaces/{workspace}/setup")
        assert setup["planner_agent_ref"] == policy_profile.ref.model_dump(mode="json")
        defaults = runtime.product_store.write_defaults(
            workspace,
            expected_version=0,
            main_profile_id=profile.ref.profile_id,
            planner_profile_id=profile.ref.profile_id,
            worker_profile_id=profile.ref.profile_id,
        )
        main_directory = runtime.settings.workspace_root / "main"
        main_directory.mkdir(mode=0o700)
        main_workspace = ExecutionWorkspace(
            directory=str(main_directory), writable=True, artifact_directory=None
        )
        operation = uuid4()
        main_grant = runtime.grants.prepare_main_scope(
            session_grant_id=session_id,
            operation_id=operation,
            profile_ref=profile.ref,
            workspace=main_workspace,
        )
        main_start = runtime.execution.start(
            RunStart(
                operation_id=operation,
                profile_ref=profile.ref,
                grant_id=main_grant,
                policy="conversation",
                instructions=(),
                text="Submit one short goal, then release active capacity.",
                context=(),
                tools=(ToolBinding(connection_id=runtime.grants.connection_id),),
                workspace=main_workspace,
                required_capabilities=("prompt",),
            )
        )
        lab.facts.update(
            main_start=main_start.model_dump(mode="json"),
            main_grant=str(main_grant),
            session_id=str(session_id),
            configured_profile=policy_profile.ref.model_dump(mode="json"),
            shared_profile=profile.ref.model_dump(mode="json"),
            setup=setup,
        )
        assert (
            main_start.status is StartStatus.ACCEPTED and main_start.run_id is not None
        )
        lab.main_stop = RunStop(
            workspace_id=workspace,
            run_id=main_start.run_id,
            operation_id=uuid4(),
            grant_id=main_grant,
        )
        main_run = lab.wait_for(
            lambda: runtime.execution.get_run(
                workspace, main_start.run_id, grant_id=main_grant
            ),
            lambda run: (
                run.state in {RunState.RUNNING, RunState.FAILED, RunState.UNKNOWN}
            ),
            "main response",
        )
        assert main_run.state is RunState.RUNNING, main_run.reason_code
        assert lab.native.main_entered.wait(20)
        main_connection = lab.native.connections[main_run.run_id]
        arguments = goal_arguments()
        logical_request_id = UUID(arguments["request_id"])
        original_find_receipt = runtime.authority_store.find_receipt
        lost = LoseOriginalReply(
            runtime.control_plane.app,
            read_original_receipt=lambda: original_find_receipt(
                grant_id=main_grant,
                tool_name="sidera_submit_goal",
                logical_request_id=logical_request_id,
            ),
        )
        lab.server.app.state.task_tools.client_transport = lost
        missing_lookup_barrier = Barrier(2, timeout=10)
        missing_lookup_lock = Lock()
        missing_lookup_count = 0

        def original_missing_lookup(**selection):
            nonlocal missing_lookup_count
            row = original_find_receipt(**selection)
            synchronize = False
            if (
                row is None
                and selection.get("grant_id") == main_grant
                and selection.get("tool_name") == "sidera_submit_goal"
                and selection.get("logical_request_id") == logical_request_id
                and selection.get("command_id") is None
            ):
                with missing_lookup_lock:
                    if missing_lookup_count < 2:
                        missing_lookup_count += 1
                        synchronize = True
            if synchronize:
                missing_lookup_barrier.wait()
            return row

        monkeypatch.setattr(
            runtime.authority_store, "find_receipt", original_missing_lookup
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as callers:
                pending = [
                    callers.submit(
                        sdk_call, main_connection, "sidera_submit_goal", arguments
                    )
                    for _ in range(2)
                ]
                concurrent_results = [
                    call.result(timeout=FUNCTIONAL_SECONDS) for call in pending
                ]
        finally:
            monkeypatch.setattr(
                runtime.authority_store, "find_receipt", original_find_receipt
            )
        first = concurrent_results[0]
        lab.facts.update(
            concurrent_goal_results=concurrent_results,
            original_missing_lookups=missing_lookup_count,
            original_goal_posts=list(lost.posts),
            original_goal_status_reads=list(lost.reads),
            original_arguments=arguments,
        )
        assert missing_lookup_count == 2
        assert all(
            reply["outcome"] == "unknown" and reply["command_id"] == first["command_id"]
            for reply in concurrent_results
        )
        assert (
            first["outcome"] == "unknown"
            and first["command_id"] == lost.original_command
        )
        assert (
            first["root_task_id"] is None
            and first["error_code"] == "command_result_unknown"
        )
        assert lost.posts == [first["command_id"]]
        lost.hide_original_status = False
        goal = sdk_call(main_connection, "sidera_submit_goal", arguments)
        assert (
            goal["outcome"] == "applied" and goal["command_id"] == first["command_id"]
        )
        assert (
            lost.posts.count(first["command_id"]) == 1
            and lost.reads == [first["command_id"]] * 3
        )
        root_id, request_id = goal["root_task_id"], goal["planning_request_id"]
        assert root_id and request_id
        receipt = runtime.authority_store.find_receipt(
            grant_id=main_grant,
            tool_name="sidera_submit_goal",
            logical_request_id=logical_request_id,
        )
        assert receipt is not None and receipt.command_id == UUID(goal["command_id"])
        receipt_rows = guarded_rows(
            lab.guards["product"],
            "product.tool_receipts",
            (
                "receipt_id",
                "grant_id",
                "run_id",
                "response_id",
                "tool_name",
                "logical_request_id",
                "command_id",
                "outcome",
            ),
            where="grant_id=:grant AND tool_name=:tool AND logical_request_id=:logical",
            parameters={
                "grant": main_grant,
                "tool": "sidera_submit_goal",
                "logical": logical_request_id,
            },
        )
        assert len(receipt_rows) == 1 and receipt_rows[0]["receipt_id"] == str(
            receipt.receipt_id
        )
        lab.facts.update(
            goal=goal,
            original_receipt_rows=receipt_rows,
            original_receipt_count=1,
            original_goal_status_reads=list(lost.reads),
            original_arguments=arguments,
            original_post_count=1,
        )
        command_status = get_json(browser, f"{prefix}/commands/{goal['command_id']}")
        assert (
            command_status["status"] == "applied"
            and command_status["command_id"] == goal["command_id"]
        )
        lab.facts.update(original_command_status=command_status)
        assert (
            runtime.execution.get_run(
                workspace, main_run.run_id, grant_id=main_grant
            ).state
            is RunState.RUNNING
        )
        assert not any(fact["role"] == "planner" for fact in lab.native.open_facts)
        first_detail = get_json(browser, f"{prefix}/planning-requests/{request_id}")
        assert first_detail["profile"][
            "planner_agent_ref"
        ] == policy_profile.ref.model_dump(mode="json")
        assert first_detail["active_run"][
            "planner_agent_ref"
        ] == profile.ref.model_dump(mode="json")
        first_snapshot = guarded_rows(
            lab.guards["cp"],
            "public.planning_requests",
            ("id", "profile_snapshot"),
            where="workspace_id=:workspace AND id=:request",
            parameters={"workspace": workspace, "request": UUID(request_id)},
        )[0]["profile_snapshot"]
        assert first_snapshot["planner_agent_ref"] == profile.ref.model_dump(
            mode="json"
        )
        assert get_json(browser, f"/product/v1/workspaces/{workspace}/setup") == setup

        revised = runtime.agents.catalog.revise_profile(
            profile.ref,
            profile.draft.model_copy(
                update={"instructions": "New exact catalogue head for new work only."}
            ),
            grant_id=session_id,
        )
        uncaptured = runtime.agents.catalog.create_profile(
            workspace,
            profile_draft(registered.ref, resource.ref, "Uncaptured C"),
            grant_id=session_id,
        )
        catalogue = sdk_call(main_connection, "sidera_list_profiles")["profiles"]
        assert revised.ref.model_dump(mode="json") in [
            entry["profile_ref"] for entry in catalogue
        ]
        assert str(uncaptured.ref.profile_id) not in [
            entry["profile_ref"]["profile_id"] for entry in catalogue
        ]
        assert (
            runtime.grants.prepare_main_scope(
                session_grant_id=session_id,
                operation_id=operation,
                profile_ref=profile.ref,
                workspace=main_workspace,
            )
            == main_grant
        )
        assert sdk_call(main_connection, "sidera_submit_goal", arguments) == goal
        changed = asyncio.run(
            sdk_exchange(
                main_connection,
                name="sidera_submit_goal",
                arguments={**arguments, "objective": "Changed duplicate intent"},
            )
        )
        assert changed.isError and lost.posts.count(first["command_id"]) == 1
        denied_choice = asyncio.run(
            sdk_exchange(
                main_connection,
                name="sidera_submit_goal",
                arguments={
                    **goal_arguments(),
                    "planner_agent_ref": uncaptured.ref.model_dump(mode="json"),
                },
            )
        )
        assert denied_choice.isError
        runtime.product_store.write_defaults(
            workspace,
            expected_version=defaults.version,
            main_profile_id=profile.ref.profile_id,
            planner_profile_id=profile.ref.profile_id,
            worker_profile_id=profile.ref.profile_id,
        )
        second_goal = sdk_call(main_connection, "sidera_submit_goal", goal_arguments())
        second_detail = get_json(
            browser, f"{prefix}/planning-requests/{second_goal['planning_request_id']}"
        )
        assert second_detail["active_run"][
            "planner_agent_ref"
        ] == revised.ref.model_dump(mode="json")
        assert (
            guarded_rows(
                lab.guards["cp"],
                "public.planning_requests",
                ("profile_snapshot",),
                where="workspace_id=:workspace AND id=:request",
                parameters={"workspace": workspace, "request": UUID(request_id)},
            )[0]["profile_snapshot"]
            == first_snapshot
        )
        assert (
            runtime.execution.get_run(
                workspace, main_run.run_id, grant_id=main_grant
            ).profile_ref
            == profile.ref
        )

        lab.native.main_release.set()
        idle_main = lab.wait_for(
            lambda: runtime.execution.get_run(
                workspace, main_run.run_id, grant_id=main_grant
            ),
            lambda run: run.state is RunState.IDLE,
            "ordinary Main becomes idle",
        )
        assert idle_main.input_open
        assert (
            lab.native.inspect(lab.native.handles[main_run.run_id]).process_state
            == "alive"
        )
        assert ("close", main_run.run_id) not in lab.native.calls
        detail = lab.wait_for(
            lambda: get_json(browser, f"{prefix}/planning-requests/{request_id}"),
            lambda data: data["status"] in {"proposal_ready", "failed"},
            "original Planner proposal",
        )
        assert detail["status"] == "proposal_ready", detail
        proposal = get_json(
            browser, f"{prefix}/planning-requests/{request_id}/proposals/1"
        )
        second_final = lab.wait_for(
            lambda: get_json(
                browser,
                f"{prefix}/planning-requests/{second_goal['planning_request_id']}",
            ),
            lambda data: data["status"] in {"proposal_ready", "failed"},
            "new Planner revision proposal",
        )
        assert second_final["status"] == "proposal_ready", second_final
        lab.facts.update(
            second_goal=second_goal,
            second_proposal=second_final,
            first_profile_snapshot=first_snapshot,
            second_planner_ref=second_detail["active_run"]["planner_agent_ref"],
        )
        confirm = command_body(
            lab,
            "ConfirmPlanningProposal",
            {
                "proposal_revision": 1,
                "proposal_content_hash": proposal["content_hash"],
                "profile_id": setup["planning_policy_ref"]["policy_id"],
                "profile_version": setup["planning_policy_ref"]["version"],
                "assignee_bindings": [
                    {
                        "target_type": "subtask",
                        "target_key": "subtask-one",
                        "assignee_kind": "human",
                        "assignee_id": runtime.settings.local_principal_id,
                        "profile_revision": None,
                        "expected_binding_version": None,
                    },
                    *[
                        {
                            "target_type": "worker_item",
                            "target_key": key,
                            "assignee_kind": "profile",
                            "assignee_id": str(profile.ref.profile_id),
                            "profile_revision": profile.ref.revision,
                            "expected_binding_version": None,
                        }
                        for key in ("worker-one", "source-worker")
                    ],
                ],
            },
            target=request_id,
            version=detail["version"],
        )
        rejected_csrf = browser.post(
            "/api/v1/commands", json=confirm, headers={CSRF_HEADER: "wrong-session"}
        )
        assert rejected_csrf.status_code == 403
        assert (
            get_json(browser, f"{prefix}/planning-requests/{request_id}")["status"]
            == "proposal_ready"
        )
        confirmed = browser.post("/api/v1/commands", json=confirm)
        assert confirmed.status_code in {200, 201}, confirmed.json()
        lab.facts["confirmation"] = confirmed.json()
        assert lab.native.worker_entered.wait(FUNCTIONAL_SECONDS)
        worker_handle = lab.native.worker_handle
        worker_connection = lab.native.connections[worker_handle.run_id]
        worker_token = SecretStr(
            worker_connection.headers["Authorization"]
            .get_secret_value()
            .removeprefix("Bearer ")
        )
        verified = runtime.grants.verify(worker_token, require_running=True)
        assert verified.scope is not None and verified.scope.root_task_id == UUID(
            root_id
        )
        lab.facts["owned_worker_identity"] = {
            "workspace_id": str(verified.scope.workspace_id),
            "execution_id": str(verified.scope.execution_id),
            "lease_id": str(verified.scope.lease_id),
            "fencing_token": verified.scope.fencing_token,
            "run_id": str(verified.run.run_id),
            "start_operation_id": str(verified.scope.start_operation_id),
            "binding_version": verified.scope.binding_version,
        }
        assert (
            verified.record.profile_ref == profile.ref
            and verified.run.profile_ref == profile.ref
        )
        assert verified.scope.execution_version >= verified.record.execution_version
        assert {
            fact["role"]
            for fact in lab.native.open_facts
            if fact["profile_ref"] == profile.ref.model_dump(mode="json")
        } == {"main", "planner", "worker"}

        before = lab.wait_for(
            lambda: business_facts(
                lab, runtime.grants.verify(worker_token, require_running=True)
            ),
            observation_is_current,
            "owned startup observations caught up",
        )
        lab.facts["before_progress_baseline"] = before
        lab.facts["owned_worker_identity"].update(
            worker_id=before["execution"][0]["assigned_worker_id"],
            authority_id=before["link"][0]["authority_id"],
        )
        progress1 = {
            "request_id": str(uuid4()),
            "summary": "Self-reported 100%, not accepted",
            "steps": [
                {"stage": "arbitrary", "percent": 100, "nested": {"verified": False}}
            ],
        }
        receipt1 = sdk_call(worker_connection, "sidera_report_progress", progress1)
        lab.facts["first_progress_arguments"] = progress1
        lab.facts["first_progress_receipt"] = receipt1
        assert receipt1["outcome"] == "applied" and receipt1["status"] == "reported"
        after1 = business_facts(
            lab, runtime.grants.verify(worker_token, require_running=True)
        )
        assert after1 == before
        assert (
            sdk_call(worker_connection, "sidera_report_progress", progress1) == receipt1
        )
        lab.native.emit(
            worker_handle,
            verified.run.active_response_id,
            "Normal owned native observation, not a business progress report",
        )
        advanced = lab.wait_for(
            lambda: business_facts(
                lab, runtime.grants.verify(worker_token, require_running=True)
            ),
            lambda facts: observation_is_current(
                facts, after_cursor=before["link"][0]["event_cursor"]
            ),
            "normal native observation advances",
        )
        lab.facts["after_native_observation_baseline"] = advanced
        progress2 = {
            "request_id": str(uuid4()),
            "summary": "Second independent unverified report",
            "steps": [{"worker_defined": ["inspect", "compare"], "complete": True}],
        }
        receipt2 = sdk_call(worker_connection, "sidera_report_progress", progress2)
        assert (
            receipt2["outcome"] == "applied"
            and receipt2["command_id"] != receipt1["command_id"]
        )
        assert (
            business_facts(
                lab, runtime.grants.verify(worker_token, require_running=True)
            )
            == advanced
        )
        assert (
            sdk_call(worker_connection, "sidera_report_progress", progress1) == receipt1
        )
        workspace_view = get_json(
            browser, f"{prefix}/worker-items/{verified.scope.worker_item_id}/workspace"
        )
        reports = guarded_rows(
            lab.guards["cp"],
            "public.events",
            (
                "id",
                "aggregate_type",
                "aggregate_id",
                "aggregate_version",
                "event_type",
                "payload",
                "causation_id",
            ),
            where="workspace_id=:workspace AND event_type='WorkerProgressReported'",
            parameters={"workspace": workspace},
        )
        assert len(reports) == 2
        assert all(
            row["id"] == row["aggregate_id"]
            and row["aggregate_version"] == 1
            and row["payload"]["verified"] is False
            for row in reports
        )
        assert {row["causation_id"] for row in reports} == {
            receipt1["command_id"],
            receipt2["command_id"],
        }
        second_report = next(
            row for row in reports if row["causation_id"] == receipt2["command_id"]
        )
        assert workspace_view["current_progress"]["report_id"] == second_report["id"]
        assert (
            workspace_view["current_progress"]["steps"] == progress2["steps"]
            and workspace_view["current_progress"]["verified"] is False
        )
        activity = get_json(
            browser, f"{prefix}/worker-items/{verified.scope.worker_item_id}/activity"
        )
        progress_activity = [
            item
            for item in activity["items"]
            if item["source_type"] == "progress_report"
        ]
        assert len(progress_activity) == 2
        for item in progress_activity:
            assert isinstance(item["body"], str)
            parsed_body = json.loads(item["body"])
            assert isinstance(parsed_body, dict) and parsed_body["verified"] is False
        lab.facts.update(
            receipts=[receipt1, receipt2],
            reports=reports,
            workspace_view=workspace_view,
            progress_activity=progress_activity,
            before_progress=before,
            after_native_observation=advanced,
        )

        reopened_store = PostgresAuthorityStore(runtime.authority_store.engine)
        reopened_sessions = LocalSessions(runtime.settings, reopened_store)
        assert reopened_sessions.identity_for_grant(
            session_id
        ) == runtime.local_sessions.identity_for_grant(session_id)
        reopened = ProductGrants(
            runtime.settings,
            reopened_store,
            reopened_sessions,
            catalog=runtime.agents.catalog,
            runtimes=runtime.agents.runtimes,
            execution=runtime.execution,
            read_cp_scope=runtime.control_plane.read_task_tool_scope,
            parent_authorizer=runtime.request_authorizer,
        )
        assert (
            reopened.verify(worker_token, require_running=True).record
            == verified.record
        )
        assert reopened_store.find_receipt(
            grant_id=verified.record.grant_id,
            tool_name="sidera_report_progress",
            logical_request_id=UUID(progress1["request_id"]),
        ).command_id == UUID(receipt1["command_id"])
        with pytest.raises(AgentError, match="cannot be reprojected") as unavailable:
            reopened.resolve(verified.record.grant_id, runtime.grants.connection_id)
        assert unavailable.value.code == "tool_connection_unavailable"
        reopened.close()
        lab.facts.update(
            persistent_originals=True,
            lost_cache_denied=True,
        )
        assert lab.stop_original_main(), "Original Main Stop cleanup pending"
        lab.native.close_unknown = True
        assert runtime.close(grace_seconds=0) is False
        assert runtime.status()["lifecycle"] == "cleanup_incomplete"
        assert (
            runtime.authority_store.find_grant(grant_id=verified.record.grant_id)
            == verified.record
        )
        assert (
            runtime.product_store.get_defaults(workspace).planner_profile_id
            == profile.ref.profile_id
        )
        lab.facts["close_false_preserved_originals"] = True
