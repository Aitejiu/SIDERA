from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[4]
for _import_root in (
    PROJECT_ROOT / "akashic-agent" / ".venv" / "lib" / "python3.12" / "site-packages",
    PROJECT_ROOT / "akashic-agent",
    PROJECT_ROOT / "integrations" / "akashic",
    PROJECT_ROOT / "services" / "knowledge-service" / "src",
):
    _import_root_text = str(_import_root)
    if _import_root_text not in sys.path:
        sys.path.insert(0, _import_root_text)

from agent.control.models import TurnItem, TurnItemKind, TurnRecord, TurnStatus
from memory2.store import MemoryStore2
from session.store import SessionStore

from sidera_control_plane.demo.cli import prepare_demo_run
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.database import database_url_for
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ArtifactModel,
    CommandModel,
    ContextPackageModel,
    ExecutionInputModel,
    ExecutionModel,
    ExecutionSessionModel,
    RealResultRunBudgetModel,
    ResultVerificationResultModel,
    TaskModel,
    WorkerItemModel,
)


@dataclass(frozen=True, slots=True)
class MaintenanceBaseline:
    manifest: DemoManifest
    control_database: Database
    knowledge_database: Any
    session_store: SessionStore
    memory_store: MemoryStore2
    baseline_path: Path
    payload: dict[str, Any]


class JsonFileKV:
    """Small file-backed KeyValueStore used only by the isolated bridge fixture."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, key: str, default: object = None) -> object:
        del key
        if not self.path.exists():
            return default
        return json.loads(self.path.read_text(encoding="utf-8"))

    def set(self, key: str, value: object) -> None:
        del key
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return completed.stdout.strip()


def _seed_knowledge_facts(database: Any, workspace_id: UUID) -> dict[str, Any]:
    from sidera_knowledge.application.command_service import KnowledgeCommandService
    from sidera_knowledge.application.knowledge_commands import (
        knowledge_command_handlers,
    )
    from sidera_knowledge.domain.commands import CommandEnvelope
    from sidera_knowledge.persistence.models import (
        KnowledgeCandidateModel,
        KnowledgePageModel,
        KnowledgeRevisionModel,
    )
    from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork

    candidate_id = uuid4()
    source_id = uuid4()
    payload = {
        "candidate_schema_version": 1,
        "candidate_id": str(candidate_id),
        "operation": "create_page",
        "title": "Maintenance recovery guide",
        "slug": f"maintenance-{candidate_id.hex}",
        "summary": "Representative knowledge retained by the maintenance baseline.",
        "tags": ["maintenance", "baseline"],
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "Fixture overview",
                "body_markdown": "This page is non-sensitive representative data.",
                "ordinal": 0,
            }
        ],
        "relations": [],
        "sources": [
            {
                "source_id": str(source_id),
                "source_type": "manual",
                "label": "Maintenance fixture source",
                "metadata": {"fixture": "lm-2.1"},
            }
        ],
        "provenance": {"producer_type": "main_agent", "producer_id": "fixture"},
    }
    submit = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="SubmitKnowledgeCandidate",
        actor_type="main_agent",
        actor_id="fixture-main-agent",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload=payload,
        expected_version=None,
        idempotency_key=f"lm-2.1-submit:{candidate_id}",
        correlation_id=uuid4(),
    )
    service = KnowledgeCommandService(
        lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
    )
    submitted = service.handle(submit)
    if submitted.status != "applied":
        raise AssertionError(f"knowledge candidate submit failed: {submitted.status}")

    with database.session() as session:
        candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        if candidate is None:
            raise AssertionError("knowledge candidate was not persisted")
        candidate_hash = candidate.candidate_hash
        candidate_version = candidate.version

    review = CommandEnvelope(
        command_id=uuid4(),
        workspace_id=workspace_id,
        command_type="ReviewKnowledgeCandidate",
        actor_type="local_owner",
        actor_id="fixture-owner",
        target_type="knowledge_candidate",
        target_id=candidate_id,
        payload={
            "candidate_id": str(candidate_id),
            "decision": "accept",
            "reason": "Accepted representative baseline content",
            "observed_candidate_hash": candidate_hash,
            "expected_candidate_version": candidate_version,
        },
        expected_version=None,
        idempotency_key=f"lm-2.1-review:{candidate_id}",
        correlation_id=uuid4(),
    )
    reviewed = service.handle(review)
    if reviewed.status != "applied":
        raise AssertionError(f"knowledge candidate review failed: {reviewed.status}")

    with database.session() as session:
        stored_candidate = session.scalar(
            select(KnowledgeCandidateModel).where(
                KnowledgeCandidateModel.id == candidate_id,
                KnowledgeCandidateModel.workspace_id == workspace_id,
            )
        )
        page = session.scalar(
            select(KnowledgePageModel).where(
                KnowledgePageModel.workspace_id == workspace_id,
                KnowledgePageModel.slug == f"maintenance-{candidate_id.hex}",
            )
        )
        revision = None
        if page is not None:
            revision = session.scalar(
                select(KnowledgeRevisionModel).where(
                    KnowledgeRevisionModel.workspace_id == workspace_id,
                    KnowledgeRevisionModel.page_id == page.id,
                )
            )
    if stored_candidate is None or stored_candidate.status != "accepted":
        raise AssertionError("knowledge candidate was not accepted")
    if page is None or revision is None:
        raise AssertionError("accepted knowledge tree was not persisted")
    return {
        "candidate_id": str(stored_candidate.id),
        "candidate_hash": stored_candidate.candidate_hash,
        "candidate_status": stored_candidate.status,
        "page_id": str(page.id),
        "revision_id": str(revision.id),
        "revision_hash": revision.content_hash,
        "snapshot": revision.snapshot,
    }


def _seed_bridge_facts(path: Path, planning_request_id: UUID, root_task_id: UUID) -> dict[str, Any]:
    from sidera_task_bridge.state import (
        BridgeStateStore,
        OutboundCommand,
        PrivateRoute,
    )

    command_id = uuid4()
    now = datetime(2026, 9, 19, 4, 0, tzinfo=UTC)
    command = OutboundCommand(
        command_id=command_id,
        resource_lock_key=f"planning:{planning_request_id}",
        request_json={
            "command_id": str(command_id),
            "planning_request_id": str(planning_request_id),
            "objective": "retain the accepted baseline",
        },
        created_at=now,
        last_attempted_at=now,
        attempt_count=0,
    )

    async def scenario() -> dict[str, Any]:
        store = BridgeStateStore(JsonFileKV(path))
        first, first_disposition = await store.reserve_or_recover(command)
        replay, replay_disposition = await store.reserve_or_recover(command)
        if first != replay:
            raise AssertionError("bridge dedupe replay changed the command")
        await store.record_attempt(command_id, at=now + timedelta(seconds=3))
        await store.save_route(
            "telegram:fixture-chat",
            PrivateRoute(
                channel="telegram",
                chat_id="fixture-chat",
                session_key="telegram:fixture-chat",
                control_plane_route_id=root_task_id,
            ),
        )
        await store.bind_resource_source(
            "planning_request",
            planning_request_id,
            "akashic_private",
            "telegram:fixture-chat",
            bound_at=now,
        )
        await store.advance_notification_cursor(7)
        snapshot = await store.snapshot()
        return {
            "command_id": str(command_id),
            "first_disposition": first_disposition,
            "replay_disposition": replay_disposition,
            "cursor": snapshot.notification_cursor,
            "unresolved_ids": sorted(snapshot.unresolved_commands),
            "routes": sorted(snapshot.source_routes),
            "resource_bindings": sorted(snapshot.resource_sources),
            "state_hash": _canonical_hash(snapshot.model_dump(mode="json")),
        }

    result = asyncio.run(scenario())
    if result["first_disposition"] != "new" or result["replay_disposition"] != "recovered":
        raise AssertionError("bridge dedupe dispositions are not representative")
    return result


def _seed_akashic_facts(run_root: Path, workspace_id: UUID) -> tuple[SessionStore, MemoryStore2, dict[str, Any]]:
    session_path = run_root / "akashic" / "sessions.db"
    memory_path = run_root / "akashic" / "memory2.db"
    session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(session_path.parent, 0o700)
    attachment_path = run_root / "akashic" / "attachments" / "baseline" / "spec.md"
    attachment_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    attachment_path.write_text(
        "Representative attachment retained by the maintenance baseline.\n",
        encoding="utf-8",
    )
    os.chmod(attachment_path, 0o600)
    session_store = SessionStore(session_path)
    session_store._conn.execute("PRAGMA journal_mode=WAL")
    session_store._conn.execute("PRAGMA wal_autocheckpoint=0")
    session_store._conn.commit()

    session_key = f"web:{workspace_id}:main-agent"
    now = datetime(2026, 9, 19, 4, 0, tzinfo=UTC)
    created_at = now.isoformat()
    session_store.create_session(
        key=session_key,
        metadata={"workspace_id": str(workspace_id), "fixture": "lm-2.1"},
        last_user_at=created_at,
    )
    session_store.persist_session(
        session_key,
        created_at=created_at,
        updated_at=(now + timedelta(seconds=2)).isoformat(),
        metadata={"workspace_id": str(workspace_id), "fixture": "lm-2.1"},
        messages=[
            {
                "role": "user",
                "content": "保留这个基线任务的执行上下文。",
                "timestamp": created_at,
                "extra": {"media": ["attachment://baseline/spec.md"]},
            },
            {
                "role": "assistant",
                "content": "已记录任务、候选和执行边界。",
                "timestamp": (now + timedelta(seconds=1)).isoformat(),
                "extra": {"source_refs": [{"kind": "task", "id": str(workspace_id)}]},
            },
        ],
    )
    turn_id = f"turn-{workspace_id}"
    turn = session_store.create_turn(
        TurnRecord(
            id=turn_id,
            thread_id=session_key,
            status=TurnStatus.QUEUED,
            input="继续检查这个任务的恢复基线。",
            created_at=now + timedelta(seconds=2),
            metadata={"fixture": "lm-2.1"},
            items=[
                TurnItem(
                    id=f"{turn_id}:input",
                    kind=TurnItemKind.USER_MESSAGE,
                    data={"text": "继续检查这个任务的恢复基线。"},
                )
            ],
        )
    )
    read_messages = session_store.fetch_session_messages(session_key)
    if session_store.get_session_meta(session_key) is None or session_store.read_turn(turn.id) is None:
        raise AssertionError("Akashic session facts were not readable")

    memory_store = MemoryStore2(memory_path, vec_dim=4)
    memory_result = memory_store.upsert_item(
        "preference",
        "任务恢复时保留主 Agent 对话和 WorkerItem 进度。",
        [0.1, 0.2, 0.3, 0.4],
        source_ref=f"workspace:{workspace_id}",
        extra={"enabled": True, "fixture": "lm-2.1"},
        happened_at=created_at,
    )
    memory_id = memory_result.removeprefix("new:").removeprefix("reinforced:")
    memory_rows = memory_store.list_by_type("preference")
    if not any(str(row["id"]) == memory_id for row in memory_rows):
        raise AssertionError("enabled memory was not readable")

    wal_path = Path(f"{session_path}-wal")
    if not wal_path.exists():
        raise AssertionError("session fixture did not retain an uncheckpointed WAL")
    body_payload = [
        {
            "id": row["id"],
            "seq": row["seq"],
            "role": row["role"],
            "content": row["content"],
            "extra": row.get("media") or row.get("source_refs") or {},
        }
        for row in read_messages
    ]
    return session_store, memory_store, {
        "session_db": str(session_path),
        "session_wal": str(wal_path),
        "thread_id": session_key,
        "turn_id": turn.id,
        "message_ids": [str(row["id"]) for row in read_messages],
        "message_order": [int(row["seq"]) for row in read_messages],
        "message_body_hash": _canonical_hash(body_payload),
        "memory_db": str(memory_path),
        "memory_id": memory_id,
        "memory_hash": _canonical_hash(memory_rows),
        "attachment_path": str(attachment_path),
        "attachment_hash": hashlib.sha256(attachment_path.read_bytes()).hexdigest(),
    }


def _seed_main_agent_facts(run_root: Path, workspace_id: UUID) -> dict[str, Any]:
    store_path = run_root / "main-agent" / "store.json"
    thread_id = f"demo-thread-{workspace_id}"
    turn_id = f"demo-turn-{workspace_id}"
    created_at = "2026-09-19T04:00:00+00:00"
    document = {
        "demo_main_agent_conversations_v1": {
            "threads": {
                thread_id: {
                    "id": thread_id,
                    "title": "恢复基线对话",
                    "created_at": created_at,
                    "updated_at": "2026-09-19T04:00:02+00:00",
                    "context_refs": [f"workspace:{workspace_id}"],
                    "turns": [
                        {
                            "id": turn_id,
                            "thread_id": thread_id,
                            "status": "completed",
                            "created_at": "2026-09-19T04:00:01+00:00",
                            "started_at": "2026-09-19T04:00:01+00:00",
                            "completed_at": "2026-09-19T04:00:02+00:00",
                            "final_response": "恢复后的历史仍然可读。",
                            "items": [
                                {
                                    "id": f"{turn_id}:input",
                                    "type": "userMessage",
                                    "data": {"text": "读取恢复后的历史。"},
                                },
                                {
                                    "id": f"{turn_id}:output",
                                    "type": "assistantMessage",
                                    "data": {"text": "恢复后的历史仍然可读。"},
                                },
                            ],
                            "usage": {"inputTokens": 12, "outputTokens": 8},
                            "error": None,
                            "events": [
                                {
                                    "sequence": 1,
                                    "type": "completed",
                                    "data": {"status": "completed"},
                                }
                            ],
                        }
                    ],
                }
            }
        }
    }
    store_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(store_path, 0o600)
    return {
        "store_path": str(store_path),
        "store_hash": hashlib.sha256(store_path.read_bytes()).hexdigest(),
        "thread_id": thread_id,
        "turn_id": turn_id,
    }


def _seed_control_facts(
    database: Database, manifest: DemoManifest, tmp_path: Path
) -> dict[str, Any]:
    from tests.integration.result_integration_scenario import ResultIntegrationScenario

    scenario = ResultIntegrationScenario(
        database,
        manifest.workspace_id,
        manifest.run_root / "result-scenario",
        {"root_task": uuid4(), "plan_snapshot": uuid4()},
        uuid4(),
    )
    candidate = scenario.candidate()
    scenario._planning.assemble_context("summary")
    scenario.command("CompleteTask", scenario.accept_payload(candidate))
    head_before_drive = scenario.head()
    operation_count = len(scenario.result_operations())
    deadline = head_before_drive.auto_deadline
    scenario.drive()
    root_task_id = scenario.root_task_id
    worker_item_id = scenario.worker_item_ids["summary"]
    with database.session() as session:
        subtask_id = session.scalar(
            select(TaskModel.id)
            .where(
                TaskModel.workspace_id == manifest.workspace_id,
                TaskModel.id != root_task_id,
            )
            .order_by(TaskModel.created_at, TaskModel.id)
            .limit(1)
        )
        artifact = session.scalar(
            select(ArtifactModel)
            .where(ArtifactModel.workspace_id == manifest.workspace_id)
            .order_by(ArtifactModel.created_at, ArtifactModel.id)
            .limit(1)
        )
        package = session.scalar(
            select(ContextPackageModel)
            .where(ContextPackageModel.workspace_id == manifest.workspace_id)
            .order_by(ContextPackageModel.created_at, ContextPackageModel.id)
            .limit(1)
        )
        execution_id = session.scalar(
            select(ExecutionModel.id)
            .where(
                ExecutionModel.workspace_id == manifest.workspace_id,
                ExecutionModel.worker_item_id == worker_item_id,
            )
            .order_by(ExecutionModel.created_at, ExecutionModel.id)
            .limit(1)
        )
        worker_item = session.get(WorkerItemModel, worker_item_id)
        execution_session = None
        if execution_id is not None:
            execution_session = session.scalar(
                select(ExecutionSessionModel)
                .where(
                    ExecutionSessionModel.workspace_id == manifest.workspace_id,
                    ExecutionSessionModel.execution_id == execution_id,
                )
                .order_by(ExecutionSessionModel.created_at, ExecutionSessionModel.id)
                .limit(1)
            )
    if (
        subtask_id is None
        or artifact is None
        or package is None
        or execution_id is None
        or worker_item is None
    ):
        raise AssertionError(
            "result scenario did not materialize the baseline facts: "
            f"subtask={subtask_id is not None}, artifact={artifact is not None}, "
            f"package={package is not None}, execution={execution_id is not None}, "
            f"worker_item={worker_item is not None}"
        )
    if execution_session is None:
        raise AssertionError("result scenario did not materialize an execution session")
    artifact_storage_root = manifest.run_root / "result-scenario" / "result-integration-artifacts"
    artifact_content_path = (
        artifact_storage_root
        / str(manifest.workspace_id)
        / artifact.content_hash[:2]
        / artifact.content_hash
    )
    if not artifact_content_path.is_file():
        raise AssertionError("result artifact content was not persisted")
    artifact_content_hash = hashlib.sha256(artifact_content_path.read_bytes()).hexdigest()
    if artifact_content_hash != artifact.content_hash:
        raise AssertionError("result artifact content hash did not round-trip")

    command_id = uuid4()
    input_id = uuid4()
    budget_run_id = uuid4()
    fixture_deadline = deadline or datetime(2026, 9, 19, 5, 0, tzinfo=UTC)
    with database.session() as session:
        session.add(
            CommandModel(
                id=command_id,
                workspace_id=manifest.workspace_id,
                command_type="SendExecutionInput",
                actor_type="user",
                actor_id="fixture-owner",
                target_type="execution_session",
                target_id=execution_session.id,
                payload={"source": "maintenance-baseline", "state": "unknown"},
                expected_version=execution_session.version,
                idempotency_key=f"lm-2.1-input:{input_id}",
                request_hash="b" * 64,
                correlation_id=root_task_id,
                status="applied",
                result={"accepted": True},
                rejection_reason=None,
            )
        )
        session.add(
            ExecutionInputModel(
                id=input_id,
                workspace_id=manifest.workspace_id,
                execution_id=execution_id,
                session_id=execution_session.id,
                client_idempotency_key=f"lm-2.1-input:{input_id}",
                text="确认当前执行结果并保留原上下文。",
                attachment_refs=["artifact://baseline/spec"],
                status="blocked",
                operation_id=None,
                failure_code="awaiting_operator",
                superseded_reason=None,
                version=1,
                source_command_id=command_id,
                source_input_id=None,
                handoff_transition_id=None,
            )
        )
        unknown_id = uuid4()
        session.add(
            ResultVerificationResultModel(
                id=unknown_id,
                schema_version=1,
                workspace_id=manifest.workspace_id,
                root_task_id=root_task_id,
                candidate_id=candidate.id,
                execution_id=candidate.integration_execution_id,
                check_key="fixture-unknown",
                check_version=1,
                check_hash="c" * 64,
                environment_ref="env://maintenance-fixture",
                environment_hash="d" * 64,
                actual_commit=scenario.candidate_commit(candidate),
                actual_tree=scenario.candidate_tree(candidate),
                operation_id=uuid4(),
                attempt=1,
                status="unknown",
                exit_code=None,
                started_at=None,
                finished_at=None,
                artifact_ids=[str(artifact.id)],
                failure_code="fixture_inspect_required",
                payload={"fixture": "lm-2.1"},
                created_at=datetime(2026, 9, 19, 4, 0, tzinfo=UTC),
            )
        )
        session.add(
            RealResultRunBudgetModel(
                run_id=budget_run_id,
                max_executions=4,
                reserved_executions=1,
                profile_fingerprint="e" * 64,
                expires_at=fixture_deadline,
                created_at=datetime(2026, 9, 19, 4, 0, tzinfo=UTC),
            )
        )
        session.commit()

    release = scenario.release_for_request(scenario.request_ids["summary"])
    ledger_count = operation_count
    with database.session() as session:
        budget = session.get(RealResultRunBudgetModel, budget_run_id)
    if ledger_count < 1 or budget is None or budget.expires_at is None:
        raise AssertionError("result operation ledger did not expose a deadline")
    ledger_deadline = budget.expires_at.isoformat()
    return {
        "root_task_id": str(root_task_id),
        "subtask_id": str(subtask_id),
        "worker_item_id": str(worker_item_id),
        "execution_id": str(execution_id),
        "session_id": str(execution_session.id),
        "command_id": str(command_id),
        "input_id": str(input_id),
        "unknown_verification_id": str(unknown_id),
        "unknown_status": "unknown",
        "input_status": "blocked",
        "accepted_candidate_id": str(candidate.id),
        "accepted_candidate_hash": candidate.content_hash,
        "accepted_tree": scenario.candidate_tree(candidate),
        "artifact_id": str(artifact.id),
        "artifact_hash": artifact.content_hash,
        "artifact_storage_root": str(artifact_storage_root),
        "artifact_content_path": str(artifact_content_path),
        "artifact_content_hash": artifact_content_hash,
        "context_package_id": str(package.id),
        "context_package_hash": package.package_hash,
        "planning_request_id": str(scenario.request_id),
        "operation_ledger": {
            "count": ledger_count,
            "deadline": ledger_deadline,
            "budget_run_id": str(budget_run_id),
            "release_id": str(release.id),
        },
        "repository": str(scenario.repository_path),
        "repository_head": _git(scenario.repository_path, "rev-parse", "HEAD"),
        "repository_tree": _git(scenario.repository_path, "rev-parse", "HEAD^{tree}"),
    }


def _write_baseline(run_root: Path, payload: dict[str, Any]) -> Path:
    path = run_root / "maintenance-baseline.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


@contextmanager
def maintenance_baseline(tmp_path: Path) -> Iterator[MaintenanceBaseline]:
    manifest: DemoManifest | None = None
    control_database: Database | None = None
    knowledge_database: Any | None = None
    session_store: SessionStore | None = None
    memory_store: MemoryStore2 | None = None
    try:
        manifest = prepare_demo_run("mainline")
        control_database = Database(database_url_for(manifest))
        from sidera_knowledge.persistence.database import Database as KnowledgeDatabase

        knowledge_database = KnowledgeDatabase(database_url_for(manifest, knowledge=True))
        control = _seed_control_facts(control_database, manifest, tmp_path)
        knowledge = _seed_knowledge_facts(knowledge_database, manifest.workspace_id)
        session_store, memory_store, akashic = _seed_akashic_facts(
            manifest.run_root, manifest.workspace_id
        )
        main_agent = _seed_main_agent_facts(manifest.run_root, manifest.workspace_id)

        (manifest.repository / "baseline-notes.md").write_text(
            "Non-sensitive uncommitted maintenance fixture.\n", encoding="utf-8"
        )
        tracked_paths = _git(manifest.repository, "ls-files").splitlines()
        if not tracked_paths:
            raise AssertionError("sample repository has no tracked file")
        working_tree_path = manifest.repository / tracked_paths[0]
        working_tree_path.write_bytes(
            working_tree_path.read_bytes()
            + b"\nUncommitted working-tree maintenance fixture.\n"
        )
        staged_path = manifest.repository / "staged-maintenance-notes.md"
        staged_path.write_text(
            "Non-sensitive staged maintenance fixture.\n", encoding="utf-8"
        )
        _git(manifest.repository, "add", "--", str(staged_path.relative_to(manifest.repository)))
        repository_status = _git(manifest.repository, "status", "--porcelain=v1")
        bridge_path = manifest.run_root / "akashic" / "plugin-data.json"
        bridge = _seed_bridge_facts(
            bridge_path,
            UUID(control["planning_request_id"]),
            UUID(control["root_task_id"]),
        )
        bridge["state_path"] = str(bridge_path)
        baseline_payload: dict[str, Any] = {
            "schema_version": 1,
            "run_id": str(manifest.run_id),
            "workspace_id": str(manifest.workspace_id),
            "source_identities": {
                "control_plane_database": manifest.database_name,
                "knowledge_database": manifest.knowledge_database_name,
                "repository_head": _git(manifest.repository, "rev-parse", "HEAD"),
            },
            "control_plane": control,
            "knowledge": knowledge,
            "akashic": akashic,
            "main_agent": main_agent,
            "bridge": bridge,
            "repository": {
                "path": str(manifest.repository),
                "status": repository_status,
                "head": _git(manifest.repository, "rev-parse", "HEAD"),
                "tree": _git(manifest.repository, "rev-parse", "HEAD^{tree}"),
                "uncommitted_file_hash": hashlib.sha256(
                    (manifest.repository / "baseline-notes.md").read_bytes()
                ).hexdigest(),
            },
            "provider_calls": 0,
            "secret_values_read": False,
        }
        baseline_path = _write_baseline(manifest.run_root, baseline_payload)
        yield MaintenanceBaseline(
            manifest=manifest,
            control_database=control_database,
            knowledge_database=knowledge_database,
            session_store=session_store,
            memory_store=memory_store,
            baseline_path=baseline_path,
            payload=baseline_payload,
        )
    finally:
        if session_store is not None:
            session_store.close()
        if memory_store is not None:
            memory_store.close()
        if knowledge_database is not None:
            knowledge_database.dispose()
        if control_database is not None:
            control_database.dispose()
        if manifest is not None:
            from .test_local_maintenance_restore import _drop_demo_databases

            _drop_demo_databases(manifest)
            shutil.rmtree(manifest.run_root, ignore_errors=True)
            shutil.rmtree(manifest.repository, ignore_errors=True)
