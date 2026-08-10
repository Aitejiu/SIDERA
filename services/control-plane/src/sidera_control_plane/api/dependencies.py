from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request

from sidera_control_plane.application.command_service import TaskCommandService
from sidera_control_plane.application.task_queries import TaskQueryService
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.task_query_store import SqlAlchemyTaskQueryStore
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_command_service(
    database: Annotated[Database, Depends(get_database)],
) -> TaskCommandService:
    factory: Callable[[], SqlAlchemyUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database
    )
    return TaskCommandService(factory)


def get_task_query_service(
    database: Annotated[Database, Depends(get_database)],
) -> TaskQueryService:
    return TaskQueryService(SqlAlchemyTaskQueryStore(database))
