from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError
from sidera_agents.contracts import ExecutionWorkspace, ProfileRef
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sidera_product.chat.contracts import (
    ChatTurn,
    Conversation,
    RequestState,
    StopState,
    TurnView,
)

from .models import ChatTurnRow, ConversationRow
from .store import ProductStoreError


def _conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        id=row.id,
        workspace_id=row.workspace_id,
        principal_id=row.principal_id,
        title=row.title,
        context_refs=tuple(row.context_refs),
        profile_ref=ProfileRef.model_validate(row.profile_ref),
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        active_turn_id=row.active_turn_id,
        grant_id=row.grant_id,
        start_operation_id=row.start_operation_id,
        run_id=row.run_id,
        execution_workspace=None
        if row.execution_workspace is None
        else ExecutionWorkspace.model_validate(row.execution_workspace),
        stop_operation_id=row.stop_operation_id,
        stop_state=None
        if row.stop_state is None
        else TypeAdapter[StopState](StopState).validate_python(row.stop_state),
    )


def _turn(row: ChatTurnRow) -> ChatTurn:
    return ChatTurn(
        id=row.id,
        conversation_id=row.conversation_id,
        workspace_id=row.workspace_id,
        principal_id=row.principal_id,
        operation_id=row.operation_id,
        native_operation_id=row.native_operation_id,
        request_fingerprint=row.request_fingerprint,
        input_text=row.input_text,
        kind=TypeAdapter[Literal["start", "input"]](
            Literal["start", "input"]
        ).validate_python(row.kind),
        request_state=TypeAdapter[RequestState](RequestState).validate_python(
            row.request_state
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        dispatch_attempt=row.dispatch_attempt,
        native_request=row.native_request,
        response_id=row.response_id,
        reason_code=row.reason_code,
        cached_view=row.cached_view,
        interrupt_operation_id=row.interrupt_operation_id,
        interrupt_state=row.interrupt_state,
    )


def _owned(conversation_id: UUID, workspace_id: UUID, principal_id: str):
    return select(ConversationRow).where(
        ConversationRow.id == conversation_id,
        ConversationRow.workspace_id == workspace_id,
        ConversationRow.principal_id == principal_id,
    )


def _owned_turns(conversation: Conversation):
    return select(ChatTurnRow).where(
        ChatTurnRow.conversation_id == conversation.id,
        ChatTurnRow.workspace_id == conversation.workspace_id,
        ChatTurnRow.principal_id == conversation.principal_id,
    )


def _lock_rows(session: Session, conversation: Conversation, turn_id: UUID):
    thread = session.execute(
        _owned(
            conversation.id, conversation.workspace_id, conversation.principal_id
        ).with_for_update()
    ).scalar_one_or_none()
    turn = session.execute(
        _owned_turns(conversation).where(ChatTurnRow.id == turn_id).with_for_update()
    ).scalar_one_or_none()
    if thread is None or turn is None:
        raise ProductStoreError("turn_not_found")
    return thread, turn


class PostgresChatStore:
    """Conversation/request journal on the product's existing owned engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_conversation(self, conversation: Conversation) -> Conversation:
        row = ConversationRow(
            id=conversation.id,
            workspace_id=conversation.workspace_id,
            principal_id=conversation.principal_id,
            title=conversation.title,
            context_refs=list(conversation.context_refs),
            profile_ref=conversation.profile_ref.model_dump(mode="json"),
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            version=conversation.version,
            active_turn_id=conversation.active_turn_id,
            grant_id=conversation.grant_id,
            start_operation_id=conversation.start_operation_id,
            run_id=conversation.run_id,
            execution_workspace=None
            if conversation.execution_workspace is None
            else conversation.execution_workspace.model_dump(mode="json"),
            stop_operation_id=conversation.stop_operation_id,
            stop_state=conversation.stop_state,
        )
        try:
            with Session(self.engine) as session, session.begin():
                session.add(row)
                session.flush()
                return _conversation(row)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def get_conversation(
        self, *, conversation_id: UUID, workspace_id: UUID, principal_id: str
    ) -> Conversation | None:
        try:
            with Session(self.engine) as session:
                row = session.execute(
                    select(ConversationRow).where(
                        ConversationRow.id == conversation_id,
                        ConversationRow.workspace_id == workspace_id,
                        ConversationRow.principal_id == principal_id,
                    )
                ).scalar_one_or_none()
                return None if row is None else _conversation(row)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def list_conversations(
        self, *, workspace_id: UUID, principal_id: str
    ) -> tuple[Conversation, ...]:
        try:
            with Session(self.engine) as session:
                rows = (
                    session.execute(
                        select(ConversationRow)
                        .where(
                            ConversationRow.workspace_id == workspace_id,
                            ConversationRow.principal_id == principal_id,
                        )
                        .order_by(ConversationRow.created_at.desc(), ConversationRow.id)
                    )
                    .scalars()
                    .all()
                )
                return tuple(_conversation(row) for row in rows)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def list_turns(self, conversation: Conversation) -> tuple[ChatTurn, ...]:
        try:
            with Session(self.engine) as session:
                rows = (
                    session.execute(
                        _owned_turns(conversation).order_by(
                            ChatTurnRow.created_at, ChatTurnRow.id
                        )
                    )
                    .scalars()
                    .all()
                )
                return tuple(_turn(row) for row in rows)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def get_turn(self, conversation: Conversation, turn_id: UUID) -> ChatTurn | None:
        try:
            with Session(self.engine) as session:
                row = session.execute(
                    _owned_turns(conversation).where(ChatTurnRow.id == turn_id)
                ).scalar_one_or_none()
                return None if row is None else _turn(row)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def claim_turn(
        self,
        conversation: Conversation,
        *,
        operation_id: str,
        native_operation_id: UUID,
        fingerprint: str,
        input_text: str,
        now: datetime,
    ) -> tuple[Conversation, ChatTurn, bool]:
        try:
            with Session(self.engine) as session, session.begin():
                thread = session.execute(
                    _owned(
                        conversation.id,
                        conversation.workspace_id,
                        conversation.principal_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if thread is None:
                    raise ProductStoreError("thread_not_found")
                existing = session.execute(
                    _owned_turns(conversation).where(
                        ChatTurnRow.operation_id == operation_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if (
                        existing.request_fingerprint != fingerprint
                        or existing.native_operation_id != native_operation_id
                    ):
                        raise ProductStoreError("operation_conflict")
                    if existing.request_state != "busy":
                        return _conversation(thread), _turn(existing), False
                    if thread.stop_operation_id is not None:
                        raise ProductStoreError("run_closed")
                    if thread.active_turn_id not in (None, existing.id):
                        raise ProductStoreError("thread_busy")
                    existing.request_state = (
                        "prepared" if existing.native_request is not None else "claimed"
                    )
                    existing.updated_at = now
                    existing.version += 1
                    thread.active_turn_id = existing.id
                    thread.version += 1
                    thread.updated_at = now
                    session.flush()
                    return _conversation(thread), _turn(existing), True
                if thread.active_turn_id is not None:
                    raise ProductStoreError("thread_busy")
                if thread.stop_operation_id is not None:
                    raise ProductStoreError("run_closed")
                if thread.run_id is None and thread.start_operation_id is not None:
                    raise ProductStoreError("original_operation_unavailable")
                turn = ChatTurnRow(
                    id=uuid4(),
                    conversation_id=thread.id,
                    workspace_id=thread.workspace_id,
                    principal_id=thread.principal_id,
                    operation_id=operation_id,
                    native_operation_id=native_operation_id,
                    request_fingerprint=fingerprint,
                    input_text=input_text,
                    kind="start" if thread.run_id is None else "input",
                    request_state="claimed",
                    created_at=now,
                    updated_at=now,
                    version=1,
                    dispatch_attempt=0,
                )
                session.add(turn)
                thread.active_turn_id = turn.id
                if turn.kind == "start":
                    thread.start_operation_id = native_operation_id
                thread.version += 1
                thread.updated_at = now
                session.flush()
                return _conversation(thread), _turn(turn), True
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def prepare_turn(
        self,
        conversation: Conversation,
        turn_id: UUID,
        *,
        grant_id: UUID,
        workspace: ExecutionWorkspace,
        request: dict[str, JsonValue],
        now: datetime,
    ) -> ChatTurn:
        try:
            with Session(self.engine) as session, session.begin():
                thread, turn = _lock_rows(session, conversation, turn_id)
                if thread.stop_operation_id is not None:
                    raise ProductStoreError("run_closed")
                if (
                    thread.grant_id not in (None, grant_id)
                    or thread.execution_workspace
                    not in (None, workspace.model_dump(mode="json"))
                    or turn.native_request not in (None, request)
                ):
                    raise ProductStoreError("operation_conflict")
                thread.grant_id = grant_id
                thread.execution_workspace = workspace.model_dump(mode="json")
                turn.native_request = request
                turn.request_state = "prepared"
                turn.updated_at = now
                turn.version += 1
                session.flush()
                return _turn(turn)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def mark_dispatching(
        self, conversation: Conversation, expected: ChatTurn, *, now: datetime
    ) -> ChatTurn | None:
        try:
            with Session(self.engine) as session, session.begin():
                thread, turn = _lock_rows(session, conversation, expected.id)
                if thread.stop_operation_id is not None:
                    raise ProductStoreError("run_closed")
                if turn.request_state != "prepared" or turn.version != expected.version:
                    return None
                turn.request_state, turn.updated_at = "dispatching", now
                turn.version += 1
                turn.dispatch_attempt += 1
                return _turn(turn)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def settle_turn(
        self,
        conversation: Conversation,
        turn_id: UUID,
        *,
        state: RequestState,
        reason_code: str | None,
        run_id: UUID | None = None,
        now: datetime,
        expected: ChatTurn,
        observation: bool = False,
    ) -> ChatTurn:
        try:
            with Session(self.engine) as session, session.begin():
                thread, turn = _lock_rows(session, conversation, turn_id)
                if (
                    turn.native_operation_id != expected.native_operation_id
                    or turn.request_fingerprint != expected.request_fingerprint
                    or turn.dispatch_attempt != expected.dispatch_attempt
                    or (observation and turn.version != expected.version)
                ):
                    return _turn(turn)
                if turn.request_state == "delivered" or (
                    turn.request_state == "accepted"
                    and state not in ("accepted", "delivered")
                ):
                    return _turn(turn)
                if observation and state == "busy" and turn.dispatch_attempt > 1:
                    state, reason_code = "unknown", "original_admission_unknown"
                if run_id is not None:
                    if thread.run_id not in (None, run_id):
                        raise ProductStoreError("original_run_mismatch")
                    thread.run_id = run_id
                turn.request_state, turn.reason_code, turn.updated_at = (
                    state,
                    reason_code,
                    now,
                )
                turn.version += 1
                if state in ("busy", "rejected") and thread.active_turn_id == turn.id:
                    thread.active_turn_id = None
                thread.updated_at = now
                thread.version += 1
                session.flush()
                return _turn(turn)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def prepare_stop(
        self, conversation: Conversation, *, now: datetime
    ) -> Conversation:
        try:
            with Session(self.engine) as session, session.begin():
                thread = session.execute(
                    _owned(
                        conversation.id,
                        conversation.workspace_id,
                        conversation.principal_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if thread is None:
                    raise ProductStoreError("thread_not_found")
                if thread.stop_operation_id is None:
                    thread.stop_operation_id, thread.stop_state = uuid4(), "prepared"
                    thread.version += 1
                    thread.updated_at = now
                return _conversation(thread)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def cache_turn(
        self,
        conversation: Conversation,
        expected: ChatTurn,
        view: TurnView,
        *,
        now: datetime,
    ) -> ChatTurn:
        try:
            with Session(self.engine) as session, session.begin():
                thread, turn = _lock_rows(session, conversation, expected.id)
                if (
                    turn.response_id is not None
                    and view.response_id != turn.response_id
                ):
                    raise ProductStoreError("original_response_mismatch")
                if turn.response_id is None:
                    turn.response_id = view.response_id
                previous = (
                    None
                    if turn.cached_view is None
                    else TurnView.model_validate(turn.cached_view)
                )
                terminal = {"completed", "interrupted", "failed", "cancelled"}
                if (
                    previous is None
                    or previous.status not in terminal
                    or view.status in terminal
                ):
                    turn.cached_view = TypeAdapter(
                        dict[str, JsonValue]
                    ).validate_python(view.model_dump(mode="json", by_alias=True))
                    turn.version += 1
                    turn.updated_at = now
                if view.status in terminal and thread.active_turn_id == turn.id:
                    thread.active_turn_id = None
                    thread.version += 1
                    thread.updated_at = now
                session.flush()
                return _turn(turn)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def bind_original_run(
        self, conversation: Conversation, run_id: UUID, *, now: datetime
    ) -> Conversation:
        try:
            with Session(self.engine) as session, session.begin():
                thread = session.execute(
                    _owned(
                        conversation.id,
                        conversation.workspace_id,
                        conversation.principal_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if thread is None:
                    raise ProductStoreError("thread_not_found")
                if thread.run_id not in (None, run_id):
                    raise ProductStoreError("original_run_mismatch")
                thread.run_id = run_id
                thread.version += 1
                thread.updated_at = now
                return _conversation(thread)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def mark_stop_dispatching(
        self, conversation: Conversation, *, now: datetime
    ) -> bool:
        try:
            with Session(self.engine) as session, session.begin():
                thread = session.execute(
                    _owned(
                        conversation.id,
                        conversation.workspace_id,
                        conversation.principal_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if (
                    thread is None
                    or thread.stop_operation_id != conversation.stop_operation_id
                ):
                    raise ProductStoreError("original_operation_mismatch")
                if thread.stop_state != "prepared":
                    return False
                thread.stop_state = "dispatching"
                thread.version += 1
                thread.updated_at = now
                return True
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None

    def settle_stop(
        self, conversation: Conversation, state: StopState, *, now: datetime
    ) -> Conversation:
        try:
            with Session(self.engine) as session, session.begin():
                thread = session.execute(
                    _owned(
                        conversation.id,
                        conversation.workspace_id,
                        conversation.principal_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if (
                    thread is None
                    or thread.stop_operation_id != conversation.stop_operation_id
                    or thread.run_id != conversation.run_id
                ):
                    raise ProductStoreError("original_operation_mismatch")
                if thread.stop_state != "stopped":
                    thread.stop_state = state
                    thread.version += 1
                    thread.updated_at = now
                return _conversation(thread)
        except (SQLAlchemyError, ValidationError):
            raise ProductStoreError("product_store_unavailable") from None
