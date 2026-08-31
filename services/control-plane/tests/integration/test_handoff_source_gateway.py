from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    WorkerGateway,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionSessionModel,
    ExecutionWorkspaceModel,
    GatewayOperationModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_missing_handoff_transition_creates_no_runtime_side_effects(
    database: Database,
    workspace_id: UUID,
) -> None:
    gateway = WorkerGateway(
        lambda: SqlAlchemyUnitOfWork(database),
        adapter_factories={},
        workspace_factories={},
        clock=lambda: NOW,
    )

    with pytest.raises(GatewayMessageError) as caught:
        gateway.capture_handoff_source(
            workspace_id=workspace_id,
            transition_id=uuid4(),
            expected_transition_version=1,
        )

    assert caught.value.code == "handoff_transition_not_found"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionModel)) == 0
        assert (
            session.scalar(select(func.count()).select_from(ExecutionSessionModel))
            == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(ExecutionWorkspaceModel))
            == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(GatewayOperationModel))
            == 0
        )
