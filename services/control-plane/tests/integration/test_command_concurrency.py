import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from tests.integration.test_planning_command_api import _planner_capabilities, _planner_profile

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import (
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    CommandModel,
    EventModel,
    PlanningRequestModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _seed_planner_profile(database: Database, workspace_id: UUID) -> UUID:
    definition_id = uuid4()
    instance_id = uuid4()
    profile = _planner_profile(workspace_id, instance_id)
    with SqlAlchemyUnitOfWork(database) as uow:
        uow.modules.add_definition(
            ModuleDefinition(
                id=definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities=_planner_capabilities(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.modules.add_instance(
            ModuleInstance(
                id=instance_id,
                workspace_id=workspace_id,
                definition_id=definition_id,
                instance_name="concurrency-planner",
                config_version=1,
                config_namespace="planning",
                config={"planning.mode": "test"},
                secret_refs=(),
                lifecycle=ModuleLifecycle.ACTIVE,
            )
        )
        uow.planning_profiles.add(profile)
    return profile.id


def _submit_request(
    workspace_id: UUID,
    profile_id: UUID,
    command_id: UUID,
    correlation_id: UUID,
    idempotency_key: str,
    *,
    objective: str,
) -> dict[str, Any]:
    return {
        "command_id": str(command_id),
        "workspace_id": str(workspace_id),
        "command_type": "SubmitPlanningRequest",
        "target_type": "workspace",
        "actor": {"type": "local", "id": "operator"},
        "target_id": None,
        "expected_version": None,
        "idempotency_key": idempotency_key,
        "correlation_id": str(correlation_id),
        "payload": {
            "profile_id": str(profile_id),
            "profile_version": 1,
            "objective": objective,
            "constraints": ["Keep the plan reviewable"],
            "acceptance_criteria": ["The request is queued once"],
            "context_refs": ["context:concurrency"],
            "requested_outputs": ["proposal"],
            "source_type": "web",
            "source_ref": None,
        },
    }


def _run_pair(
    settings: Settings,
    first_request: dict[str, Any],
    second_request: dict[str, Any],
) -> list[Any]:
    barrier = threading.Barrier(2)
    with TestClient(create_app(settings)) as first_client, TestClient(
        create_app(settings)
    ) as second_client:
        def post(client: TestClient, request: dict[str, Any]) -> Any:
            barrier.wait(timeout=5)
            return client.post("/api/v1/commands", json=request)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(post, first_client, first_request),
                executor.submit(post, second_client, second_request),
            ]
            return [future.result(timeout=15) for future in futures]


def _command_counts(database: Database, workspace_id: UUID, command_id: UUID) -> dict[str, int]:
    with database.session() as session:
        return {
            "commands": session.scalar(
                select(func.count()).select_from(CommandModel)
            )
            or 0,
            "planning_requests": session.scalar(
                select(func.count())
                .select_from(PlanningRequestModel)
                .where(PlanningRequestModel.workspace_id == workspace_id)
            )
            or 0,
            "events_for_command": session.scalar(
                select(func.count())
                .select_from(EventModel)
                .where(EventModel.causation_id == command_id)
            )
            or 0,
        }


def test_concurrent_exact_submit_replays_one_terminal_result(
    database_url: str,
    database: Database,
    workspace_id: UUID,
) -> None:
    profile_id = _seed_planner_profile(database, workspace_id)
    command_id = uuid4()
    correlation_id = uuid4()
    settings = Settings(database_url=database_url, local_actor_id="operator")
    request = _submit_request(
        workspace_id,
        profile_id,
        command_id,
        correlation_id,
        "concurrent-submit-exact",
        objective="Implement exact concurrent submit",
    )

    responses = _run_pair(settings, request, request)

    assert sorted(response.status_code for response in responses) == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["command_id"] == str(command_id)
    assert _command_counts(database, workspace_id, command_id) == {
        "commands": 1,
        "planning_requests": 1,
        "events_for_command": 2,
    }


def test_concurrent_changed_submit_conflicts_without_duplicate_request(
    database_url: str,
    database: Database,
    workspace_id: UUID,
) -> None:
    profile_id = _seed_planner_profile(database, workspace_id)
    command_id = uuid4()
    correlation_id = uuid4()
    settings = Settings(database_url=database_url, local_actor_id="operator")
    first = _submit_request(
        workspace_id,
        profile_id,
        command_id,
        correlation_id,
        "concurrent-submit-changed",
        objective="Implement the first concurrent submit",
    )
    second = _submit_request(
        workspace_id,
        profile_id,
        command_id,
        correlation_id,
        "concurrent-submit-changed",
        objective="Implement a changed concurrent submit",
    )

    responses = _run_pair(settings, first, second)

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert _command_counts(database, workspace_id, command_id) == {
        "commands": 1,
        "planning_requests": 1,
        "events_for_command": 2,
    }
