from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sidera_control_plane import collaboration_readiness as readiness_module
from sidera_control_plane.application.collaboration_metrics import (
    CollaborationMetricSample,
)
from sidera_control_plane.application.planning_commands import PlanningCommandHandler
from sidera_control_plane.collaboration_readiness import (
    ReadinessCheck,
    _result_input_artifact_store_check,
    _result_input_reader_check,
    _result_input_runtime_check,
    _result_input_worker_adapter_check,
    build_collaboration_readiness,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.errors import ResultInputsDisabled
from sidera_control_plane.persistence.runtime_metrics_store import (
    _add_result_input_metrics,
)


def test_result_input_writes_are_opt_in() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://test@127.0.0.1:1/test",
    )

    assert settings.result_inputs_enabled is False


def test_readiness_separates_disabled_writes_from_reader_capabilities(monkeypatch) -> None:
    ready = lambda *_args, **_kwargs: ReadinessCheck(
        name="dependency", ready=True, code="ok", summary="ready"
    )
    monkeypatch.setattr(readiness_module, "_database_check", ready)
    monkeypatch.setattr(readiness_module, "_alembic_check", ready)
    monkeypatch.setattr(readiness_module, "_reference_runtime_check", ready)
    monkeypatch.setattr(readiness_module, "_capability_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_reader_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_runtime_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_worker_adapter_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_binding_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_artifact_store_check", ready)

    report = build_collaboration_readiness(
        Settings(
            database_url="postgresql+psycopg://test@127.0.0.1:1/test",
        ),
        object(),
    ).check()

    writes = next(
        check for check in report.checks if check.name == "result_input_writes"
    )
    capabilities = next(
        check for check in report.checks if check.name == "result_input_capabilities"
    )
    assert writes.ready is False
    assert writes.code == "result_inputs_disabled"
    assert capabilities.ready is True
    assert capabilities.code == "ok"
    assert report.result_input_writes_enabled is False
    assert report.result_input_capabilities_ready is True


def test_result_input_metric_contract_has_bounded_labels() -> None:
    assert CollaborationMetricSample(
        name="sidera_collaboration_result_input_blocked_total",
        labels=(("reason", "result_input_unavailable"),),
        value=1,
    )
    assert CollaborationMetricSample(
        name="sidera_collaboration_result_input_selection_total",
        labels=(("result", "stale"),),
        value=1,
    )
    assert CollaborationMetricSample(
        name="sidera_collaboration_result_input_delivery_total",
        labels=(("outcome", "delivered"),),
        value=1,
    )
    assert CollaborationMetricSample(
        name="sidera_collaboration_result_input_delivery_duration_seconds",
        labels=(("outcome", "blocked"),),
        value=0.5,
    )


def test_disabled_v5_confirmation_is_rejected_but_existing_materialization_replays() -> None:
    handler = PlanningCommandHandler(
        local_actor_type="local",
        local_actor_id="operator",
        result_inputs_enabled=False,
    )
    request = SimpleNamespace(id=uuid4(), workspace_id=uuid4())
    proposal = SimpleNamespace(id=uuid4(), proposal_schema_version=5)
    profile = SimpleNamespace(proposal_schema_version=5)

    empty_uow = SimpleNamespace(
        proposal_materializations=SimpleNamespace(get_by_request=lambda *_args: None)
    )
    with pytest.raises(ResultInputsDisabled) as error:
        handler._assert_result_input_confirmation_allowed(
            empty_uow,
            request=request,
            proposal=proposal,
            profile=profile,
        )
    assert error.value.code == "result_inputs_disabled"
    assert error.value.details == {
        "setting": "SIDERA_RESULT_INPUTS_ENABLED",
        "proposal_schema_version": 5,
    }

    replay_uow = SimpleNamespace(
        proposal_materializations=SimpleNamespace(
            get_by_request=lambda *_args: SimpleNamespace(
                proposal_revision_id=proposal.id
            )
        )
    )
    handler._assert_result_input_confirmation_allowed(
        replay_uow,
        request=request,
        proposal=proposal,
        profile=profile,
    )


def test_disabled_v2_confirmation_path_remains_available() -> None:
    handler = PlanningCommandHandler(
        local_actor_type="local",
        local_actor_id="operator",
        result_inputs_enabled=False,
    )
    handler._assert_result_input_confirmation_allowed(
        SimpleNamespace(
            proposal_materializations=SimpleNamespace(
                get_by_request=lambda *_args: None
            )
        ),
        request=SimpleNamespace(id=uuid4(), workspace_id=uuid4()),
        proposal=SimpleNamespace(id=uuid4(), proposal_schema_version=2),
        profile=SimpleNamespace(proposal_schema_version=2),
    )


def test_result_input_metrics_aggregate_without_free_text_labels() -> None:
    counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
    created_at = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    occurred_at = created_at + timedelta(seconds=3)

    _add_result_input_metrics(
        counts,
        values,
        decisions=(SimpleNamespace(reason_code="input_selection_conflict"),),
        intents=(SimpleNamespace(reason_code="input_selection_hash_stale"),),
        selection_command_rows=("private selection explanation",),
        result_input_block_rows=("result_input_unavailable", "private reason"),
        context_delivery_rows=(
            (created_at, "succeeded", None, occurred_at),
            (created_at, "failed", "result_input_budget_exceeded", occurred_at),
        ),
    )

    assert counts[
        (
            "sidera_collaboration_result_input_selection_total",
            (("result", "conflict"),),
        )
    ] == 1
    assert counts[
        (
            "sidera_collaboration_result_input_selection_total",
            (("result", "stale"),),
        )
    ] == 1
    assert counts[
        (
            "sidera_collaboration_result_input_blocked_total",
            (("reason", "result_input_unavailable"),),
        )
    ] == 1
    assert counts[
        (
            "sidera_collaboration_result_input_blocked_total",
            (("reason", "unknown"),),
        )
    ] == 1
    assert counts[
        (
            "sidera_collaboration_result_input_delivery_total",
            (("outcome", "delivered"),),
        )
    ] == 1
    assert counts[
        (
            "sidera_collaboration_result_input_delivery_total",
            (("outcome", "blocked"),),
        )
    ] == 1
    assert values[
        (
            "sidera_collaboration_result_input_delivery_duration_seconds",
            (("outcome", "delivered"),),
        )
    ] == 3
    assert values[
        (
            "sidera_collaboration_result_input_delivery_duration_seconds",
            (("outcome", "blocked"),),
        )
    ] == 3


def test_readiness_classifies_missing_migration_and_artifact_store(monkeypatch, tmp_path) -> None:
    class MissingMigrationDirectory:
        @classmethod
        def from_config(cls, _config):
            return cls()

        def get_revision(self, _revision):
            return None

    monkeypatch.setattr(
        readiness_module, "ScriptDirectory", MissingMigrationDirectory
    )
    migration_check = _result_input_reader_check(object())
    assert migration_check.code == "result_input_migration_missing"

    missing_root = tmp_path / "artifact-store-does-not-exist"
    store_check = _result_input_artifact_store_check(
        Settings(
            database_url="postgresql+psycopg://test@127.0.0.1:1/test",
            artifact_root=missing_root,
        )
    )
    assert store_check.code == "result_input_artifact_store_unavailable"
    assert not missing_root.exists()


def test_readiness_classifies_runtime_capability_and_worker_config_mismatch(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        readiness_module,
        "_reference_capability",
        lambda: SimpleNamespace(supports_input_groups_v1=lambda: False),
    )
    assert _result_input_runtime_check().code == "result_input_runtime_capability_missing"

    class SessionContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return SimpleNamespace(
                all=lambda: ((uuid4(), 1, 2, "active", {}, "active"),)
            )

    worker_check = _result_input_worker_adapter_check(
        SimpleNamespace(session=lambda: SessionContext())
    )
    assert worker_check.code == "result_input_worker_adapter_config_mismatch"
