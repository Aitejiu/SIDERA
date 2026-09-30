from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sidera_runtime_guard import RuntimeBudgetGuard
from sidera_task_bridge.client import (
    CommandDispatcher,
    CommandExecution,
    CommandIntent,
    CommandResultUnknown,
    ControlPlaneClient,
    DispatchResult,
    TransportUnavailable,
)
from sidera_task_bridge.config import TaskBridgeConfig

from sidera_main_agent_gateway.acceptance_guard import (
    begin_main_turn,
    settle_main_turn,
)
from sidera_main_agent_gateway.config import Settings
from sidera_main_agent_gateway.contracts import (
    EventType,
    JsonValue,
    MainAgentAdapter,
    MainAgentEvent,
    MainAgentItem,
    MainAgentThread,
    MainAgentThreadPage,
    MainAgentTurn,
    MainAgentTurnHandle,
)
from sidera_main_agent_gateway.errors import (
    ProviderProtocolError,
    ThreadBusy,
    ThreadNotFound,
    TurnNotFound,
)

from .intents import DemoIntent, classify_demo_intent
from .store import DemoKeyValueStore, DemoManifestIdentity, load_demo_manifest

_CONVERSATIONS_KEY = "demo_main_agent_conversations_v1"
_DEMO_SOURCE_REF = "module:interview-demo/local-channel"


class DemoMainAgentAdapter(MainAgentAdapter):
    """Persistent finite-intent Main Agent for the isolated interview demo."""

    def __init__(
        self,
        settings: Settings,
        *,
        dispatcher: Any | None = None,
        client: ControlPlaneClient | None = None,
        store: DemoKeyValueStore | None = None,
        acceptance_guard: RuntimeBudgetGuard | None = None,
    ) -> None:
        if settings.demo_manifest is None:
            raise ValueError("interview demo adapter requires a manifest")
        self._settings = settings
        self._acceptance_guard = acceptance_guard
        self._manifest: DemoManifestIdentity = load_demo_manifest(
            settings.demo_manifest
        )
        if self._manifest.workspace_id != settings.sidera_workspace_id:
            raise ValueError("demo manifest workspace does not match Gateway settings")
        self._store = store or DemoKeyValueStore(
            self._manifest.run_root / "main-agent" / "store.json",
            read_only=settings.restore_read_only,
        )
        self._owned_client = False
        if dispatcher is None:
            bridge_config = TaskBridgeConfig.model_validate(
                {
                    "control_plane_url": self._manifest.endpoints.control_plane,
                    "workspace_id": self._manifest.workspace_id,
                    "planning_profile_id": self._manifest.planning_profile_id,
                    "planning_profile_version": self._manifest.planning_profile_version,
                    "web_base_url": self._manifest.endpoints.web,
                    "actor_type": "integration",
                    "actor_id": "interview-demo-main-agent",
                }
            )
            self._client = client or ControlPlaneClient(bridge_config)
            self._dispatcher = CommandDispatcher(
                self._client,
                _bridge_state(self._store),
                owned_command_types={"SubmitPlanningRequest", "SubmitPlanRevision"},
            )
            self._owned_client = client is None
        else:
            self._client = client
            self._dispatcher = dispatcher
        self._interrupted_turns: set[tuple[str, str]] = set()

    async def health(self) -> Mapping[str, JsonValue]:
        return {
            "status": "configured",
            "adapter": "interview-demo",
            "run_id": str(self._manifest.run_id),
        }

    async def capabilities(self) -> Mapping[str, JsonValue]:
        return {
            "thread_rename": False,
            "stream_resume_after_gateway_restart": False,
            "turn_interrupt": True,
            "finite_intents": [
                "create_csv",
                "optional_phone",
                "add_tsv",
                "unsupported",
            ],
        }

    async def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MainAgentThreadPage:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        offset = _cursor_offset(cursor)
        threads = sorted(
            (
                self._thread_from_record(item)
                for item in self._documents()["threads"].values()
            ),
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )
        page = threads[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(threads) else None
        return MainAgentThreadPage(tuple(page), next_cursor)

    async def create_thread(
        self,
        *,
        title: str,
        context_refs: Sequence[str],
    ) -> MainAgentThread:
        normalized_refs = _normalize_context_refs(context_refs)
        now = datetime.now(UTC)
        thread_id = f"demo-thread-{uuid4()}"
        record: dict[str, object] = {
            "id": thread_id,
            "title": title.strip() or "新对话",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "context_refs": list(normalized_refs),
            "turns": [],
        }
        document = self._documents()
        document["threads"][thread_id] = record
        self._save_documents(document)
        return self._thread_from_record(record)

    async def read_thread(self, thread_id: str) -> MainAgentThread:
        record = self._thread_record(thread_id)
        return self._thread_from_record(record)

    async def start_turn(
        self,
        thread_id: str,
        input_text: str,
        *,
        operation_id: str | None = None,
    ) -> MainAgentTurnHandle:
        if not input_text.strip():
            raise ValueError("turn input must not be empty")
        document = self._documents()
        thread = self._thread_record_from_document(document, thread_id)
        for turn in thread["turns"]:
            if isinstance(turn, dict) and turn.get("status") in {"queued", "in_progress"}:
                raise ThreadBusy("thread already has an active turn")
        guard, guarded_operation_id = begin_main_turn(
            self._acceptance_guard,
            operation_id=operation_id,
            thread_id=thread_id,
            input_text=input_text,
        )
        now = datetime.now(UTC)
        turn_id = f"demo-turn-{uuid4()}"
        record = {
            "id": turn_id,
            "thread_id": thread_id,
            "status": "queued",
            "created_at": now.isoformat(),
            "started_at": None,
            "completed_at": None,
            "final_response": None,
            "items": [
                {
                    "id": f"demo-item-{uuid4()}",
                    "type": "userMessage",
                    "data": {"text": input_text},
                }
            ],
            "usage": None,
            "error": None,
            "events": [
                {
                    "sequence": 1,
                    "type": "queued",
                    "data": {"status": "queued"},
                }
            ],
        }
        thread["turns"].append(record)
        thread["updated_at"] = now.isoformat()
        self._save_documents(document)
        settle_main_turn(
            guard,
            guarded_operation_id,
            outcome="completed",
            resource_id=f"turn:{turn_id}",
        )
        return _DemoTurnHandle(self, thread_id, turn_id)

    async def read_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        return self._turn_from_record(self._turn_record(thread_id, turn_id))

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        document = self._documents()
        record = self._thread_record_from_document(document, thread_id, turn_id)
        if record["status"] in {"queued", "in_progress"}:
            self._interrupted_turns.add((thread_id, turn_id))
            now = datetime.now(UTC)
            record["status"] = "interrupted"
            record["completed_at"] = now.isoformat()
            record["error"] = {"code": "interrupted", "message": "turn interrupted"}
            _append_event(record, "interrupted", {"status": "interrupted"})
            thread = document["threads"][thread_id]
            thread["updated_at"] = now.isoformat()
            self._save_documents(document)
        return self._turn_from_record(record)

    async def close(self) -> None:
        try:
            if self._owned_client and self._client is not None:
                await self._client.close()
        finally:
            self._store.close()

    async def _run_turn(self, thread_id: str, turn_id: str) -> None:
        key = (thread_id, turn_id)
        try:
            document = self._documents()
            record = self._thread_record_from_document(document, thread_id, turn_id)
            if record["status"] != "queued":
                return
            now = datetime.now(UTC)
            record["status"] = "in_progress"
            record["started_at"] = now.isoformat()
            _append_event(record, "started", {"status": "in_progress"})
            self._touch_thread(document, thread_id, now)
            self._save_documents(document)

            input_text = _user_input(record)
            intent = classify_demo_intent(input_text)
            if intent == "create_csv":
                await self._run_create_csv(document, thread_id, record, input_text)
            elif intent == "add_tsv":
                await self._run_add_tsv(document, thread_id, record, input_text)
            else:
                response = _response_for_intent(intent)
                _append_assistant(record, response)
                _complete(record, response)
                self._touch_thread(document, thread_id, datetime.now(UTC))
                self._save_documents(document)
        finally:
            self._interrupted_turns.discard(key)

    async def _run_create_csv(
        self,
        document: dict[str, Any],
        thread_id: str,
        record: dict[str, Any],
        input_text: str,
    ) -> None:
        intent = CommandIntent(
            command_type="SubmitPlanningRequest",
            target_type="workspace",
            target_id=None,
            expected_version=None,
            payload={
                "profile_id": str(self._manifest.planning_profile_id),
                "profile_version": self._manifest.planning_profile_version,
                "objective": input_text.strip(),
                "constraints": ["手机号策略在执行阶段向用户确认"],
                "acceptance_criteria": ["CSV 导入完成并通过验证"],
                "context_refs": list(
                    self._thread_record_from_document(document, thread_id).get(
                        "context_refs", []
                    )
                ),
                "requested_outputs": ["planning proposal", "execution plan"],
                "source_type": "sidera_web_chat",
                "source_ref": _DEMO_SOURCE_REF,
            },
        )
        envelope = self._dispatcher.new_envelope(intent)
        tool_item = {
            "id": f"demo-tool-{uuid4()}",
            "type": "toolCall",
            "data": {
                "name": "sidera_submit_brief",
                "tool": "sidera_submit_brief",
                "intent": "create_csv",
                "command_type": envelope.command_type,
                "command_id": str(envelope.command_id),
                "status": "pending",
            },
        }
        record["items"].append(tool_item)
        _append_event(record, "item", {"item": tool_item})
        self._touch_thread(document, thread_id, datetime.now(UTC))
        self._save_documents(document)
        try:
            result = await self._dispatcher.execute(
                f"planning:source:demo:{thread_id}", envelope
            )
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            if result.status != "applied":
                _fail_turn(
                    record,
                    str(result.error_code or "planning_submission_failed"),
                    "规划提交没有得到明确成功回执。",
                )
                self._touch_thread(document, thread_id, datetime.now(UTC))
                self._save_documents(document)
                return
            planning_request_id = _planning_request_id(result)
            if planning_request_id is None:
                raise ProviderProtocolError(
                    "control plane accepted planning request without an id"
                )
            tool_item["data"] = {
                **tool_item["data"],
                "status": "applied",
                "planning_request_id": str(planning_request_id),
                "resultPreview": json.dumps(
                    {
                        "outcome": "ok",
                        "summary": "规划请求已提交，等待用户查看并确认方案。",
                        "resource_ref": f"planning_request:{planning_request_id}",
                        "web_url": (
                            f"/workspaces/{self._manifest.workspace_id}/planning/"
                            f"{planning_request_id}"
                        ),
                        "data": {"planning_request_id": str(planning_request_id)},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
            response = (
                f"需求已提交，规划请求 {planning_request_id} 正在生成提案。"
                "确认提案前不会创建执行任务。"
            )
            _append_event(record, "item", {"item": tool_item})
            _append_assistant(record, response)
            _complete(record, response)
        except (TransportUnavailable, CommandResultUnknown) as exc:
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            _fail_turn(record, "result_unknown", str(exc))
        except ProviderProtocolError as exc:
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            _fail_turn(record, "invalid_control_plane_response", str(exc))
        except Exception as exc:  # noqa: BLE001
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            _fail_turn(record, "planning_submission_failed", str(exc))
        if self._turn_was_interrupted(thread_id, str(record["id"])):
            return
        self._touch_thread(document, thread_id, datetime.now(UTC))
        self._save_documents(document)

    async def _run_add_tsv(
        self,
        document: dict[str, Any],
        thread_id: str,
        record: dict[str, Any],
        input_text: str,
    ) -> None:
        tool_item = {
            "id": f"demo-tool-{uuid4()}",
            "type": "toolCall",
            "data": {
                "name": "sidera_create_plan_revision",
                "tool": "sidera_create_plan_revision",
                "intent": "add_tsv",
                "command_type": "SubmitPlanRevision",
                "status": "pending",
            },
        }
        record["items"].append(tool_item)
        _append_event(record, "item", {"item": tool_item})
        self._touch_thread(document, thread_id, datetime.now(UTC))
        self._save_documents(document)
        try:
            root_task_id = await self._root_task_id_for_thread(document, thread_id)
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            client = self._client
            if root_task_id is None or client is None:
                raise ProviderProtocolError(
                    "the demo conversation has no confirmed root task"
                )
            task = await client.get_task(root_task_id)
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            active_plan = await client.get_active_plan(root_task_id)
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            root_version = _positive_int(task.get("version"))
            base = _active_plan_base(active_plan, root_task_id)
            if root_version is None or base is None:
                raise ProviderProtocolError(
                    "control plane returned an incomplete revision base"
                )
            intent = CommandIntent(
                command_type="SubmitPlanRevision",
                target_type="task",
                target_id=root_task_id,
                expected_version=root_version,
                payload={
                    "root_task_id": str(root_task_id),
                    **base,
                    "objective": input_text.strip(),
                    "constraints": ["保持既有 CSV 校验和报告结果可复用"],
                    "acceptance_criteria": ["CSV 与 TSV 都能完成导入验证"],
                    "planning_profile_id": str(self._manifest.planning_profile_id),
                    "planning_profile_version": self._manifest.planning_profile_version,
                },
            )
            envelope = self._dispatcher.new_envelope(intent)
            tool_item["data"] = {
                **tool_item["data"],
                "command_id": str(envelope.command_id),
                "root_task_id": str(root_task_id),
                "expected_base_generation": base["expected_base_generation"],
            }
            _append_event(record, "item", {"item": tool_item})
            self._touch_thread(document, thread_id, datetime.now(UTC))
            self._save_documents(document)
            result = await self._dispatcher.execute(
                f"planning:root:{root_task_id}", envelope
            )
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            if result.status != "applied":
                _fail_turn(
                    record,
                    str(result.error_code or "planning_revision_submission_failed"),
                    "需求变更没有得到明确成功回执。",
                )
                self._touch_thread(document, thread_id, datetime.now(UTC))
                self._save_documents(document)
                return
            planning_request_id = _planning_request_id(result)
            if planning_request_id is None:
                raise ProviderProtocolError(
                    "control plane accepted revision without an id"
                )
            tool_item["data"] = {
                **tool_item["data"],
                "status": "applied",
                "planning_request_id": str(planning_request_id),
                "resultPreview": json.dumps(
                    {
                        "outcome": "ok",
                        "summary": "需求变更已提交，等待用户查看并确认修订方案。",
                        "resource_ref": f"planning_request:{planning_request_id}",
                        "web_url": (
                            f"/workspaces/{self._manifest.workspace_id}/planning/"
                            f"{planning_request_id}"
                        ),
                        "data": {"planning_request_id": str(planning_request_id)},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
            response = (
                f"需求变更已提交，修订请求 {planning_request_id} 正在基于当前计划生成提案。"
                "确认修订提案前不会切换任务代际或调整 Worker 排布。"
            )
            _append_event(record, "item", {"item": tool_item})
            _append_assistant(record, response)
            _complete(record, response)
        except (TransportUnavailable, CommandResultUnknown) as exc:
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            _fail_turn(record, "result_unknown", str(exc))
        except ProviderProtocolError as exc:
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            _fail_turn(record, "invalid_control_plane_response", str(exc))
        except Exception as exc:  # noqa: BLE001
            if self._turn_was_interrupted(thread_id, str(record["id"])):
                return
            _fail_turn(record, "planning_revision_submission_failed", str(exc))
        if self._turn_was_interrupted(thread_id, str(record["id"])):
            return
        self._touch_thread(document, thread_id, datetime.now(UTC))
        self._save_documents(document)

    def _turn_was_interrupted(self, thread_id: str, turn_id: str) -> bool:
        return (thread_id, turn_id) in self._interrupted_turns

    async def _root_task_id_for_thread(
        self, document: Mapping[str, Any], thread_id: str
    ) -> UUID | None:
        client = self._client
        if client is None:
            return None
        thread = self._thread_record_from_document(
            cast(dict[str, Any], document), thread_id
        )
        for turn in reversed(_mapping_records(thread, "turns")):
            for item in reversed(_mapping_records(turn, "items")):
                if item.get("type") != "toolCall":
                    continue
                data = item.get("data")
                if not isinstance(data, Mapping):
                    continue
                request_id = _parse_uuid(data.get("planning_request_id"))
                if request_id is None:
                    continue
                materialization = await client.get_materialization(request_id)
                root_task_id = _parse_uuid(materialization.get("root_task_id"))
                if root_task_id is not None:
                    return root_task_id
        return None

    def _documents(self) -> dict[str, Any]:
        raw = self._store.get(_CONVERSATIONS_KEY, {"threads": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("threads", {}), dict):
            raise ProviderProtocolError("demo conversation state is corrupt")
        return cast(dict[str, Any], raw)

    def _save_documents(self, document: dict[str, Any]) -> None:
        self._store.set(_CONVERSATIONS_KEY, document)

    def _thread_record(self, thread_id: str) -> dict[str, Any]:
        document = self._documents()
        return self._thread_record_from_document(document, thread_id)

    @staticmethod
    def _thread_record_from_document(
        document: dict[str, Any],
        thread_id: str,
        turn_id: str | None = None,
        *,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        thread = document["threads"].get(thread_id)
        if not isinstance(thread, dict):
            if allow_missing:
                return {}
            raise ThreadNotFound("thread was not found")
        if turn_id is None:
            return thread
        for turn in _mapping_records(thread, "turns"):
            if turn.get("id") == turn_id:
                return cast(dict[str, Any], turn)
        if allow_missing:
            return {}
        raise TurnNotFound("turn was not found")

    def _turn_record(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        document = self._documents()
        return self._thread_record_from_document(document, thread_id, turn_id)

    def _touch_thread(
        self, document: dict[str, Any], thread_id: str, now: datetime
    ) -> None:
        document["threads"][thread_id]["updated_at"] = now.isoformat()

    @staticmethod
    def _thread_from_record(record: Mapping[str, object]) -> MainAgentThread:
        turns = tuple(
            DemoMainAgentAdapter._turn_from_record(item)
            for item in _mapping_records(record, "turns")
        )
        context_refs = _string_values(record, "context_refs")
        return MainAgentThread(
            id=str(record["id"]),
            title=str(record["title"]),
            created_at=datetime.fromisoformat(str(record["created_at"])),
            updated_at=datetime.fromisoformat(str(record["updated_at"])),
            context_refs=context_refs,
            turns=turns,
        )

    @staticmethod
    def _turn_from_record(record: Mapping[str, object]) -> MainAgentTurn:
        items = tuple(
            MainAgentItem(
                id=str(item["id"]),
                type=cast(Any, item["type"]),
                data=cast(Mapping[str, JsonValue], item.get("data", {})),
            )
            for item in _mapping_records(record, "items")
        )
        return MainAgentTurn(
            id=str(record["id"]),
            thread_id=str(record["thread_id"]),
            status=cast(Any, record["status"]),
            created_at=datetime.fromisoformat(str(record["created_at"])),
            started_at=_optional_datetime(record.get("started_at")),
            completed_at=_optional_datetime(record.get("completed_at")),
            final_response=cast(str | None, record.get("final_response")),
            items=items,
            usage=cast(Mapping[str, JsonValue] | None, record.get("usage")),
            error=cast(Mapping[str, JsonValue] | None, record.get("error")),
        )


class _DemoTurnHandle(MainAgentTurnHandle):
    def __init__(self, adapter: DemoMainAgentAdapter, thread_id: str, turn_id: str) -> None:
        self._adapter = adapter
        self.thread_id = thread_id
        self.id = turn_id

    async def events(self) -> AsyncIterator[MainAgentEvent]:
        await self._adapter._run_turn(self.thread_id, self.id)
        record = self._adapter._turn_record(self.thread_id, self.id)
        for event in _mapping_records(record, "events"):
            yield MainAgentEvent(
                sequence=_integer_value(event, "sequence"),
                type=cast(EventType, event["type"]),
                data=cast(Mapping[str, JsonValue], event.get("data", {})),
            )

    async def result(self) -> MainAgentTurn:
        await self._adapter._run_turn(self.thread_id, self.id)
        return await self._adapter.read_turn(self.thread_id, self.id)

    async def interrupt(self) -> MainAgentTurn:
        return await self._adapter.interrupt_turn(self.thread_id, self.id)


def _bridge_state(store: DemoKeyValueStore) -> Any:
    from sidera_task_bridge.state import BridgeStateStore

    return BridgeStateStore(store)


def _normalize_context_refs(context_refs: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in context_refs:
        if not isinstance(item, str) or not item.strip() or len(item) > 200:
            raise ValueError("context refs are invalid")
        if item not in seen:
            seen.add(item)
            values.append(item)
    if len(values) > 20:
        raise ValueError("too many context refs")
    return tuple(values)


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise ValueError("cursor is invalid") from exc
    if value < 0:
        raise ValueError("cursor is invalid")
    return value


def _user_input(record: Mapping[str, object]) -> str:
    for item in _mapping_records(record, "items"):
        if item.get("type") == "userMessage":
            data = item.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("text"), str):
                return data["text"]
    raise ProviderProtocolError("demo turn has no user input")


def _mapping_records(
    record: Mapping[str, object], field_name: str
) -> tuple[Mapping[str, object], ...]:
    value = record.get(field_name, [])
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise ProviderProtocolError(f"demo {field_name} state is corrupt")
    return tuple(cast(Mapping[str, object], item) for item in value)


def _string_values(
    record: Mapping[str, object], field_name: str
) -> tuple[str, ...]:
    value = record.get(field_name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProviderProtocolError(f"demo {field_name} state is corrupt")
    return tuple(value)


def _integer_value(record: Mapping[str, object], field_name: str) -> int:
    value = record.get(field_name)
    if type(value) is not int:
        raise ProviderProtocolError(f"demo {field_name} state is corrupt")
    return value


def _response_for_intent(intent: DemoIntent) -> str:
    return {
        "optional_phone": "我会把手机号策略留给校验 Worker，在执行阶段向你确认，不提前替你决定。",
        "add_tsv": "已识别为整体需求变更。需要在当前计划基础上生成修订提案，确认后再调整 Worker 排布。",
        "unsupported": "当前演示主 Agent 只处理 CSV 导入、手机号策略确认和 TSV 需求变更。这个输入不会触发控制面命令。",
        "create_csv": "",
    }[intent]


def _append_event(
    record: dict[str, Any], event_type: EventType, data: Mapping[str, JsonValue]
) -> None:
    events = record["events"]
    sequence = max((int(item["sequence"]) for item in events), default=0) + 1
    events.append({"sequence": sequence, "type": event_type, "data": dict(data)})


def _append_assistant(record: dict[str, Any], response: str) -> None:
    item = {
        "id": f"demo-assistant-{uuid4()}",
        "type": "assistantMessage",
        "data": {"content": response},
    }
    record["items"].append(item)
    _append_event(record, "item", {"item": item})


def _complete(record: dict[str, Any], response: str) -> None:
    record["status"] = "completed"
    record["completed_at"] = datetime.now(UTC).isoformat()
    record["final_response"] = response
    _append_event(record, "completed", {"status": "completed"})


def _fail_turn(record: dict[str, Any], code: str, message: str) -> None:
    record["status"] = "failed"
    record["completed_at"] = datetime.now(UTC).isoformat()
    record["error"] = {"code": code, "message": message}
    _append_event(record, "failed", {"code": code})


def _planning_request_id(result: DispatchResult) -> UUID | None:
    execution = result.execution
    if not isinstance(execution, CommandExecution) or execution.result is None:
        return None
    value = execution.result.data.get("planning_request_id")
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _parse_uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _positive_int(value: object) -> int | None:
    if type(value) is not int or value < 1:
        return None
    return value


def _active_plan_base(
    active_plan: Mapping[str, object], task_id: UUID
) -> dict[str, JsonValue] | None:
    returned_task_id = _parse_uuid(active_plan.get("root_task_id"))
    snapshot_id = _parse_uuid(active_plan.get("active_snapshot_id"))
    generation = _positive_int(active_plan.get("active_generation"))
    content_hash = active_plan.get("active_snapshot_hash")
    if not isinstance(content_hash, str) or not content_hash.strip():
        return None
    if returned_task_id is not None and returned_task_id != task_id:
        return None
    if snapshot_id is None or generation is None:
        return None
    return {
        "expected_base_snapshot_id": str(snapshot_id),
        "expected_base_generation": generation,
        "expected_base_content_hash": content_hash.strip(),
    }


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))
