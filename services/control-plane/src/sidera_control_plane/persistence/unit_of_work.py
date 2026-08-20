from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import (
    ArtifactRepositoryPort,
    CommandRepositoryPort,
    EventStorePort,
    EvidenceClaimRepositoryPort,
    ExecutionBindingRepositoryPort,
    ExecutionEventRepositoryPort,
    ExecutionInputRepositoryPort,
    ExecutionRepositoryPort,
    GatewayOperationRepositoryPort,
    LeaseStorePort,
    ModuleRepositoryPort,
    SessionRepositoryPort,
    TaskRepositoryPort,
    WorkerRepositoryPort,
    WorkspaceRepositoryPort,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.execution_repository import ExecutionRepository
from sidera_control_plane.persistence.gateway_repository import (
    ArtifactRepository,
    EvidenceClaimRepository,
    ExecutionEventRepository,
    ExecutionInputRepository,
    GatewayOperationRepository,
)
from sidera_control_plane.persistence.lease_store import LeaseStore
from sidera_control_plane.persistence.module_repository import (
    ExecutionBindingRepository,
    ModuleRepository,
)
from sidera_control_plane.persistence.repositories import (
    CommandRepository,
    EventRepository,
    TaskRepository,
)
from sidera_control_plane.persistence.session_repository import SessionRepository
from sidera_control_plane.persistence.worker_repository import WorkerRepository
from sidera_control_plane.persistence.workspace_repository import WorkspaceRepository


class SqlAlchemyUnitOfWork:
    session: Session
    tasks: TaskRepositoryPort
    workers: WorkerRepositoryPort
    executions: ExecutionRepositoryPort
    leases: LeaseStorePort
    sessions: SessionRepositoryPort
    modules: ModuleRepositoryPort
    bindings: ExecutionBindingRepositoryPort
    gateway_operations: GatewayOperationRepositoryPort
    workspaces: WorkspaceRepositoryPort
    execution_events: ExecutionEventRepositoryPort
    artifacts: ArtifactRepositoryPort
    evidence_claims: EvidenceClaimRepositoryPort
    execution_inputs: ExecutionInputRepositoryPort
    commands: CommandRepositoryPort
    events: EventStorePort

    def __init__(self, database: Database) -> None:
        self._database = database

    def __enter__(self) -> Self:
        self.session = Session(self._database.engine, expire_on_commit=False)
        self.tasks = TaskRepository(self.session)
        self.workers = WorkerRepository(self.session)
        self.executions = ExecutionRepository(self.session)
        self.leases = LeaseStore(self.session)
        self.sessions = SessionRepository(self.session)
        self.modules = ModuleRepository(self.session)
        self.bindings = ExecutionBindingRepository(self.session)
        self.gateway_operations = GatewayOperationRepository(self.session)
        self.workspaces = WorkspaceRepository(self.session)
        self.execution_events = ExecutionEventRepository(self.session)
        self.artifacts = ArtifactRepository(self.session)
        self.evidence_claims = EvidenceClaimRepository(self.session)
        self.execution_inputs = ExecutionInputRepository(self.session)
        self.commands = CommandRepository(self.session)
        self.events = EventRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        except BaseException:
            self.session.rollback()
            raise
        finally:
            self.session.close()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        with self.session.begin_nested():
            yield
