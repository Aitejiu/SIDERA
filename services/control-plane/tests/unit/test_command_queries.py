from uuid import UUID

from sidera_control_plane.application.command_queries import (
    CommandQueryService,
    CommandTerminalView,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeCommandQueryStore:
    def __init__(self, terminal: CommandTerminalView | None) -> None:
        self.terminal = terminal

    def get_terminal(self, workspace_id: UUID, command_id: UUID) -> CommandTerminalView | None:
        assert workspace_id == WORKSPACE_ID
        assert command_id == COMMAND_ID
        return self.terminal


def test_missing_command_is_pending_and_keeps_command_id() -> None:
    service = CommandQueryService(FakeCommandQueryStore(None))

    view = service.get_status(WORKSPACE_ID, COMMAND_ID)

    assert view.command_id == COMMAND_ID
    assert view.status == "pending"
    assert view.result is None
    assert view.error_code is None


def test_applied_command_returns_safe_result() -> None:
    terminal = CommandTerminalView(
        command_id=COMMAND_ID,
        status="applied",
        result={
            "command_id": str(COMMAND_ID),
            "aggregate_id": str(WORKSPACE_ID),
            "aggregate_version": 2,
            "event_ids": [],
            "data": {"state": "ready"},
        },
        error_code=None,
    )
    service = CommandQueryService(FakeCommandQueryStore(terminal))

    view = service.get_status(WORKSPACE_ID, COMMAND_ID)

    assert view.status == "applied"
    assert view.result == terminal.result
    assert view.error_code is None


def test_rejected_command_returns_only_stable_error_code() -> None:
    terminal = CommandTerminalView(
        command_id=COMMAND_ID,
        status="rejected",
        result={"code": "validation_error", "message": "internal details"},
        error_code="validation_error",
    )
    service = CommandQueryService(FakeCommandQueryStore(terminal))

    view = service.get_status(WORKSPACE_ID, COMMAND_ID)

    assert view.status == "rejected"
    assert view.result is None
    assert view.error_code == "validation_error"
