from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request

from sidera_control_plane.application.command_service import CommandService
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.task_commands import TaskCommandHandler
from sidera_control_plane.application.task_queries import TaskQueryService
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.task_query_store import SqlAlchemyTaskQueryStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_command_service(
    database: Annotated[Database, Depends(get_database)],
) -> CommandService:
    factory: Callable[[], ControlPlaneUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database
    )
    return CommandService(factory, (TaskCommandHandler(),))


def get_task_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> TaskQueryService:
    return TaskQueryService(SqlAlchemyTaskQueryStore(database))
