from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import update

from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ModuleDefinitionModel,
)
from sidera_control_plane.runtime_readiness import main
from tests.integration.test_scheduler_runtime_binding import _seed

pytestmark = pytest.mark.integration


def test_readiness_reports_actionable_runtime_gaps_without_sensitive_facts(
    database,
    database_url: str,
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _seed(database, workspace_id, configured_worker_indexes=(0,))
    with database.session() as session:
        session.execute(
            update(ModuleDefinitionModel)
            .where(ModuleDefinitionModel.id == fixture.adapter_instance.definition_id)
            .values(lifecycle="disabled")
        )
        session.execute(
            update(ExecutionModel)
            .where(ExecutionModel.id == fixture.execution.id)
            .values(status="running", assigned_worker_id=fixture.workers[0].id)
        )
        session.commit()

    monkeypatch.setenv("SIDERA_DATABASE_URL", database_url)
    exit_code = main(["--workspace-id", str(workspace_id), "--json"])

    assert exit_code == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["ready"] is False
    assert payload["missing_runtime_configuration"] == 1
    assert payload["invalid_runtime_configuration"] == 1
    assert payload["executions_without_binding"] == [str(fixture.execution.id)]
    assert payload["module_mismatches"][0]["worker_id"] == str(fixture.workers[0].id)
    assert "/tmp/sidera-scheduler" not in output
    assert "scheduler-opencode" not in output
    assert "adapter.command" not in output
