import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from threading import Condition, Lock
from time import monotonic
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, JsonValue, TypeAdapter
from sidera_agents.api import AgentCatalog, AgentExecution, AgentRuntimes
from sidera_agents.contracts import (
    ExecutionWorkspace,
    InputReceipt,
    InputStatus,
    RunEvent,
    RunInput,
    RunPolicy,
    RunSnapshot,
    RunStart,
    RunState,
    RunStop,
    StartReceipt,
    StartStatus,
    StopStatus,
    ToolBinding,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

from sidera_product.auth.grants import ProductGrants
from sidera_product.auth.local_session import LocalSessions
from sidera_product.config import ProductSettings
from sidera_product.postgres.authority_store import (
    AgentGrantRecord,
    GrantStore,
    ReceiptReader,
)
from sidera_product.postgres.chat_store import PostgresChatStore
from sidera_product.postgres.store import PostgresProductStore, ProductStoreError

from .contracts import (
    ChatEvent,
    ChatTurn,
    Conversation,
    CreateThreadBody,
    RequestState,
    StartTurnBody,
    ThreadView,
    TurnItem,
    TurnView,
)
from .projection import (
    ProjectionError,
    event_frames,
    project_turn,
    receipt_items,
    resume_cursor,
)


def _json(model: BaseModel) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        model.model_dump(mode="json")
    )


class ChatError(Exception):
    def __init__(
        self, code: str, *, status_code: int = 409, retryable: bool = False
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(code)


class ChatService:
    def __init__(
        self,
        *,
        settings: ProductSettings,
        product_store: PostgresProductStore,
        store: PostgresChatStore,
        sessions: LocalSessions,
        catalog: AgentCatalog,
        runtimes: AgentRuntimes,
        execution: AgentExecution,
        grants: ProductGrants,
        authority_store: GrantStore,
        receipt_store: ReceiptReader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.product_store = product_store
        self.store = store
        self.sessions = sessions
        self.catalog = catalog
        self.runtimes = runtimes
        self.execution = execution
        self.grants = grants
        self.authority_store = authority_store
        self.receipt_store = receipt_store
        self.clock = clock or (lambda: datetime.now(UTC))
        self._condition = Condition()
        self._closing = False
        self._invocations = 0
        self._close_lock = Lock()
        self._used = False
        self._close_complete = False

    def _current(self, identity: ControlPlaneIdentity) -> None:
        if (
            identity.actor_type != "user"
            or identity.principal_id != self.settings.local_principal_id
            or self.sessions.identity_for_grant(identity.grant_id) != identity
        ):
            raise ChatError("local_session_required", status_code=401)

    def create_thread(
        self, identity: ControlPlaneIdentity, body: CreateThreadBody
    ) -> Conversation:
        with self._condition:
            if self._closing:
                raise ChatError("shutdown_pending", retryable=True)
            self._invocations += 1
        try:
            return self._create_thread(identity, body)
        finally:
            with self._condition:
                self._invocations -= 1
                self._condition.notify_all()

    def _create_thread(
        self, identity: ControlPlaneIdentity, body: CreateThreadBody
    ) -> Conversation:
        self._current(identity)
        with self._condition:
            if self._closing:
                raise ChatError("shutdown_pending", retryable=True)
        ref = body.profile_ref
        if ref is None:
            defaults = self.product_store.get_defaults(self.settings.workspace_id)
            if defaults is None or defaults.main_profile_id is None:
                raise ChatError("configuration_unavailable", status_code=503)
            choices = tuple(
                profile.ref
                for profile in self.catalog.list_profiles(
                    self.settings.workspace_id, grant_id=identity.grant_id
                )
                if profile.ref.profile_id == defaults.main_profile_id
            )
            if len(choices) != 1:
                raise ChatError("configuration_unavailable", status_code=503)
            ref = choices[0]
        if ref.workspace_id != self.settings.workspace_id:
            raise ChatError("permission_denied", status_code=403)
        profile = self.catalog.get_profile(ref, grant_id=identity.grant_id)
        if profile.ref != ref:
            raise ChatError("permission_denied", status_code=403)
        status = self.catalog.get_profile_status(ref, grant_id=identity.grant_id)
        if status.profile_id != ref.profile_id or not status.enabled:
            raise ChatError("profile_unavailable")
        now = self.clock()
        with self._condition:
            if self._closing:
                raise ChatError("shutdown_pending", retryable=True)
            self._used = True
        return self.store.create_conversation(
            Conversation(
                id=uuid4(),
                workspace_id=self.settings.workspace_id,
                principal_id=identity.principal_id,
                title=body.title or "New conversation",
                context_refs=body.context_refs,
                profile_ref=ref,
                created_at=now,
                updated_at=now,
                version=1,
            )
        )

    def read_thread(
        self, identity: ControlPlaneIdentity, conversation_id: UUID
    ) -> Conversation:
        self._current(identity)
        self._used = True
        conversation = self.store.get_conversation(
            conversation_id=conversation_id,
            workspace_id=self.settings.workspace_id,
            principal_id=identity.principal_id,
        )
        if conversation is None:
            raise ChatError("thread_not_found", status_code=404)
        return conversation

    def list_threads(self, identity: ControlPlaneIdentity) -> tuple[Conversation, ...]:
        self._current(identity)
        self._used = True
        return self.store.list_conversations(
            workspace_id=self.settings.workspace_id, principal_id=identity.principal_id
        )

    def read_thread_view(
        self, identity: ControlPlaneIdentity, conversation_id: UUID
    ) -> ThreadView:
        conversation = self.read_thread(identity, conversation_id)
        turns = self.store.list_turns(conversation)
        views = tuple(
            self.read_turn(identity, conversation_id, turn.id) for turn in turns
        )
        current = self.read_thread(identity, conversation_id)
        return ThreadView(
            id=str(current.id),
            title=current.title,
            createdAt=current.created_at,
            updatedAt=current.updated_at,
            contextRefs=current.context_refs,
            profileRef=current.profile_ref,
            runId=current.run_id,
            turns=views,
        )

    def _saved_grant(self, conversation: Conversation) -> AgentGrantRecord:
        if conversation.grant_id is None or conversation.start_operation_id is None:
            raise ChatError("original_authority_unavailable", status_code=403)
        record = self.authority_store.find_grant(grant_id=conversation.grant_id)
        if (
            record is None
            or record.source != "product"
            or record.role != "main"
            or record.workspace_id != conversation.workspace_id
            or record.principal_id != conversation.principal_id
            or record.profile_ref != conversation.profile_ref
            or record.start_operation_id != conversation.start_operation_id
            or record.connection_id != self.grants.connection_id
            or (
                record.run_id is not None
                and conversation.run_id is not None
                and record.run_id != conversation.run_id
            )
        ):
            raise ChatError("original_authority_unavailable", status_code=403)
        return record

    def _original_grant(self, conversation: Conversation) -> AgentGrantRecord:
        record = self._saved_grant(conversation)
        self.grants.require(record.grant_id, "profile.read", conversation.profile_ref)
        return record

    def _checked_run(
        self,
        conversation: Conversation,
        receipt: StartReceipt,
        record: AgentGrantRecord,
    ) -> RunSnapshot:
        if (
            receipt.operation_id != conversation.start_operation_id
            or receipt.status is not StartStatus.ACCEPTED
            or receipt.run_id is None
            or (
                conversation.run_id is not None
                and receipt.run_id != conversation.run_id
            )
            or (record.run_id is not None and receipt.run_id != record.run_id)
        ):
            raise ChatError("original_run_mismatch", status_code=403)
        run = self.execution.get_run(
            conversation.workspace_id, receipt.run_id, grant_id=record.grant_id
        )
        if (
            run.run_id != receipt.run_id
            or run.workspace_id != conversation.workspace_id
            or run.profile_ref != conversation.profile_ref
            or run.effective_config.profile_ref != conversation.profile_ref
            or run.policy is not RunPolicy.CONVERSATION
        ):
            raise ChatError("original_run_mismatch", status_code=403)
        fresh = self._original_grant(conversation)
        if fresh.run_id is not None and fresh.run_id != run.run_id:
            raise ChatError("original_run_mismatch", status_code=403)
        return run

    def _original_run(self, conversation: Conversation) -> RunSnapshot:
        record = self._original_grant(conversation)
        if conversation.start_operation_id is None or conversation.run_id is None:
            raise ChatError("original_operation_unknown", retryable=True)
        lookup = self.execution.get_start(
            conversation.workspace_id,
            conversation.start_operation_id,
            grant_id=record.grant_id,
        )
        receipt = lookup.receipt
        if (
            not lookup.found
            or receipt is None
            or receipt.operation_id != conversation.start_operation_id
            or receipt.status is not StartStatus.ACCEPTED
            or receipt.run_id != conversation.run_id
        ):
            raise ChatError("original_run_mismatch", status_code=403)
        return self._checked_run(conversation, receipt, record)

    def _workspace(self, conversation: Conversation) -> ExecutionWorkspace:
        if conversation.execution_workspace is not None:
            return conversation.execution_workspace
        directories: list[str] = []
        for root in (self.settings.workspace_root, self.settings.artifact_root):
            canonical = root.resolve(strict=True)
            path = canonical / "conversations" / str(conversation.id)
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.resolve(strict=True) != path or not path.is_dir():
                raise ChatError("workspace_unavailable")
            directories.append(str(path))
        return ExecutionWorkspace(
            directory=directories[0], writable=True, artifact_directory=directories[1]
        )

    def _settle(
        self,
        conversation: Conversation,
        turn: ChatTurn,
        receipt: StartReceipt | InputReceipt,
        *,
        observation: bool = False,
    ) -> ChatTurn:
        if receipt.operation_id != turn.native_operation_id:
            raise ChatError("original_operation_mismatch", status_code=403)
        state: RequestState
        if isinstance(receipt, StartReceipt):
            state = receipt.status.value
            if receipt.status is StartStatus.ACCEPTED:
                current = self.store.get_conversation(
                    conversation_id=conversation.id,
                    workspace_id=conversation.workspace_id,
                    principal_id=conversation.principal_id,
                )
                if current is None:
                    raise ChatError("thread_not_found", status_code=404)
                self._checked_run(current, receipt, self._original_grant(current))
        else:
            if receipt.run_id != conversation.run_id:
                raise ChatError("original_run_mismatch", status_code=403)
            if (
                receipt.status is InputStatus.REJECTED
                and receipt.reason_code == "resource_busy"
            ):
                state = "busy"
            elif receipt.status is InputStatus.FAILED:
                state = "rejected"
            else:
                state = receipt.status.value
        return self.store.settle_turn(
            conversation,
            turn.id,
            state=state,
            reason_code=receipt.reason_code,
            run_id=receipt.run_id,
            now=self.clock(),
            expected=turn,
            observation=observation,
        )

    def _reconcile(self, conversation: Conversation, turn: ChatTurn) -> ChatTurn:
        if turn.request_state in ("claimed", "prepared", "busy", "rejected"):
            return turn
        if conversation.grant_id is None:
            return turn
        grant_id = self._original_grant(conversation).grant_id
        if turn.kind == "start":
            lookup = self.execution.get_start(
                conversation.workspace_id, turn.native_operation_id, grant_id=grant_id
            )
            if not lookup.found or lookup.receipt is None:
                return self.store.settle_turn(
                    conversation,
                    turn.id,
                    state="unknown",
                    reason_code="original_operation_unknown",
                    now=self.clock(),
                    expected=turn,
                    observation=True,
                )
            return self._settle(conversation, turn, lookup.receipt, observation=True)
        if conversation.run_id is None:
            raise ChatError("original_run_mismatch", status_code=403)
        self._original_run(conversation)
        return self._settle(
            conversation,
            turn,
            self.execution.get_input(
                conversation.workspace_id,
                conversation.run_id,
                turn.native_operation_id,
                grant_id=grant_id,
            ),
            observation=True,
        )

    def start_turn(
        self, identity: ControlPlaneIdentity, conversation_id: UUID, body: StartTurnBody
    ) -> ChatTurn:
        conversation = self.read_thread(identity, conversation_id)
        operation_id = (
            body.operation_id if body.operation_id is not None else str(uuid4())
        )
        native_operation_id = uuid5(
            conversation.id, "sidera.chat.input:" + operation_id
        )
        fingerprint = sha256(
            json.dumps(
                {
                    "conversation": str(conversation.id),
                    "workspace": str(conversation.workspace_id),
                    "principal": conversation.principal_id,
                    "profile": _json(conversation.profile_ref),
                    "context": conversation.context_refs,
                    "operation": operation_id,
                    "input": body.input,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        with self._condition:
            if self._closing:
                raise ChatError("shutdown_pending", retryable=True)
            self._invocations += 1
        try:
            try:
                conversation, turn, winner = self.store.claim_turn(
                    conversation,
                    operation_id=operation_id,
                    native_operation_id=native_operation_id,
                    fingerprint=fingerprint,
                    input_text=body.input,
                    now=self.clock(),
                )
            except ProductStoreError as error:
                raise ChatError(
                    error.code, retryable=error.code == "thread_busy"
                ) from None
            if not winner:
                return self._reconcile(conversation, turn)
            if turn.native_request is None:
                try:
                    workspace = self._workspace(conversation)
                    if turn.kind == "start":
                        grant_id = self.grants.prepare_main_scope(
                            session_grant_id=identity.grant_id,
                            operation_id=turn.native_operation_id,
                            profile_ref=conversation.profile_ref,
                            workspace=workspace,
                            sources=(),
                        )
                        request = RunStart(
                            operation_id=turn.native_operation_id,
                            profile_ref=conversation.profile_ref,
                            grant_id=grant_id,
                            policy=RunPolicy.CONVERSATION,
                            instructions=(),
                            text=turn.input_text,
                            context=(),
                            tools=(
                                ToolBinding(connection_id=self.grants.connection_id),
                            ),
                            workspace=workspace,
                            required_capabilities=("mcp",),
                        )
                    else:
                        run = self._original_run(conversation)
                        if run.state is not RunState.IDLE or not run.input_open:
                            raise ChatError(
                                "run_closed"
                                if run.state
                                in (
                                    RunState.STOPPED,
                                    RunState.COMPLETED,
                                    RunState.FAILED,
                                )
                                else "run_unavailable"
                            )
                        grant_id = self._original_grant(conversation).grant_id
                        request = RunInput(
                            workspace_id=conversation.workspace_id,
                            run_id=run.run_id,
                            operation_id=turn.native_operation_id,
                            grant_id=grant_id,
                            text=turn.input_text,
                            context=(),
                        )
                    turn = self.store.prepare_turn(
                        conversation,
                        turn.id,
                        grant_id=grant_id,
                        workspace=workspace,
                        request=_json(request),
                        now=self.clock(),
                    )
                except (AgentError, ChatError) as error:
                    return self.store.settle_turn(
                        conversation,
                        turn.id,
                        state="rejected",
                        reason_code=error.code,
                        now=self.clock(),
                        expected=turn,
                    )
            with self._condition:
                if self._closing:
                    return self.store.settle_turn(
                        conversation,
                        turn.id,
                        state="rejected",
                        reason_code="shutdown_pending",
                        now=self.clock(),
                        expected=turn,
                    )
            dispatched = self.store.mark_dispatching(
                conversation, turn, now=self.clock()
            )
            if dispatched is None:
                return self._reconcile(conversation, turn)
            turn = dispatched
            try:
                if turn.kind == "start":
                    receipt = self.execution.start(
                        RunStart.model_validate(turn.native_request)
                    )
                else:
                    receipt = self.execution.send_input(
                        RunInput.model_validate(turn.native_request)
                    )
            except AgentError as error:
                non_admitted = error.code in {
                    "permission_denied",
                    "configuration_invalid",
                    "profile_reference_mismatch",
                }
                return self.store.settle_turn(
                    conversation,
                    turn.id,
                    state="rejected" if non_admitted else "unknown",
                    reason_code=error.code,
                    now=self.clock(),
                    expected=turn,
                )
            except Exception:  # noqa: BLE001 - uncertain native invocation must never be resent
                return self.store.settle_turn(
                    conversation,
                    turn.id,
                    state="unknown",
                    reason_code="native_operation_unknown",
                    now=self.clock(),
                    expected=turn,
                )
            return self._settle(conversation, turn, receipt)
        finally:
            with self._condition:
                self._invocations -= 1
                self._condition.notify_all()

    @staticmethod
    def _user_view(
        conversation: Conversation, turn: ChatTurn, code: str | None = None
    ) -> TurnView:
        previous = (
            None
            if turn.cached_view is None
            else TurnView.model_validate(turn.cached_view)
        )
        status = (
            previous.status
            if previous is not None
            and previous.status in ("completed", "interrupted", "failed", "cancelled")
            else "failed"
            if turn.request_state in ("busy", "rejected")
            else "queued"
        )
        reason = code or turn.reason_code
        return TurnView(
            id=str(turn.id),
            threadId=str(conversation.id),
            status=status,
            createdAt=turn.created_at,
            items=(
                TurnItem(
                    id=f"{turn.id}:user",
                    type="userMessage",
                    data={"text": turn.input_text},
                ),
            ),
            profileRef=conversation.profile_ref,
            runId=conversation.run_id,
            operationId=turn.operation_id,
            operationState=turn.request_state,
            recoveryMode="poll" if status == "queued" and code is None else None,
            error=None
            if reason is None
            else {
                "code": reason,
                "message": reason,
                "retryable": turn.request_state == "busy" and code is None,
            },
            interruptReasonCode="response_not_current",
        )

    def _events(
        self, conversation: Conversation, run: RunSnapshot
    ) -> tuple[RunEvent, ...]:
        record = self._original_grant(conversation)
        events: list[RunEvent] = []
        after = 0
        while after < run.last_event_sequence:
            page = self.execution.list_events(
                conversation.workspace_id,
                run.run_id,
                after,
                500,
                grant_id=record.grant_id,
            )
            if any(
                event.run_id != run.run_id or event.sequence <= after
                for event in page.events
            ):
                raise ProjectionError("native_cursor_invalid")
            selected = tuple(
                event
                for event in page.events
                if event.sequence <= run.last_event_sequence
            )
            if not selected:
                raise ProjectionError("resync_required")
            events.extend(selected)
            after = selected[-1].sequence
            if not page.has_more:
                break
        return tuple(events)

    def read_turn(
        self, identity: ControlPlaneIdentity, conversation_id: UUID, turn_id: UUID
    ) -> TurnView:
        conversation = self.read_thread(identity, conversation_id)
        turn = self.store.get_turn(conversation, turn_id)
        if turn is None:
            raise ChatError("turn_not_found", status_code=404)
        if turn.request_state in ("claimed", "prepared", "busy", "rejected"):
            return self._user_view(conversation, turn)
        try:
            turn = self._reconcile(conversation, turn)
            conversation = self.read_thread(identity, conversation_id)
            if conversation.run_id is None:
                return self._user_view(conversation, turn)
            run = self._original_run(conversation)
            events = self._events(conversation, run)
            view = project_turn(turn, run, events)
            self._original_grant(conversation)
        except (AgentError, ChatError, ProjectionError) as error:
            return self._user_view(conversation, turn, error.code)
        self._current(identity)
        saved = self.store.cache_turn(conversation, turn, view, now=self.clock())
        view = (
            view
            if saved.cached_view is None
            else TurnView.model_validate(saved.cached_view)
        )
        if view.response_id is None:
            return view
        try:
            record = self._original_grant(conversation)
            rows = self.receipt_store.list_receipts(
                grant_id=record.grant_id,
                run_id=run.run_id,
                response_id=view.response_id,
            )
            self._original_grant(conversation)
        except (AgentError, ChatError) as error:
            return self._user_view(conversation, turn, error.code)
        self._current(identity)
        return view.model_copy(
            update={
                "items": (
                    *view.items,
                    *receipt_items(
                        rows,
                        grant_id=record.grant_id,
                        run_id=run.run_id,
                        response_id=view.response_id,
                        workspace_id=conversation.workspace_id,
                    ),
                )
            }
        )

    def interrupt_turn(
        self, identity: ControlPlaneIdentity, conversation_id: UUID, turn_id: UUID
    ) -> TurnView:
        view = self.read_turn(identity, conversation_id, turn_id)
        raise ChatError(view.interrupt_reason_code or "cancel_unconfirmed")

    def read_events(
        self,
        identity: ControlPlaneIdentity,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        after: int,
        last_event_id: str | None = None,
    ) -> tuple[tuple[ChatEvent, ...], int, bool]:
        conversation = self.read_thread(identity, conversation_id)
        turn = self.store.get_turn(conversation, turn_id)
        if turn is None:
            raise ChatError("turn_not_found", status_code=404)
        turn = self._reconcile(conversation, turn)
        conversation = self.read_thread(identity, conversation_id)
        run = self._original_run(conversation)
        events = self._events(conversation, run)
        cursor = resume_cursor(after, last_event_id, head=run.last_event_sequence)
        view = project_turn(turn, run, events)
        frames = event_frames(turn, run, events, after=cursor)
        self._original_grant(conversation)
        self._current(identity)
        self.store.cache_turn(conversation, turn, view, now=self.clock())
        return (
            frames,
            run.last_event_sequence,
            self._closing or view.status not in ("queued", "in_progress"),
        )

    def activate(self) -> None:
        """A ready owner must drain saved conversations, including after restart."""
        with self._condition:
            if self._closing:
                raise ChatError("shutdown_pending")
            self._used = True

    def begin_close(self) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()

    def _close_conversation(self, original: Conversation) -> bool:
        if original.stop_state == "stopped":
            return True
        conversation = self.store.prepare_stop(original, now=self.clock())
        if conversation.run_id is None:
            turns = self.store.list_turns(conversation)
            if all(
                turn.dispatch_attempt == 0 or turn.request_state in ("busy", "rejected")
                for turn in turns
            ):
                self.store.settle_stop(conversation, "stopped", now=self.clock())
                return True
        record = self._saved_grant(conversation)
        self.grants.require(record.grant_id, "run.stop", conversation.workspace_id)
        if conversation.start_operation_id is None:
            return False
        lookup = self.execution.get_start(
            conversation.workspace_id,
            conversation.start_operation_id,
            grant_id=record.grant_id,
        )
        receipt = lookup.receipt
        if (
            not lookup.found
            or receipt is None
            or receipt.status is not StartStatus.ACCEPTED
            or receipt.run_id is None
        ):
            return False
        if (
            receipt.operation_id != conversation.start_operation_id
            or (
                conversation.run_id is not None
                and receipt.run_id != conversation.run_id
            )
            or (record.run_id is not None and receipt.run_id != record.run_id)
        ):
            return False
        run = self.execution.get_run(
            conversation.workspace_id, receipt.run_id, grant_id=record.grant_id
        )
        if (
            run.run_id != receipt.run_id
            or run.workspace_id != conversation.workspace_id
            or run.profile_ref != conversation.profile_ref
            or run.effective_config.profile_ref != conversation.profile_ref
            or run.policy is not RunPolicy.CONVERSATION
        ):
            return False
        if conversation.run_id is None:
            conversation = self.store.bind_original_run(
                conversation, run.run_id, now=self.clock()
            )
        operation_id = conversation.stop_operation_id
        if operation_id is None:
            return False
        if self.store.mark_stop_dispatching(conversation, now=self.clock()):
            try:
                result = self.execution.stop(
                    RunStop(
                        workspace_id=conversation.workspace_id,
                        run_id=run.run_id,
                        operation_id=operation_id,
                        grant_id=record.grant_id,
                    )
                )
            except Exception:  # noqa: BLE001 - an uncertain original Stop must only be queried
                self.store.settle_stop(conversation, "unknown", now=self.clock())
                return False
        else:
            result = self.execution.get_stop(
                conversation.workspace_id,
                run.run_id,
                operation_id,
                grant_id=record.grant_id,
            )
        if result.operation_id != operation_id or result.run_id != run.run_id:
            return False
        self.store.settle_stop(conversation, result.status.value, now=self.clock())
        return result.status is StopStatus.STOPPED

    def close(self, *, grace_seconds: float) -> bool:
        if not math.isfinite(grace_seconds) or not 0 <= grace_seconds <= 300:
            raise ValueError("A finite bounded close grace is required")
        deadline = monotonic() + grace_seconds
        self.begin_close()
        with self._condition:
            while self._invocations:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
        if not self._close_lock.acquire(timeout=max(0.0, deadline - monotonic())):
            return False
        try:
            if self._close_complete:
                return True
            if not self._used:
                self._close_complete = True
                return True
            while True:
                conversations = self.store.list_conversations(
                    workspace_id=self.settings.workspace_id,
                    principal_id=self.settings.local_principal_id,
                )
                complete = True
                for conversation in conversations:
                    if not self._close_conversation(conversation):
                        complete = False
                if complete:
                    self._close_complete = True
                    return True
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                with self._condition:
                    self._condition.wait(timeout=min(0.05, remaining))
        except Exception:  # noqa: BLE001 - pending original resources remain owned for reconciliation
            return False
        finally:
            self._close_lock.release()
