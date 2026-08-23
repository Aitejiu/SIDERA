from uuid import UUID

from sqlalchemy import select

from sidera_control_plane.application.command_queries import CommandTerminalView
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import CommandModel


class SqlAlchemyCommandQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_terminal(
        self, workspace_id: UUID, command_id: UUID
    ) -> CommandTerminalView | None:
        with self._database.session() as session:
            row = session.scalar(
                select(CommandModel).where(
                    CommandModel.workspace_id == workspace_id,
                    CommandModel.id == command_id,
                )
            )
        if row is None:
            return None
        if row.status == "applied":
            status = "applied"
        elif row.status == "rejected":
            status = "rejected"
        else:
            raise RuntimeError("stored command status is invalid")
        return CommandTerminalView(
            command_id=row.id,
            status=status,
            result=dict(row.result or {}),
            error_code=row.rejection_reason if status == "rejected" else None,
        )
