from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import (
    ArtifactRepositoryPort,
    AttentionAcknowledgementRepositoryPort,
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
    PlanRepositoryPort,
    ProjectionChangePublisherPort,
    SessionRepositoryPort,
    TaskRepositoryPort,
    WorkerRepositoryPort,
    WorkspaceRepositoryPort,
)
from sidera_control_plane.application.projection_changes import (
    ProjectionChangePublisher,
)
from sidera_control_plane.persistence.attention_repository import (
    AttentionAcknowledgementRepository,
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
from sidera_control_plane.persistence.plan_repository import PlanRepository
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
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
    plans: PlanRepositoryPort
    attention: AttentionAcknowledgementRepositoryPort
    projection_changes: ProjectionChangePublisherPort
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

    def __init__(
        self, database: Database, *, projection_change_retention_seconds: int = 604_800
    ) -> None:
        self._database = database
        self._projection_change_retention_seconds = projection_change_retention_seconds

    def __enter__(self) -> Self:
        self.session = Session(self._database.engine, expire_on_commit=False)
        self.tasks = TaskRepository(self.session)
        self.plans = PlanRepository(self.session)
        self.attention = AttentionAcknowledgementRepository(self.session)
        self.projection_changes = ProjectionChangePublisher(
            ProjectionChangeRepository(
                self.session,
                retention_seconds=self._projection_change_retention_seconds,
            )
        )
        self.workers = WorkerRepository(self.session)
        self.executions = ExecutionRepository(self.session)
        self.leases = LeaseStore(self.session)
        self.sessions = SessionRepository(self.session)
        self.modules = ModuleRepository(self.session)
        self.bindings = ExecutionBindingRepository(self.session)
        self.gateway_operations = GatewayOperationRepository(self.session)
        self.workspaces = WorkspaceRepository(self.session)
        self.execution_events = ExecutionEventRepository(
            self.session, self.projection_changes
        )
        self.artifacts = ArtifactRepository(self.session, self.projection_changes)
        self.evidence_claims = EvidenceClaimRepository(
            self.session, self.projection_changes
        )
        self.execution_inputs = ExecutionInputRepository(self.session)
        self.commands = CommandRepository(self.session)
        self.events = EventRepository(self.session, self.projection_changes)
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
