import asyncio
import hashlib
import json
import os
import socket
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from threading import enumerate as enumerate_threads
from time import monotonic
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from pydantic import BaseModel, SecretStr
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    CapabilitySupport,
    NativeEvent,
    NativeHandle,
    NativeInspection,
    NativeOutcome,
    ProfileRef,
    RunState,
    RuntimeSnapshot,
    StartStatus,
    StopStatus,
    ToolConnection,
)
from sqlalchemy import select, text

from sidera_product.app import create_app
from sidera_product.auth.local_session import CSRF_COOKIE, CSRF_HEADER
from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings
from sidera_product.lifecycle import ProductRuntime
from sidera_product.postgres.models import ChatTurnRow, ConversationRow
from tests.integration.test_product_lifecycle import repository
from tests.integration.test_task_tool_progress import (
    ROLE_TOOLS,
    LoopbackProduct,
    account_outstanding,
    browser_session,
    controlled_proposal,
    get_json,
    goal_arguments,
    guarded_rows,
    owned_verification_check,
    profile_draft,
    sdk_call,
    sdk_exchange,
)
from tests.support.controlled_knowledge import OwnedKnowledgeServer
from tests.support.controlled_native import ControlledHarness, ControlledSecretStore
from tests.support.product_lab import CONTROLLED_PLANNING_BUDGET, write_evidence
from tests.support.product_targets import (
    TargetGuard,
    acquire_guards,
    select_targets,
    verify_objects,
)

pytestmark = pytest.mark.integration
FUNCTIONAL_SECONDS = 180


def retained_first_attempt(evidence: Path | None):
    if evidence is None or not (evidence / "chat-api-first-outcome.json").is_file():
        pytest.fail("NOT_RUN: retained original N19 attempt is required")
    outcome = json.loads((evidence / "chat-api-first-outcome.json").read_text())
    name = outcome["functional_and_cleanup"]
    assert Path(name).name == name and outcome["exit_code"] == 1
    saved = json.loads((evidence / name).read_text())
    assert (
        saved["cleanup_complete"] is True
        and saved["workspace_id"] == outcome["workspace_id"]
    )
    assert (
        saved["owner_threads"] == [] and saved["original_goal"]["outcome"] == "applied"
    )
    conversation = saved["final_durable_cleanup"]["conversations"][0]
    goal = saved["original_goal"]
    for value in (
        saved["workspace_id"],
        conversation["id"],
        conversation["run_id"],
        conversation["grant_id"],
        conversation["stop_operation_id"],
        goal["root_task_id"],
        goal["command_id"],
        goal["planning_request_id"],
        saved["actual_receipt_ids"][0],
        saved["first_active"]["responseId"],
    ):
        assert UUID(value).int != 0
    assert conversation["stop_state"] == "stopped"
    assert saved["first_run"]["run_id"] == conversation["run_id"]
    assert sum(call[0] == "open" for call in saved["native_calls"]) == 1
    assert sum(call[0] == "prompt" for call in saved["native_calls"]) == 1
    assert sum(call[0] == "close" for call in saved["native_calls"]) == 1
    return saved


def retained_original_rows(guards: dict[str, TargetGuard], saved):
    conversation = saved["final_durable_cleanup"]["conversations"][0]
    goal = saved["original_goal"]
    selection = {
        "workspace": UUID(saved["workspace_id"]),
        "conversation": UUID(conversation["id"]),
        "run": UUID(conversation["run_id"]),
        "stop": UUID(conversation["stop_operation_id"]),
        "root": UUID(goal["root_task_id"]),
        "command": UUID(goal["command_id"]),
        "receipt": UUID(saved["actual_receipt_ids"][0]),
    }
    return {
        "conversations": guarded_rows(
            guards["product"],
            "product.conversations",
            (
                "id",
                "workspace_id",
                "run_id",
                "grant_id",
                "start_operation_id",
                "stop_operation_id",
                "stop_state",
            ),
            where="id=:conversation AND workspace_id=:workspace",
            parameters=selection,
        ),
        "turns": guarded_rows(
            guards["product"],
            "product.chat_turns",
            (
                "id",
                "conversation_id",
                "operation_id",
                "native_operation_id",
                "response_id",
                "request_state",
                "dispatch_attempt",
            ),
            where="conversation_id=:conversation AND workspace_id=:workspace",
            parameters=selection,
        ),
        "receipts": guarded_rows(
            guards["product"],
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
            where="receipt_id=:receipt AND run_id=:run",
            parameters=selection,
        ),
        "commands": guarded_rows(
            guards["cp"],
            "public.commands",
            (
                "id",
                "workspace_id",
                "command_type",
                "status",
            ),
            where="id=:command AND workspace_id=:workspace",
            parameters=selection,
        ),
        "tasks": guarded_rows(
            guards["cp"],
            "public.tasks",
            (
                "id",
                "workspace_id",
                "title",
                "status",
                "version",
            ),
            where="id=:root AND workspace_id=:workspace",
            parameters=selection,
        ),
        "runs": guarded_rows(
            guards["agents"],
            "agents.runs",
            (
                "run_id",
                "workspace_id",
                "operation_id",
                "original_grant_id",
                "state",
                "terminal_state",
            ),
            where="run_id=:run AND workspace_id=:workspace",
            parameters=selection,
        ),
        "stop_events": guarded_rows(
            guards["agents"],
            "agents.run_events",
            (
                "run_id",
                "sequence",
                "kind",
                "operation_id",
                "status",
            ),
            where="run_id=:run AND operation_id=:stop AND kind LIKE 'stop.%'",
            parameters=selection,
        ),
    }


@pytest.mark.anyio
async def test_retained_original_task_link_uses_actual_cold_authenticated_route(
    selected_product_targets,
    tmp_path,
    retain_incomplete_product_work,
):
    targets, selected_guards, evidence = selected_product_targets
    guards = {guard.target.component: guard for guard in selected_guards}
    saved = retained_first_attempt(evidence)
    workspace, goal = UUID(saved["workspace_id"]), saved["original_goal"]
    before = retained_original_rows(guards, saved)
    facts = {
        "original_workspace_id": str(workspace),
        "before": before,
        "lifespan_started": False,
    }
    runtime = None
    complete = False
    native, secrets = ChatJourneyHarness(), ControlledSecretStore()
    try:
        components = {target.component: target for target in targets}
        settings = ProductSettings(
            **{
                name: components[component].url.render_as_string(hide_password=False)
                for name, component in (
                    ("product_database_url", "product"),
                    ("agent_database_url", "agents"),
                    ("cp_database_url", "cp"),
                    ("credential_database_url", "credentials"),
                )
            },
            deployment_namespace=UUID("898937f8-0a89-51ea-bfee-563b8524f4ad"),
            workspace_id=workspace,
            local_principal_id="owned-native-chat-user",
            bind_host="127.0.0.1",
            port=18080,
            web_origin="http://127.0.0.1:18080",
            workspace_root=tmp_path / "cold-workspaces",
            artifact_root=tmp_path / "cold-artifacts",
        )
        settings.workspace_root.mkdir(mode=0o700)
        settings.artifact_root.mkdir(mode=0o700)
        runtime = build_product_runtime(
            settings, harnesses=(native,), secret_store=secrets
        )
        app = create_app(runtime=runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=settings.local_url,
            headers={"Origin": settings.web_origin},
            trust_env=False,
        ) as browser:
            issue = runtime.local_sessions.create_bootstrap()
            exchanged = await browser.post(
                "/product/v1/session/bootstrap",
                json={"token": issue.token.get_secret_value()},
            )
            assert exchanged.status_code == 200, exchanged.json()
            browser.headers[CSRF_HEADER] = browser.cookies[CSRF_COOKIE]
            reply = await browser.get(
                f"/api/v1/tasks/{goal['root_task_id']}",
                params={"workspace_id": str(workspace)},
            )
            facts.update(
                task_read_status=reply.status_code,
                task_read_response=reply.json(),
                web_url=f"/workspaces/{workspace}/tasks/{goal['root_task_id']}",
            )
            write_evidence(evidence, "chat-retained-original-task-read", facts)
            assert reply.status_code == 200, reply.json()
            task = reply.json()
            assert task["id"] == goal["root_task_id"] and task["workspace_id"] == str(
                workspace
            )
            assert task["title"] == saved["original_goal_arguments"]["title"]
            assert runtime.status()["execution"]["ready"] is False
        after = retained_original_rows(guards, saved)
        facts["after"] = after
        assert after == before and all(
            len(after[key]) == 1
            for key in (
                "tasks",
                "commands",
                "receipts",
                "conversations",
                "runs",
                "turns",
            )
        )
        assert (
            after["runs"][0]["state"] == "stopped"
            and after["conversations"][0]["stop_state"] == "stopped"
        )
        assert any(row["kind"] == "stop.stopped" for row in after["stop_events"])
        assert native.calls == [] and secrets.reads == []
        facts["functional_complete"] = True
    finally:
        try:
            owner_closed = runtime is None or runtime.close(grace_seconds=0)
            facts.update(
                owner_close=owner_closed,
                native_calls=list(native.calls),
                secret_read_count=len(secrets.reads),
            )
            facts["original_rows_after_cold_disposal"] = retained_original_rows(
                guards, saved
            )
            cleanup_proven = (
                owner_closed
                and native.calls == []
                and facts["original_rows_after_cold_disposal"] == before
            )
            facts["cleanup_complete"] = cleanup_proven
            write_evidence(evidence, "chat-retained-original-task-final", facts)
            complete = cleanup_proven
        finally:
            if not complete:
                retain_incomplete_product_work((runtime, native, selected_guards))
                write_evidence(
                    evidence, "chat-retained-original-task-incomplete", facts
                )
        assert complete, (
            "Cold original Task read cleanup incomplete; resources retained"
        )


class ChatJourneyHarness(ControlledHarness):
    harness_id = "controlled-native-chat"

    def __init__(self):
        super().__init__()
        self.main_release = Event()
        self.main_entered = Event()
        self.runtime: ProductRuntime | None = None
        self.connections: dict[UUID, ToolConnection] = {}
        self.roles: dict[UUID, str] = {}
        self.events: dict[UUID, list[NativeEvent]] = {}
        self.inputs: list[dict[str, object]] = []

    def probe(self, candidate, checked_at):
        observed = super().probe(candidate, checked_at)
        return observed.model_copy(
            update={
                "capabilities": {
                    **observed.capabilities,
                    "mcp": CapabilitySupport(
                        status="supported",
                        reason_code="controlled-chat-sdk",
                        constraints={},
                    ),
                }
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
        role = next(role for role in ("main", "planner") if names == ROLE_TOOLS[role])
        assert self.runtime is not None
        verified = self.runtime.grants.verify(
            SecretStr(
                connection.headers["Authorization"]
                .get_secret_value()
                .removeprefix("Bearer ")
            )
        )
        assert verified.record.role == role and verified.run.run_id == request.run_id
        assert verified.run.state is RunState.STARTING
        denied = asyncio.run(sdk_exchange(connection, name=next(iter(sorted(names)))))
        assert denied.isError
        with self.lock:
            self.connections[request.run_id], self.roles[request.run_id] = (
                connection,
                role,
            )
        return handle

    def prompt(self, handle, assembled_input, response_id):
        with self.lock:
            role = self.roles[handle.run_id]
            self.calls.append(("prompt", handle.run_id, response_id))
            self.inputs.append(
                {
                    "run_id": str(handle.run_id),
                    "response_id": str(response_id),
                    "role": role,
                    "text": assembled_input.text,
                    "context_count": len(assembled_input.context),
                }
            )
            self.inspections[handle.run_id] = NativeInspection(
                process_state="alive", response_state="active", reason_code=None
            )
        if role == "main":
            self.main_entered.set()
            assert self.main_release.wait(FUNCTIONAL_SECONDS), (
                "Original chat response was not released"
            )
            self.emit(handle, response_id, "reply:" + assembled_input.text)
        else:
            self.emit(handle, response_id, controlled_proposal())
        with self.lock:
            self.inspections[handle.run_id] = NativeInspection(
                process_state="alive", response_state="idle", reason_code=None
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
            events = self.events.setdefault(handle.run_id, [])
            events.append(
                NativeEvent(
                    cursor=len(events) + 1,
                    native_id="owned-chat-message-" + str(len(events) + 1),
                    kind="message",
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
                for event in self.events.get(handle.run_id, ())
                if event.cursor > after_cursor
            )[:limit]

    def observe_original_close_completion(self, run_id: UUID) -> None:
        with self.lock:
            assert self.close_unknown and ("close", run_id) in self.calls
            assert self.inspections[run_id].process_state == "alive"
            self.inspections[run_id] = NativeInspection(
                process_state="gone", response_state="idle", reason_code=None
            )


@dataclass
class ChatLab:
    runtime: ProductRuntime
    native: ChatJourneyHarness
    secrets: ControlledSecretStore
    registered: RuntimeSnapshot
    server: LoopbackProduct
    knowledge: OwnedKnowledgeServer
    guards: dict[str, TargetGuard]
    evidence: Path | None
    facts: dict[str, object] = field(default_factory=dict)

    def wait_for[T](
        self, read: Callable[[], T], ready: Callable[[T], bool], stage: str
    ) -> T:
        deadline, wake = monotonic() + FUNCTIONAL_SECONDS, Event()
        while True:
            value = read()
            observation = (
                value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            )
            self.facts["last_stage"], self.facts["last_observation"] = (
                stage,
                observation,
            )
            if ready(value):
                return value
            if monotonic() >= deadline:
                write_evidence(self.evidence, "chat-original-stage-failure", self.facts)
                raise AssertionError("Original chat stage did not finish: " + stage)
            wake.wait(0.02)

    def owned_cleanup_facts(self) -> dict[str, object]:
        workspace = self.runtime.settings.workspace_id
        guard = self.guards["product"]
        guard.connection.rollback()
        try:
            assert (
                guard.connection.execute(
                    text("SHOW transaction_read_only")
                ).scalar_one()
                == "on"
            )
            assert verify_objects(guard.connection, guard.target)["heads"] == [
                "0003_conversations"
            ]
            conversations = [
                dict(row)
                for row in guard.connection.execute(
                    select(
                        ConversationRow.id,
                        ConversationRow.run_id,
                        ConversationRow.grant_id,
                        ConversationRow.start_operation_id,
                        ConversationRow.stop_operation_id,
                        ConversationRow.stop_state,
                        ConversationRow.active_turn_id,
                    ).where(
                        ConversationRow.workspace_id == workspace,
                        ConversationRow.principal_id
                        == self.runtime.settings.local_principal_id,
                    )
                ).mappings()
            ]
            turns = [
                dict(row)
                for row in guard.connection.execute(
                    select(
                        ChatTurnRow.id,
                        ChatTurnRow.conversation_id,
                        ChatTurnRow.operation_id,
                        ChatTurnRow.native_operation_id,
                        ChatTurnRow.request_state,
                        ChatTurnRow.response_id,
                        ChatTurnRow.dispatch_attempt,
                        ChatTurnRow.cached_view["status"]
                        .as_string()
                        .label("cached_status"),
                    ).where(
                        ChatTurnRow.workspace_id == workspace,
                        ChatTurnRow.principal_id
                        == self.runtime.settings.local_principal_id,
                    )
                ).mappings()
            ]
        finally:
            guard.connection.rollback()
        selection = {"workspace": workspace}
        runs = guarded_rows(
            self.guards["agents"],
            "agents.runs",
            (
                "run_id",
                "operation_id",
                "original_grant_id",
                "state",
                "installation_key",
                "terminal_state",
                "terminal_reason_code",
            ),
            where="workspace_id=:workspace",
            parameters=selection,
        )
        events = guarded_rows(
            self.guards["agents"],
            "agents.run_events",
            (
                "run_id",
                "sequence",
                "kind",
                "operation_id",
                "response_id",
                "status",
                "reason_code",
            ),
            where="run_id IN (SELECT run_id FROM agents.runs WHERE workspace_id=:workspace) AND kind LIKE 'stop.%'",
            parameters=selection,
        )
        installations = guarded_rows(
            self.guards["agents"],
            "agents.runtime_installations",
            (
                "installation_key",
                "harness_id",
                "max_active",
                "max_sessions",
                "active_count",
                "session_count",
            ),
            where="harness_id=:harness",
            parameters={"harness": self.native.harness_id},
        )
        reservations = guarded_rows(
            self.guards["agents"],
            "agents.run_reservations",
            (
                "run_id",
                "workspace_id",
                "installation_key",
                "active",
                "session",
            ),
            where="installation_key IN (SELECT installation_key FROM agents.runtime_installations WHERE harness_id=:harness)",
            parameters={"harness": self.native.harness_id},
        )
        return {
            "conversations": conversations,
            "turns": turns,
            "native_runs": runs,
            "original_stop_events": events,
            "installations": installations,
            "reservations": reservations,
        }

    def planner_processes_closed(self) -> bool:
        with self.native.lock:
            run_ids = [
                run_id
                for run_id, role in self.native.roles.items()
                if role == "planner"
            ]
            return bool(run_ids) and all(
                self.native.inspections[run_id].process_state == "gone"
                for run_id in run_ids
            )


@pytest.fixture
def chat_api_lab(selected_product_targets, tmp_path, retain_incomplete_product_work):
    targets, selected_guards, evidence = selected_product_targets
    guards = {guard.target.component: guard for guard in selected_guards}
    account_outstanding(guards, evidence, "n19-original-work-before-chat-background")
    listener = knowledge = native = runtime = server = lab = None
    facts: dict[str, object] = {}
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        workspace, port = uuid4(), listener.getsockname()[1]
        facts["workspace_id"] = str(workspace)
        knowledge = OwnedKnowledgeServer(workspace)
        knowledge.start()
        by_component = {target.component: target for target in targets}
        settings = ProductSettings(
            **{
                name: by_component[component].url.render_as_string(hide_password=False)
                for name, component in (
                    ("product_database_url", "product"),
                    ("agent_database_url", "agents"),
                    ("cp_database_url", "cp"),
                    ("credential_database_url", "credentials"),
                )
            },
            deployment_namespace=UUID("898937f8-0a89-51ea-bfee-563b8524f4ad"),
            workspace_id=workspace,
            local_principal_id="owned-native-chat-user",
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
        native, secrets = ChatJourneyHarness(), ControlledSecretStore()
        runtime = build_product_runtime(
            settings, harnesses=(native,), secret_store=secrets
        )
        native.runtime = runtime
        issue = runtime.local_sessions.create_bootstrap()
        human = runtime.local_sessions.exchange(issue.token)
        registered = runtime.agents.runtimes.register(
            workspace,
            native.harness_id,
            sys.executable,
            max_active=1,
            max_sessions=2,
            grant_id=human.session_id,
        )
        facts["cold_runtime_registration"] = registered.model_dump(mode="json")
        facts["cold_human_grant_id"] = str(human.session_id)
        server = LoopbackProduct(create_app(runtime=runtime), listener)
        lab = ChatLab(
            runtime=runtime,
            native=native,
            secrets=secrets,
            registered=registered,
            server=server,
            knowledge=knowledge,
            guards=guards,
            evidence=evidence,
            facts=facts,
        )
        yield lab
    finally:
        complete = False
        try:
            if native is not None:
                native.main_release.set()
            owner_complete = runtime is None or runtime.close(grace_seconds=10)
            server_complete = owner_complete and (server is None or server.close())
            if server is None and listener is not None and owner_complete:
                listener.close()
            knowledge_complete = server_complete and (
                knowledge is None or knowledge.close()
            )
            facts.update(
                owner_close=owner_complete,
                http_closed=server_complete,
                knowledge_closed=knowledge_complete,
            )
            if lab is not None:
                facts["native_calls"] = list(native.calls)
                facts["native_inputs"] = list(native.inputs)
                facts["final_durable_cleanup"] = lab.owned_cleanup_facts()
            facts["owner_threads"] = [
                thread.name
                for thread in enumerate_threads()
                if thread.name == "sidera-agent-execution"
                or thread.name.startswith("sidera-native-")
            ]
            facts["external_knowledge"] = (
                None if knowledge is None else knowledge.facts()
            )
            cleanup_proven = (
                owner_complete
                and server_complete
                and knowledge_complete
                and not facts["owner_threads"]
            )
            if lab is not None and native.handles:
                durable = facts["final_durable_cleanup"]
                cleanup_proven = cleanup_proven and all(
                    not row["active"] and not row["session"]
                    for row in durable["reservations"]
                )
                cleanup_proven = cleanup_proven and all(
                    row["active_count"] == row["session_count"] == 0
                    for row in durable["installations"]
                )
                cleanup_proven = cleanup_proven and all(
                    row["run_id"] is None or row["stop_state"] == "stopped"
                    for row in durable["conversations"]
                )
            facts["cleanup_complete"] = cleanup_proven
            write_evidence(evidence, "chat-functional-and-cleanup", facts)
            complete = cleanup_proven
        finally:
            if not complete:
                retain_incomplete_product_work(
                    (lab, runtime, native, server, knowledge, listener, selected_guards)
                )
                write_evidence(evidence, "chat-incomplete-originals-retained", facts)
        assert complete, "Original chat cleanup incomplete; owners and guards retained"


def test_native_chat_two_replies_original_receipt_and_confirmed_cleanup(
    chat_api_lab,
    monkeypatch,
):
    lab = chat_api_lab
    runtime, workspace = lab.runtime, lab.runtime.settings.workspace_id
    lab.server.start()
    chat_prefix, cp_prefix = "/main-agent/v1", f"/api/v1/workspaces/{workspace}"
    with browser_session(lab) as (browser, session_id):
        status = get_json(browser, "/product/v1/status")
        health = get_json(browser, f"{chat_prefix}/health")
        capabilities = get_json(browser, f"{chat_prefix}/capabilities")
        lab.facts.update(
            status=status,
            chat_health=health,
            capabilities=capabilities,
            session_id=str(session_id),
        )
        assert status["execution"]["ready"] is True and status["main_agent"] == {
            "available": True,
            "code": None,
        }
        assert (
            health["provider"] == "available"
            and capabilities["turn_interrupt"] is False
        )
        registered = lab.registered
        installations = guarded_rows(
            lab.guards["agents"],
            "agents.runtime_installations",
            (
                "installation_key",
                "harness_id",
                "max_active",
                "max_sessions",
                "active_count",
                "session_count",
            ),
            where="harness_id=:harness",
            parameters={"harness": lab.native.harness_id},
        )
        lab.facts.update(
            runtime_registration=registered.model_dump(mode="json"),
            actual_installations_before_admission=installations,
        )
        assert (
            registered.harness_id == "controlled-native-chat"
            and registered.limits == {"max_active": 1, "max_sessions": 2}
        )
        assert (
            len(installations) == 1
            and installations[0]["max_active"] == 1
            and installations[0]["max_sessions"] == 2
        )
        assert (
            installations[0]["active_count"] == installations[0]["session_count"] == 0
        )
        resource = runtime.agents.catalog.create_resource(
            workspace,
            CapabilityResourceDraft(
                name="Owned chat task bridge",
                kind="mcp",
                settings={"connection_id": str(runtime.grants.connection_id)},
                secret_refs=(),
            ),
            grant_id=session_id,
        )
        main_profile = runtime.agents.catalog.create_profile(
            workspace,
            profile_draft(registered.ref, resource.ref, "Owned chat Main"),
            grant_id=session_id,
        )
        planner_profile = runtime.agents.catalog.create_profile(
            workspace,
            profile_draft(registered.ref, resource.ref, "Owned chat Planner"),
            grant_id=session_id,
        )
        repo = repository(runtime.settings.workspace_root / "repository")
        setup_reply = browser.post(
            f"/product/v1/workspaces/{workspace}/setup",
            json={
                "repository_directory": str(repo),
                "expected_version": 0,
                "planner_agent_ref": planner_profile.ref.model_dump(mode="json"),
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
        lab.facts["workspace_setup"] = setup_reply.json()
        assert setup_reply.status_code == 200, setup_reply.json()
        defaults = runtime.product_store.write_defaults(
            workspace,
            expected_version=0,
            main_profile_id=main_profile.ref.profile_id,
            planner_profile_id=planner_profile.ref.profile_id,
            worker_profile_id=main_profile.ref.profile_id,
        )
        lab.facts.update(
            defaults_version=defaults.version,
            main_profile=main_profile.ref.model_dump(mode="json"),
            planner_profile=planner_profile.ref.model_dump(mode="json"),
        )
        missing_csrf = browser.build_request("POST", f"{chat_prefix}/threads", json={})
        missing_csrf.headers.pop(CSRF_HEADER)
        denied_csrf = browser.send(missing_csrf)
        denied_origin = browser.post(
            f"{chat_prefix}/threads", json={}, headers={"Origin": "http://127.0.0.1:1"}
        )
        with httpx.Client(
            base_url=runtime.settings.local_url, trust_env=False
        ) as anonymous:
            denied_anonymous = anonymous.post(
                f"{chat_prefix}/threads",
                json={},
                headers={"Origin": runtime.settings.web_origin},
            )
        lab.facts["boundary_denials"] = [
            reply.json() for reply in (denied_csrf, denied_origin, denied_anonymous)
        ]
        assert (
            denied_csrf.status_code == denied_origin.status_code == 403
            and denied_anonymous.status_code == 401
        )
        created = browser.post(
            f"{chat_prefix}/threads", json={"title": "Owned persistent native chat"}
        )
        assert created.status_code == 201, created.json()
        thread = created.json()
        thread_id, turn_prefix = (
            UUID(thread["id"]),
            f"{chat_prefix}/threads/{thread['id']}/turns",
        )
        lab.facts["empty_thread"] = thread
        assert (
            thread["profileRef"] == main_profile.ref.model_dump(mode="json")
            and thread["runId"] is None
            and thread["turns"] == []
        )
        assert (
            not any(call[0] in {"open", "prompt"} for call in lab.native.calls)
            and lab.secrets.reads == []
        )

        original_claim, original_start = (
            runtime.chat.store.claim_turn,
            runtime.execution.start,
        )
        admission_barrier, original_started = Barrier(2, timeout=10), []
        operation, first_text = (
            "browser-chat-original-not-a-uuid",
            "First controlled native reply",
        )
        first_body = {"input": first_text, "operationId": operation}

        def competing_original_claim(conversation, **request):
            if conversation.id == thread_id and request["operation_id"] == operation:
                admission_barrier.wait()
            return original_claim(conversation, **request)

        def lose_committed_original_start(request):
            receipt = original_start(request)
            original_started.append(receipt.model_dump(mode="json"))
            assert receipt.status is StartStatus.ACCEPTED and receipt.run_id is not None
            raise OSError("Owned original native reply lost after durable admission")

        monkeypatch.setattr(runtime.chat.store, "claim_turn", competing_original_claim)
        monkeypatch.setattr(runtime.execution, "start", lose_committed_original_start)
        try:
            with ThreadPoolExecutor(max_workers=2) as callers:
                calls = [
                    callers.submit(browser.post, turn_prefix, json=first_body)
                    for _ in range(2)
                ]
                replies = [call.result(timeout=FUNCTIONAL_SECONDS) for call in calls]
        finally:
            monkeypatch.setattr(runtime.chat.store, "claim_turn", original_claim)
            monkeypatch.setattr(runtime.execution, "start", original_start)
        lab.facts.update(
            concurrent_original_replies=[
                {"status": reply.status_code, "body": reply.json()} for reply in replies
            ],
            committed_lost_start_receipts=original_started,
        )
        assert all(reply.status_code == 202 for reply in replies), lab.facts[
            "concurrent_original_replies"
        ]
        first_id = UUID(replies[0].json()["id"])
        assert (
            all(reply.json()["id"] == str(first_id) for reply in replies)
            and len(original_started) == 1
        )
        first_path = f"{turn_prefix}/{first_id}"
        first_active = lab.wait_for(
            lambda: get_json(browser, first_path),
            lambda value: value["status"] in {"in_progress", "failed"},
            "first original response active",
        )
        lab.facts["first_active"] = first_active
        assert first_active["status"] == "in_progress", first_active
        assert lab.native.main_entered.wait(20)
        conversation = runtime.chat.store.get_conversation(
            conversation_id=thread_id,
            workspace_id=workspace,
            principal_id=runtime.settings.local_principal_id,
        )
        assert (
            conversation is not None
            and conversation.grant_id is not None
            and conversation.run_id is not None
        )
        run_id, grant_id = conversation.run_id, conversation.grant_id
        first_run = runtime.execution.get_run(workspace, run_id, grant_id=grant_id)
        original_rows = lab.owned_cleanup_facts()
        lab.facts.update(
            first_run=first_run.model_dump(mode="json"), original_rows=original_rows
        )
        assert len(original_rows["turns"]) == 1 and original_rows["turns"][0][
            "native_operation_id"
        ] == uuid5(thread_id, "sidera.chat.input:" + operation)
        assert (
            first_run.state is RunState.RUNNING
            and first_run.profile_ref == main_profile.ref
        )
        duplicate = browser.post(turn_prefix, json=first_body)
        changed = browser.post(
            turn_prefix, json={**first_body, "input": "Changed original payload"}
        )
        lab.facts.update(
            duplicate_original=duplicate.json(), changed_original=changed.json()
        )
        assert duplicate.status_code == 202 and duplicate.json()["id"] == str(first_id)
        assert (
            changed.status_code == 409
            and changed.json()["error"]["code"] == "operation_conflict"
        )
        assert len(original_started) == 1

        main_connection = lab.native.connections[run_id]
        arguments = goal_arguments()
        goal = sdk_call(main_connection, "sidera_submit_goal", arguments)
        goal = lab.wait_for(
            lambda: (
                goal
                if goal["outcome"] in {"applied", "rejected"}
                else sdk_call(main_connection, "sidera_submit_goal", arguments)
            ),
            lambda result: result["outcome"] in {"applied", "rejected"},
            "original task-tool receipt",
        )
        lab.facts.update(original_goal_arguments=arguments, original_goal=goal)
        assert (
            goal["outcome"] == "applied"
            and goal["root_task_id"] is not None
            and goal["planning_request_id"] is not None
        ), goal
        receipt_view = get_json(browser, first_path)
        receipt_items = [
            item
            for item in receipt_view["items"]
            if item["type"] == "toolCall"
            and item["data"].get("receiptSource") == "sidera.product.task-receipt.v1"
        ]
        receipt_rows = runtime.authority_store.list_receipts(
            grant_id=grant_id,
            run_id=run_id,
            response_id=UUID(first_active["responseId"]),
        )
        lab.facts.update(
            first_receipt_view=receipt_view,
            actual_receipt_ids=[str(row.receipt_id) for row in receipt_rows],
        )
        assert len(receipt_items) == len(receipt_rows) == 1
        card = receipt_items[0]["data"]
        assert (
            card["verified"] is True
            and card["commandId"] == goal["command_id"]
            and card["receiptId"] == str(receipt_rows[0].receipt_id)
        )
        assert (
            card["rootTaskId"] == goal["root_task_id"]
            and card["planningRequestId"] == goal["planning_request_id"]
        )
        assert (
            card["runId"] == str(run_id)
            and card["responseId"] == first_active["responseId"]
        )
        linked_reply = browser.get(
            f"/api/v1/tasks/{goal['root_task_id']}",
            params={"workspace_id": str(workspace)},
        )
        assert linked_reply.status_code == 200, linked_reply.json()
        linked_task = linked_reply.json()
        assert linked_task["id"] == goal["root_task_id"]
        assert linked_task["workspace_id"] == str(workspace)
        lab.facts["linked_original_task"] = linked_task
        assert card["webUrl"] == f"/workspaces/{workspace}/tasks/{goal['root_task_id']}"

        lab.native.main_release.set()
        first_completed = lab.wait_for(
            lambda: get_json(browser, first_path),
            lambda value: value["status"] in {"completed", "failed"},
            "first original reply completed",
        )
        lab.facts["first_completed"] = first_completed
        assert (
            first_completed["status"] == "completed"
            and first_completed["finalResponse"] == "reply:" + first_text
            and first_completed["usage"] is None
        )
        idle = lab.wait_for(
            lambda: runtime.execution.get_run(workspace, run_id, grant_id=grant_id),
            lambda run: run.state is RunState.IDLE,
            "first reply leaves original Run idle",
        )
        lab.facts["first_idle_run"] = idle.model_dump(mode="json")
        assert (
            idle.input_open
            and lab.native.inspect(lab.native.handles[run_id]).process_state == "alive"
        )
        assert ("close", run_id) not in lab.native.calls
        with httpx.Client(trust_env=False) as tool_client:
            idle_tools = tool_client.post(
                main_connection.endpoint_or_argv,
                headers={
                    **{
                        name: value.get_secret_value()
                        for name, value in main_connection.headers.items()
                    },
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "owned-idle-denial", "version": "1"},
                    },
                },
            )
        lab.facts["idle_mcp_denial_status"] = idle_tools.status_code
        assert idle_tools.status_code == 401
        idle_history = get_json(browser, first_path)
        assert idle_history["status"] == "completed" and any(
            item["data"].get("verified") is True
            for item in idle_history["items"]
            if item["type"] == "toolCall"
        )
        planner_detail = lab.wait_for(
            lambda: get_json(
                browser, f"{cp_prefix}/planning-requests/{goal['planning_request_id']}"
            ),
            lambda value: value["status"] in {"proposal_ready", "failed"},
            "original goal Planner result",
        )
        lab.facts["original_planner_detail"] = planner_detail
        assert planner_detail["status"] == "proposal_ready", planner_detail
        lab.wait_for(
            lab.planner_processes_closed,
            bool,
            "original Planner native session released",
        )

        assert len(original_started) == 1
        prove_continuation_history_and_original_close(
            lab,
            browser,
            monkeypatch=monkeypatch,
            session_id=session_id,
            thread_id=thread_id,
            profile_ref=main_profile.ref,
            turn_prefix=turn_prefix,
            first_path=first_path,
            first_completed=first_completed,
            run_id=run_id,
            grant_id=grant_id,
            first_text=first_text,
        )


def test_remaining_chat_two_replies_same_run_and_original_close(
    chat_api_lab,
    monkeypatch,
):
    lab = chat_api_lab
    saved = retained_first_attempt(lab.evidence)
    old_rows = retained_original_rows(lab.guards, saved)
    lab.facts["retained_original_rows_before_fresh_chat"] = old_rows
    runtime, workspace = lab.runtime, lab.runtime.settings.workspace_id
    assert str(workspace) != saved["workspace_id"]
    lab.server.start()
    with browser_session(lab) as (browser, session_id):
        resource = runtime.agents.catalog.create_resource(
            workspace,
            CapabilityResourceDraft(
                name="Owned remaining chat task bridge",
                kind="mcp",
                settings={"connection_id": str(runtime.grants.connection_id)},
                secret_refs=(),
            ),
            grant_id=session_id,
        )
        profile = runtime.agents.catalog.create_profile(
            workspace,
            profile_draft(
                lab.registered.ref, resource.ref, "Owned remaining chat Main"
            ),
            grant_id=session_id,
        )
        runtime.product_store.write_defaults(
            workspace,
            expected_version=0,
            main_profile_id=profile.ref.profile_id,
            planner_profile_id=None,
            worker_profile_id=None,
        )
        created = browser.post(
            "/main-agent/v1/threads", json={"title": "Only unreached native chat proof"}
        )
        assert created.status_code == 201, created.json()
        thread = created.json()
        thread_id = UUID(thread["id"])
        turn_prefix = f"/main-agent/v1/threads/{thread_id}/turns"
        first_text, operation = (
            "First remaining controlled reply",
            "remaining-chat-not-a-uuid",
        )
        reply = browser.post(
            turn_prefix, json={"input": first_text, "operationId": operation}
        )
        lab.facts.update(
            coverage="unreached-only-no-goal-or-planner-setup",
            empty_thread=thread,
            first_admission=reply.json(),
        )
        assert reply.status_code == 202, reply.json()
        first_id = UUID(reply.json()["id"])
        first_path = f"{turn_prefix}/{first_id}"
        first_active = lab.wait_for(
            lambda: get_json(browser, first_path),
            lambda value: value["status"] in {"in_progress", "failed"},
            "remaining first response active",
        )
        lab.facts["first_active"] = first_active
        assert first_active["status"] == "in_progress", first_active
        assert lab.native.main_entered.wait(20)
        conversation = runtime.chat.store.get_conversation(
            conversation_id=thread_id,
            workspace_id=workspace,
            principal_id=runtime.settings.local_principal_id,
        )
        assert (
            conversation is not None
            and conversation.run_id is not None
            and conversation.grant_id is not None
        )
        run_id, grant_id = conversation.run_id, conversation.grant_id
        assert (
            str(run_id) != saved["first_run"]["run_id"]
            and conversation.profile_ref == profile.ref
        )
        first_rows = lab.owned_cleanup_facts()
        lab.facts["first_remaining_rows"] = first_rows
        assert len(first_rows["turns"]) == 1 and first_rows["turns"][0][
            "native_operation_id"
        ] == uuid5(thread_id, "sidera.chat.input:" + operation)
        lab.native.main_release.set()
        first_completed = lab.wait_for(
            lambda: get_json(browser, first_path),
            lambda value: value["status"] in {"completed", "failed"},
            "remaining first reply completed",
        )
        idle = lab.wait_for(
            lambda: runtime.execution.get_run(workspace, run_id, grant_id=grant_id),
            lambda run: run.state is RunState.IDLE,
            "remaining first reply leaves original Run idle",
        )
        lab.facts.update(
            first_completed=first_completed, first_idle_run=idle.model_dump(mode="json")
        )
        assert (
            first_completed["status"] == "completed"
            and first_completed["finalResponse"] == "reply:" + first_text
            and first_completed["usage"] is None
        )
        assert first_completed["responseId"] == first_active["responseId"]
        assert (
            idle.input_open
            and idle.profile_ref == profile.ref
            and lab.native.inspect(lab.native.handles[run_id]).process_state == "alive"
        )
        assert ("close", run_id) not in lab.native.calls
        assert not any(
            item["data"].get("verified") is True for item in first_completed["items"]
        )
        prove_continuation_history_and_original_close(
            lab,
            browser,
            monkeypatch=monkeypatch,
            session_id=session_id,
            thread_id=thread_id,
            profile_ref=profile.ref,
            turn_prefix=turn_prefix,
            first_path=first_path,
            first_completed=first_completed,
            run_id=run_id,
            grant_id=grant_id,
            first_text=first_text,
        )
        after_old_rows = retained_original_rows(lab.guards, saved)
        lab.facts["retained_original_rows_after_fresh_chat"] = after_old_rows
        write_evidence(lab.evidence, "chat-disjoint-remaining-proof", lab.facts)
        assert after_old_rows == old_rows
        assert (
            set(lab.native.roles.values()) == {"main"} and len(lab.native.inputs) == 2
        )


def prove_continuation_history_and_original_close(
    lab: ChatLab,
    browser: httpx.Client,
    *,
    monkeypatch: pytest.MonkeyPatch,
    session_id: UUID,
    thread_id: UUID,
    profile_ref: ProfileRef,
    turn_prefix: str,
    first_path: str,
    first_completed: dict[str, object],
    run_id: UUID,
    grant_id: UUID,
    first_text: str,
):
    runtime, workspace = lab.runtime, lab.runtime.settings.workspace_id
    chat_prefix = "/main-agent/v1"
    lab.native.main_release.clear()
    lab.native.main_entered.clear()
    second_text, second_operation = (
        "Second reply without transcript replay",
        str(uuid4()),
    )
    second_reply = browser.post(
        turn_prefix, json={"input": second_text, "operationId": second_operation}
    )
    lab.facts["second_admission"] = second_reply.json()
    assert second_reply.status_code == 202, second_reply.json()
    second_id = UUID(second_reply.json()["id"])
    second_path = f"{turn_prefix}/{second_id}"
    second_active = lab.wait_for(
        lambda: get_json(browser, second_path),
        lambda value: value["status"] in {"in_progress", "failed"},
        "second same-Run response active",
    )
    lab.facts["second_active"] = second_active
    assert second_active["status"] == "in_progress" and second_active["runId"] == str(
        run_id
    )
    assert (
        second_active["responseId"] != first_completed["responseId"]
        and second_active["operationId"] == second_operation
    )
    assert lab.native.main_entered.wait(20)
    lab.native.main_release.set()
    second_completed = lab.wait_for(
        lambda: get_json(browser, second_path),
        lambda value: value["status"] in {"completed", "failed"},
        "second same-Run reply completed",
    )
    idle_again = lab.wait_for(
        lambda: runtime.execution.get_run(workspace, run_id, grant_id=grant_id),
        lambda run: run.state is RunState.IDLE,
        "second reply retains original idle Run",
    )
    durable_turns = lab.owned_cleanup_facts()
    main_inputs = [
        input for input in lab.native.inputs if input["run_id"] == str(run_id)
    ]
    main_opens = [call for call in lab.native.calls if call == ("open", run_id)]
    lab.facts.update(
        second_completed=second_completed,
        second_idle_run=idle_again.model_dump(mode="json"),
        two_reply_durable_rows=durable_turns,
        original_main_inputs=main_inputs,
        original_main_open_count=len(main_opens),
    )
    assert (
        second_completed["status"] == "completed"
        and second_completed["finalResponse"] == "reply:" + second_text
        and second_completed["usage"] is None
    )
    assert idle_again.input_open and len(main_opens) == 1 and len(main_inputs) == 2
    assert [input["text"] for input in main_inputs] == [
        first_text,
        second_text,
    ] and all(input["context_count"] == 0 for input in main_inputs)
    assert len(durable_turns["turns"]) == 2 and all(
        turn["dispatch_attempt"] == 1 and turn["cached_status"] == "completed"
        for turn in durable_turns["turns"]
    )
    second_saved = next(
        turn for turn in durable_turns["turns"] if turn["id"] == second_id
    )
    assert second_saved["native_operation_id"] == uuid5(
        thread_id, "sidera.chat.input:" + second_operation
    )
    assert durable_turns["conversations"][0]["active_turn_id"] is None
    refreshed = get_json(browser, f"{chat_prefix}/threads/{thread_id}")
    lab.facts["refreshed_saved_thread"] = refreshed
    assert refreshed["runId"] == str(run_id) and refreshed[
        "profileRef"
    ] == profile_ref.model_dump(mode="json")
    assert (
        len(refreshed["turns"]) == 2
        and refreshed["turns"][0]["responseId"] == first_completed["responseId"]
    )

    native_events = runtime.execution.list_events(
        workspace, run_id, 0, 500, grant_id=grant_id
    ).events
    event_reply = browser.get(f"{first_path}/events")
    first_sequences = [
        int(line.removeprefix("id: "))
        for line in event_reply.text.splitlines()
        if line.startswith("id: ")
    ]
    native_sequences = {
        event.sequence
        for event in native_events
        if event.response_id == UUID(first_completed["responseId"])
    }
    lab.facts.update(
        first_sse_sequences=first_sequences,
        native_response_sequences=sorted(native_sequences),
        native_event_head=idle_again.last_event_sequence,
    )
    assert (
        event_reply.status_code == 200
        and first_sequences
        and first_sequences == sorted(set(first_sequences))
    )
    assert set(first_sequences) <= native_sequences
    assert "product-receipt:" not in event_reply.text
    resumed = browser.get(
        f"{first_path}/events",
        params={"after": 0},
        headers={"Last-Event-ID": str(first_sequences[0])},
    )
    resumed_sequences = [
        int(line.removeprefix("id: "))
        for line in resumed.text.splitlines()
        if line.startswith("id: ")
    ]
    future = browser.get(
        f"{first_path}/events", params={"after": idle_again.last_event_sequence + 1}
    )
    lab.facts.update(
        resumed_sse_sequences=resumed_sequences, future_sse_resync=future.text
    )
    assert resumed.status_code == 200 and resumed_sequences == first_sequences[1:]
    assert (
        future.status_code == 200
        and "event: resync_required" in future.text
        and "id: " not in future.text
    )
    assert len(main_opens) == 1

    with browser_session(lab) as (current_browser, current_session):
        current_history = get_json(current_browser, first_path)
        assert (
            current_session != session_id
            and current_history["responseId"] == first_completed["responseId"]
        )
        current_before = []
        with monkeypatch.context() as history_boundary:
            for name in ("get_start", "get_run", "list_events"):
                original_read = getattr(runtime.execution, name)

                def record_native_read(
                    *args, _original=original_read, _name=name, **kwargs
                ):
                    if kwargs.get("grant_id") == grant_id:
                        current_before.append(_name)
                    return _original(*args, **kwargs)

                history_boundary.setattr(runtime.execution, name, record_native_read)
            original_receipt_read = runtime.authority_store.list_receipts

            def record_receipt_read(**selection):
                if selection.get("grant_id") == grant_id:
                    current_before.append("list_receipts")
                return original_receipt_read(**selection)

            history_boundary.setattr(
                runtime.authority_store, "list_receipts", record_receipt_read
            )
            runtime.local_sessions.revoke(session_id)
            unavailable = get_json(current_browser, first_path)
        lab.facts.update(
            current_session_id=str(current_session),
            revoked_original_history=unavailable,
            revoked_history_native_receipt_reads=list(current_before),
        )
        assert unavailable["error"]["code"] == "permission_denied" and [
            item["type"] for item in unavailable["items"]
        ] == ["userMessage"]
        assert current_before == [] and all(
            item["data"].get("verified") is not True for item in unavailable["items"]
        )

    lab.native.close_unknown = True
    pending_close = runtime.close(grace_seconds=2)
    pending_durable = lab.owned_cleanup_facts()
    lab.facts.update(
        first_product_close=pending_close,
        first_close_status=runtime.status(),
        original_pending_close_rows=pending_durable,
    )
    write_evidence(lab.evidence, "chat-original-close-unknown", lab.facts)
    assert (
        pending_close is False and runtime.status()["lifecycle"] == "cleanup_incomplete"
    )
    original_stop = pending_durable["conversations"][0]["stop_operation_id"]
    assert (
        original_stop is not None
        and pending_durable["conversations"][0]["stop_state"] == "unknown"
    )
    assert lab.native.inspect(lab.native.handles[run_id]).process_state == "alive"
    close_calls = [call for call in lab.native.calls if call == ("close", run_id)]
    assert len(close_calls) == 1
    lab.native.observe_original_close_completion(run_id)
    lab.facts["controlled_original_close_completion"] = {
        "run_id": str(run_id),
        "original_stop_operation_id": str(original_stop),
        "evidence": "controlled-external-completion-after-closeFalse",
        "real_provider": False,
    }
    confirmed_close = runtime.close(grace_seconds=10)
    confirmed_rows = lab.owned_cleanup_facts()
    lab.facts.update(
        confirmed_product_close=confirmed_close,
        confirmed_close_status=runtime.status(),
        confirmed_durable_rows=confirmed_rows,
    )
    write_evidence(lab.evidence, "chat-original-close-confirmed", lab.facts)
    assert confirmed_close is True and runtime.status()["lifecycle"] == "closed"
    assert (
        confirmed_rows["conversations"][0]["stop_operation_id"] == original_stop
        and confirmed_rows["conversations"][0]["stop_state"] == "stopped"
    )
    main_run_row = next(
        row for row in confirmed_rows["native_runs"] if row["run_id"] == str(run_id)
    )
    assert main_run_row["state"] == "stopped" and all(
        not row["active"] and not row["session"]
        for row in confirmed_rows["reservations"]
    )
    original_stop_events = [
        row
        for row in confirmed_rows["original_stop_events"]
        if row["operation_id"] == str(original_stop)
    ]
    assert any(row["kind"] == "stop.stopped" for row in original_stop_events)
    assert {
        row["operation_id"]
        for row in confirmed_rows["original_stop_events"]
        if row["run_id"] == str(run_id)
    } == {str(original_stop)}
    assert all(
        row["active_count"] == row["session_count"] == 0
        for row in confirmed_rows["installations"]
    )
    assert all(row["cached_status"] == "completed" for row in confirmed_rows["turns"])
    assert [
        call for call in lab.native.calls if call == ("close", run_id)
    ] == close_calls
    lab.facts["functional_complete"] = True


class RetainedOriginalExitInspector(ControlledHarness):
    harness_id = "controlled-native-chat"

    def __init__(self, run_id: UUID):
        super().__init__()
        self.original_run_id = run_id
        self.inspected_handles: list[dict[str, object]] = []

    def inspect(self, handle: NativeHandle):
        assert handle.run_id == self.original_run_id
        assert handle.session_id == "controlled-" + str(self.original_run_id)
        assert handle.process_identity.pid == 1
        assert (
            handle.process_identity.start_identity
            == "no-os-process-controlled-boundary"
        )
        assert handle.process_identity.owned_nonce.int != 0
        self.inspected_handles.append(handle.model_dump(mode="json"))
        return NativeInspection(
            process_state="gone", response_state="idle", reason_code=None
        )

    def forbidden(self, operation):
        self.calls.append((operation,))
        raise AssertionError("Retained original exit proof forbids native mutation")

    def probe(self, candidate, checked_at):
        return self.forbidden("probe")

    def open(self, request):
        return self.forbidden("open")

    def prompt(self, handle, input, response_id):
        return self.forbidden("prompt")

    def read_events(self, handle, after_cursor, limit=100):
        return self.forbidden("read_events")

    def cancel(self, handle, response_id):
        return self.forbidden("cancel")

    def close(self, handle):
        return self.forbidden("close")

    def recover(self, *args, **kwargs):
        return self.forbidden("recover")


def unrelated_original_cleanup_fingerprints(guards, *, conversation_id, run_id):
    result = {}
    for component, guard in guards.items():
        guard.connection.rollback()
        try:
            assert (
                guard.connection.execute(
                    text("SHOW transaction_read_only")
                ).scalar_one()
                == "on"
            )
            inventory = verify_objects(guard.connection, guard.target)
            tables = {}
            for relation in inventory["relations"]:
                if (
                    relation["kind"] not in {"r", "p"}
                    or relation["name"] == "alembic_version"
                ):
                    continue
                schema, name = relation["schema"], relation["name"]
                where = "TRUE"
                if component == "product" and name == "conversations":
                    where = "id <> :conversation"
                elif component == "agents" and name in {
                    "runs",
                    "run_operations",
                    "run_events",
                    "run_inputs",
                    "run_reservations",
                }:
                    where = "run_id IS NULL OR run_id <> :run"
                quote = guard.connection.dialect.identifier_preparer.quote
                projection = "to_jsonb(original)"
                if component == "product" and name == "browser_sessions":
                    projection += " - 'csrf_digest'"
                rows = sorted(
                    guard.connection.execute(
                        text(
                            f"SELECT ({projection})::text FROM {quote(schema)}.{quote(name)} AS original WHERE {where}"
                        ),
                        {"conversation": conversation_id, "run": run_id},
                    ).scalars()
                )
                digest = hashlib.sha256()
                for row in rows:
                    encoded = row.encode("utf-8")
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
                tables[f"{schema}.{name}"] = {
                    "rows": len(rows),
                    "sha256": digest.hexdigest(),
                }
            result[component] = tables
        finally:
            guard.connection.rollback()
    return result


def retained_unknown_cleanup_rows(guards, *, workspace, conversation, run, stop):
    parameters = {
        "workspace": workspace,
        "conversation": conversation,
        "run": run,
        "stop": stop,
    }
    turns = guarded_rows(
        guards["product"],
        "product.chat_turns",
        (
            "id",
            "operation_id",
            "native_operation_id",
            "response_id",
            "request_state",
            "dispatch_attempt",
            "cached_view",
        ),
        where="workspace_id=:workspace AND conversation_id=:conversation",
        parameters=parameters,
    )
    for turn in turns:
        cached = turn.pop("cached_view")
        assert isinstance(cached, dict)
        turn["cached_status"] = cached["status"]
    return {
        "conversations": guarded_rows(
            guards["product"],
            "product.conversations",
            (
                "id",
                "workspace_id",
                "principal_id",
                "profile_ref",
                "created_at",
                "updated_at",
                "version",
                "active_turn_id",
                "run_id",
                "grant_id",
                "start_operation_id",
                "stop_operation_id",
                "stop_state",
            ),
            where="workspace_id=:workspace AND id=:conversation",
            parameters=parameters,
        ),
        "turns": sorted(turns, key=lambda turn: turn["id"]),
        "runs": guarded_rows(
            guards["agents"],
            "agents.runs",
            (
                "run_id",
                "workspace_id",
                "operation_id",
                "original_grant_id",
                "profile_id",
                "profile_revision",
                "runtime_workspace_id",
                "runtime_id",
                "installation_key",
                "native_handle",
                "state",
                "terminal_state",
            ),
            where="workspace_id=:workspace AND run_id=:run",
            parameters=parameters,
        ),
        "stop_events": sorted(
            guarded_rows(
                guards["agents"],
                "agents.run_events",
                (
                    "run_id",
                    "sequence",
                    "kind",
                    "operation_id",
                    "response_id",
                    "status",
                    "reason_code",
                ),
                where="run_id=:run AND operation_id=:stop AND kind LIKE 'stop.%'",
                parameters=parameters,
            ),
            key=lambda event: event["sequence"],
        ),
        "all_events": sorted(
            guarded_rows(
                guards["agents"],
                "agents.run_events",
                (
                    "run_id",
                    "sequence",
                    "kind",
                    "operation_id",
                    "response_id",
                    "status",
                    "reason_code",
                    "native_cursor",
                ),
                where="run_id=:run",
                parameters=parameters,
            ),
            key=lambda event: event["sequence"],
        ),
        "inputs": guarded_rows(
            guards["agents"],
            "agents.run_inputs",
            (
                "workspace_id",
                "run_id",
                "operation_id",
                "response_id",
                "status",
                "queue_sequence",
                "evidence_sequence",
            ),
            where="workspace_id=:workspace AND run_id=:run",
            parameters=parameters,
        ),
        "reservations": guarded_rows(
            guards["agents"],
            "agents.run_reservations",
            ("run_id", "workspace_id", "installation_key", "active", "session"),
            where="workspace_id=:workspace AND run_id=:run",
            parameters=parameters,
        ),
        "installations": guarded_rows(
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
            where="harness_id=:harness",
            parameters={"harness": RetainedOriginalExitInspector.harness_id},
        ),
    }


def test_retained_original_unknown_stop_reconciles_after_confirmed_exit(
    tmp_path,
    retain_incomplete_product_work,
):
    targets = select_targets(os.environ)
    if targets is None:
        pytest.skip("NOT_RUN: four explicit product target selections are absent")
    directory = os.environ.get("SIDERA_PRODUCT_TEST_EVIDENCE_DIRECTORY")
    assert directory is not None
    evidence = Path(directory)
    pending = json.loads(
        (evidence / "chat-api-disjoint-1-outcome-pending.json").read_text()
    )
    saved = json.loads(
        (
            evidence
            / "chat-functional-and-cleanup-2e3c5ab19a0144039c9397f56c59262c.json"
        ).read_text()
    )
    observed = json.loads(
        (
            evidence
            / "chat-original-close-confirmed-94f7f762aa4841c4b229b678fcfd9440.json"
        ).read_text()
    )
    retirement = json.loads(
        (evidence / "chat-disjoint-host-postexit-targets-2.json").read_text()
    )
    workspace = UUID("f8e3d8a9-3963-454f-bda0-edc058345078")
    conversation = UUID("012e427e-9609-4c7c-aa97-ea636dc3d894")
    run = UUID("c27aa8b9-ccbc-46f4-b7b9-41e67ece0e27")
    grant = UUID("207519a5-803d-4546-a6d7-6840799e73f4")
    stop = UUID("47dcf7ba-55c4-435c-91c4-ff2969e90e80")
    start = UUID("99ef0209-974b-55f0-ba07-739eac260761")
    responses = {
        "06b01814-580e-48e8-aa7c-3814be624d69",
        "d8d6fed4-6fbe-4490-af52-4c1bbc14ac2e",
    }
    assert retirement["retired_pid"] == 66449 and retirement["host_exit_code"] == 143
    assert (
        retirement["normal_fixture_cleanup"] is False
        and retirement["all_readonly_inspections_disposed"] is True
    )
    assert (
        pending["remaining_chat"]["workspace_id"]
        == saved["workspace_id"]
        == str(workspace)
    )
    assert pending["remaining_chat"]["run_id"] == str(run) and pending[
        "remaining_chat"
    ]["stop_operation_id"] == str(stop)
    assert (
        saved["cleanup_complete"] is False and saved["confirmed_product_close"] is False
    )
    assert all(
        saved[key]["status"] == "completed"
        for key in ("first_completed", "second_completed")
    )
    assert {
        saved[key]["responseId"] for key in ("first_completed", "second_completed")
    } == responses
    completion = observed["controlled_original_close_completion"]
    assert (
        completion
        == saved["controlled_original_close_completion"]
        == {
            "run_id": str(run),
            "original_stop_operation_id": str(stop),
            "evidence": "controlled-external-completion-after-closeFalse",
            "real_provider": False,
        }
    )
    assert [
        sum(call[0] == name for call in saved["native_calls"])
        for name in ("open", "prompt", "close")
    ] == [1, 2, 1]
    old_saved = retained_first_attempt(evidence)
    selected_guards = acquire_guards(targets)
    guards = {guard.target.component: guard for guard in selected_guards}
    runtime = None
    native, secrets = RetainedOriginalExitInspector(run), ControlledSecretStore()
    facts = {
        "workspace_id": str(workspace),
        "conversation_id": str(conversation),
        "original_run_id": str(run),
        "original_grant_id": str(grant),
        "original_stop_operation_id": str(stop),
        "proof": "restart-style-controlled-original-exit-observation-only",
        "old_host_exit_code": 143,
        "old_host_normal_cleanup": False,
        "lifespan_started": False,
        "cp_background_started": False,
        "functional_complete": False,
    }
    complete = False
    try:
        heads = {
            "product": "0003_conversations",
            "agents": "0004_inputs_events",
            "cp": "0039_hard_only_input_selections",
            "credentials": "0001_connections_credentials",
        }
        assert all(
            guard.initial["inventory"]["heads"] == [heads[guard.target.component]]
            for guard in selected_guards
        )
        facts["guarded_before_mutation"] = [guard.initial for guard in selected_guards]
        active = guarded_rows(
            guards["agents"],
            "agents.runs",
            ("run_id", "workspace_id", "state"),
            where="state IN ('starting', 'running', 'idle', 'closing')",
        )
        facts["live_runs_before_poll"] = active
        write_evidence(evidence, "chat-retained-cleanup-preflight", facts)
        assert active == [], "Retained original poll must not fence another live intent"
        before = retained_unknown_cleanup_rows(
            guards, workspace=workspace, conversation=conversation, run=run, stop=stop
        )
        old_before = retained_original_rows(guards, old_saved)
        unrelated = unrelated_original_cleanup_fingerprints(
            guards, conversation_id=conversation, run_id=run
        )
        facts.update(
            before=before, old_first_before=old_before, unrelated_before=unrelated
        )
        assert (
            len(before["conversations"])
            == len(before["runs"])
            == len(before["reservations"])
            == 1
        )
        assert (
            before["conversations"][0]["stop_state"]
            == before["runs"][0]["state"]
            == "unknown"
        )
        original_conversation = before["conversations"][0]
        assert original_conversation["run_id"] == str(run) and original_conversation[
            "grant_id"
        ] == str(grant)
        assert original_conversation["start_operation_id"] == str(
            start
        ) and original_conversation["stop_operation_id"] == str(stop)
        assert original_conversation["principal_id"] == "owned-native-chat-user"
        assert before["runs"][0]["original_grant_id"] == str(grant) and before["runs"][
            0
        ]["operation_id"] == str(start)
        assert [event["kind"] for event in before["stop_events"]] == [
            "stop.accepted",
            "stop.unknown",
        ]
        assert [event["sequence"] for event in before["stop_events"]] == [14, 15]
        assert (
            len(before["turns"]) == 2
            and {turn["response_id"] for turn in before["turns"]} == responses
        )
        assert all(
            turn["cached_status"] == "completed" and turn["dispatch_attempt"] == 1
            for turn in before["turns"]
        )
        components = {target.component: target for target in targets}
        settings = ProductSettings(
            **{
                name: components[component].url.render_as_string(hide_password=False)
                for name, component in (
                    ("product_database_url", "product"),
                    ("agent_database_url", "agents"),
                    ("cp_database_url", "cp"),
                    ("credential_database_url", "credentials"),
                )
            },
            deployment_namespace=UUID("898937f8-0a89-51ea-bfee-563b8524f4ad"),
            workspace_id=workspace,
            local_principal_id="owned-native-chat-user",
            bind_host="127.0.0.1",
            port=18080,
            web_origin="http://127.0.0.1:18080",
            workspace_root=tmp_path / "retained-workspaces",
            artifact_root=tmp_path / "retained-artifacts",
        )
        settings.workspace_root.mkdir(mode=0o700)
        settings.artifact_root.mkdir(mode=0o700)
        runtime = build_product_runtime(
            settings, harnesses=(native,), secret_store=secrets
        )
        runtime.agents.poll_once()
        receipt = runtime.execution.get_stop(workspace, run, stop, grant_id=grant)
        snapshot = runtime.execution.get_run(workspace, run, grant_id=grant)
        after = retained_unknown_cleanup_rows(
            guards, workspace=workspace, conversation=conversation, run=run, stop=stop
        )
        facts.update(
            original_stop_receipt=receipt.model_dump(mode="json"),
            original_run=snapshot.model_dump(mode="json"),
            after_poll=after,
            inspected_original_handles=list(native.inspected_handles),
            native_mutations=list(native.calls),
        )
        write_evidence(evidence, "chat-retained-cleanup-after-poll", facts)
        assert (
            receipt.status is StopStatus.STOPPED
            and receipt.operation_id == stop
            and receipt.run_id == run
        )
        assert snapshot.state is RunState.STOPPED and not snapshot.input_open
        assert native.inspected_handles and all(
            handle == before["runs"][0]["native_handle"]
            for handle in native.inspected_handles
        )
        assert native.calls == [] and secrets.reads == []
        assert after["all_events"][:-1] == before["all_events"]
        assert [event["kind"] for event in after["stop_events"]] == [
            "stop.accepted",
            "stop.unknown",
            "stop.stopped",
        ]
        assert after["stop_events"][-1]["operation_id"] == str(stop)
        assert after["turns"] == before["turns"] and after["inputs"] == before["inputs"]
        assert all(
            not row["active"] and not row["session"] for row in after["reservations"]
        )
        assert all(
            row["active_count"] == row["session_count"] == 0
            for row in after["installations"]
        )
        runtime.agents.poll_once()
        repeated = runtime.execution.get_stop(workspace, run, stop, grant_id=grant)
        repeated_rows = retained_unknown_cleanup_rows(
            guards, workspace=workspace, conversation=conversation, run=run, stop=stop
        )
        facts.update(
            repeated_stop_receipt=repeated.model_dump(mode="json"),
            repeated_rows=repeated_rows,
        )
        assert repeated == receipt and repeated_rows == after
        runtime.chat.activate()
        closed = runtime.close(grace_seconds=10)
        final = retained_unknown_cleanup_rows(
            guards, workspace=workspace, conversation=conversation, run=run, stop=stop
        )
        old_after = retained_original_rows(guards, old_saved)
        unrelated_after = unrelated_original_cleanup_fingerprints(
            guards, conversation_id=conversation, run_id=run
        )
        facts.update(
            owner_close=closed,
            final_status=runtime.status(),
            final_rows=final,
            old_first_after=old_after,
            unrelated_after=unrelated_after,
        )
        write_evidence(evidence, "chat-retained-cleanup-confirmed", facts)
        assert closed is True and runtime.status()["lifecycle"] == "closed"
        confirmed_conversation = final["conversations"][0]
        assert (
            confirmed_conversation["stop_operation_id"] == str(stop)
            and confirmed_conversation["stop_state"] == "stopped"
        )
        assert {
            key: value
            for key, value in confirmed_conversation.items()
            if key not in {"stop_state", "version", "updated_at"}
        } == {
            key: value
            for key, value in original_conversation.items()
            if key not in {"stop_state", "version", "updated_at"}
        }
        assert (
            final["stop_events"] == after["stop_events"]
            and final["turns"] == before["turns"]
            and final["inputs"] == before["inputs"]
        )
        assert old_after == old_before and unrelated_after == unrelated
        assert native.calls == [] and secrets.reads == []
        facts["functional_complete"] = True
    finally:
        try:
            owner_closed = runtime is None or runtime.close(grace_seconds=10)
            facts.update(
                owner_disposed=owner_closed,
                native_mutations=list(native.calls),
                inspected_original_handles=list(native.inspected_handles),
            )
            facts["owner_threads"] = [
                thread.name
                for thread in enumerate_threads()
                if thread.name == "sidera-agent-execution"
                or thread.name.startswith("sidera-native-")
            ]
            facts["target_idle_after_disposal"] = [
                guard.inspect() for guard in selected_guards
            ]
            cleanup_proven = (
                owner_closed
                and facts["functional_complete"]
                and not facts["owner_threads"]
                and native.calls == []
                and all(
                    all(value == 0 for value in item["idle"].values())
                    for item in facts["target_idle_after_disposal"]
                )
            )
            facts["cleanup_complete"] = cleanup_proven
            write_evidence(evidence, "chat-retained-cleanup-final", facts)
            if cleanup_proven:
                released = {
                    guard.target.component: guard.release()
                    for guard in reversed(selected_guards)
                }
                facts["guards_released"] = released
                write_evidence(evidence, "chat-retained-cleanup-guards-released", facts)
                complete = all(released.values())
        finally:
            if not complete:
                retain_incomplete_product_work((runtime, native, selected_guards))
                write_evidence(evidence, "chat-retained-cleanup-incomplete", facts)
        assert complete, (
            "Retained original cleanup incomplete; owners and guards retained"
        )
