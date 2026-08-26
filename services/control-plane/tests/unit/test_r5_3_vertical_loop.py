from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidera_control_plane.gateway_worker import (
    _deterministic_artifact_storage_ref,
)

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "r5-3-vertical-loop.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("r5_3_vertical_loop", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("vertical loop runner is not importable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_help_exposes_bounded_gate_cli() -> None:
    module = _load_runner_module()

    parser = module.build_parser()
    help_text = parser.format_help()

    assert "--mode" in help_text
    assert "--run-id" in help_text
    assert "--deadline-seconds" in help_text
    assert "--cleanup-test-run" in help_text


def test_runner_exposes_fault_recovery_mode_and_fixed_test_scope() -> None:
    module = _load_runner_module()

    parser = module.build_parser()
    args = parser.parse_args(["--mode", "fault"])

    assert args.mode == "fault"
    assert module.fault_recovery_test_paths()
    assert all(
        path.parent.name == "integration" and path.parent.parent.name == "tests"
        for path in module.fault_recovery_test_paths()
    )


def test_runner_accepts_the_shared_r5_3_env_example(tmp_path: Path) -> None:
    module = _load_runner_module()
    example = SCRIPT.parents[1] / "config" / "examples" / "r5-3.local.env.example"
    runnable_example = tmp_path / "r5-3.local.env"
    runnable_example.write_text(
        example.read_text(encoding="utf-8").replace("<set-locally>", "local-capability"),
        encoding="utf-8",
    )

    values = module.parse_env_file(runnable_example)

    assert "SIDERA_AKASHIC_CONFIG" in values
    assert "SIDERA_OPENCODE_MODEL" in values


def test_runner_accepts_stack_lease_overrides(tmp_path: Path) -> None:
    module = _load_runner_module()
    env_file = tmp_path / "r5-3.local.env"
    env_file.write_text(
        "SIDERA_ASSIGNMENT_LEASE_SECONDS=300\n"
        "SIDERA_ASSIGNMENT_LEASE_RENEW_INTERVAL_SECONDS=30.0\n",
        encoding="utf-8",
    )

    values = module.parse_env_file(env_file)

    assert values == {
        "SIDERA_ASSIGNMENT_LEASE_SECONDS": "300",
        "SIDERA_ASSIGNMENT_LEASE_RENEW_INTERVAL_SECONDS": "30.0",
    }


def test_runner_ids_are_stable_and_run_scoped() -> None:
    module = _load_runner_module()
    run_id = module.UUID("11111111-1111-4111-8111-111111111111")

    first = module.RunIdentity(run_id)
    second = module.RunIdentity(run_id)

    assert first.entity("candidate") == second.entity("candidate")
    assert first.entity("candidate") != first.entity("planning-request")
    assert first.idempotency("submit-candidate") == second.idempotency(
        "submit-candidate"
    )
    assert first.entity("candidate").version == 5


def test_runner_error_summary_never_echoes_response_body() -> None:
    module = _load_runner_module()
    body = "secret knowledge body marker and sk-do-not-print"

    error = module.GateFailure.from_http(
        stage="submit-candidate", status_code=503, body=body
    )
    summary = error.as_dict()

    assert summary == {
        "stage": "submit-candidate",
        "error_code": "http_503",
    }
    assert body not in str(summary)


def test_runner_deadline_rejects_expired_poll() -> None:
    module = _load_runner_module()

    deadline = module.Deadline(seconds=0)

    with pytest.raises(module.GateFailure, match="deadline_exceeded"):
        deadline.require("planning-proposal")


def test_runner_selects_a_deterministic_materialization_mapping_entry() -> None:
    module = _load_runner_module()
    first = module.UUID("11111111-1111-4111-8111-111111111111")
    second = module.UUID("22222222-2222-4222-8222-222222222222")

    selected = module.VerticalLoopRunner._first_mapping_uuid(
        {"worker-b": str(second), "worker-a": str(first)},
        "worker_item_mapping",
    )

    assert selected == first
    with pytest.raises(ValueError, match="must not be empty"):
        module.VerticalLoopRunner._first_mapping_uuid({}, "worker_item_mapping")


def test_json_http_client_can_accept_an_array_response_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()

    class FakeResponse:
        status_code = 200
        text = "[]"

        def json(self):
            return [{"id": "execution"}]

    class FakeHttpClient:
        def request(self, *args, **kwargs):
            del args, kwargs
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: FakeHttpClient())
    client = module.JsonHttpClient("http://127.0.0.1:8100")

    _, document = client.request(
        "GET",
        "/api/v1/executions",
        stage="execution_list",
        allow_json_array=True,
    )

    assert document == [{"id": "execution"}]
    client.close()


def test_runner_uses_the_consumer_specific_context_package_route() -> None:
    module = _load_runner_module()
    workspace_id = module.UUID("20000000-0000-0000-0000-000000000001")
    consumer_id = module.UUID("33333333-3333-4333-8333-333333333333")

    assert module.VerticalLoopRunner._package_path(
        workspace_id,
        consumer_id,
        module.ContextConsumerType.PLANNER,
    ) == (
        f"/api/v1/workspaces/{workspace_id}/planning-runs/{consumer_id}/context-package"
    )
    assert module.VerticalLoopRunner._package_path(
        workspace_id,
        consumer_id,
        module.ContextConsumerType.WORKER,
    ) == (
        f"/api/v1/workspaces/{workspace_id}/executions/{consumer_id}/context-package"
    )


def test_runner_scopes_gate_repository_by_run_id(tmp_path: Path) -> None:
    module = _load_runner_module()
    run_id = module.UUID("44444444-4444-4444-8444-444444444444")
    candidate_spool = tmp_path / "candidate-spool"

    assert module.VerticalLoopRunner._gate_repository_path(
        candidate_spool, run_id
    ) == tmp_path / "gate-repositories" / str(run_id)


@pytest.mark.parametrize(
    ("overview", "expected"),
    [
        (
            {
                "subtask_count": 2,
                "planned_subtask_count": 0,
                "running_subtask_count": 0,
                "completed_subtask_count": 2,
                "failed_subtask_count": 0,
            },
            True,
        ),
        (
            {
                "subtask_count": 2,
                "planned_subtask_count": 0,
                "running_subtask_count": 0,
                "completed_subtask_count": 1,
                "failed_subtask_count": 0,
            },
            False,
        ),
        (
            {
                "subtask_count": 2,
                "planned_subtask_count": 0,
                "running_subtask_count": 0,
                "completed_subtask_count": 2,
                "failed_subtask_count": 1,
            },
            False,
        ),
    ],
)
def test_runner_recognizes_task_first_completion_projection(
    overview: dict[str, int], expected: bool
) -> None:
    module = _load_runner_module()

    assert module.VerticalLoopRunner._task_overview_is_complete(overview) is expected


def test_runner_builds_worker_candidate_with_task_provenance() -> None:
    module = _load_runner_module()
    source_id = module.UUID("77777777-7777-4777-8777-777777777777")

    source = module.VerticalLoopRunner._worker_candidate_source(source_id)

    assert source.source_type == "task"
    assert source.source_id == source_id
    assert source.metadata == {}


def test_real_evidence_report_contains_only_redacted_execution_facts() -> None:
    module = _load_runner_module()
    report = module.build_real_evidence_report(
        run_marker="SIDERA-R5-3-5-REAL-20260827-R23T",
        workspace_id=module.UUID("20000000-0000-0000-0000-000000000001"),
        task_id=module.UUID("82cd1382-ad9a-4925-b95f-43160b360468"),
        execution_id=module.UUID("c9ab4cd6-baf5-4d29-8477-e4827d3b0bf8"),
        session_id=module.UUID("f4c07678-3d36-4853-9382-ed882860ec86"),
        worker_item_id=module.UUID("aa2407ae-a657-44be-b621-33cceca88ca2"),
        worker_id=module.UUID("0156fd33-d298-5f3c-b9f8-f33841d31853"),
        lease_id=module.UUID("ec855030-8416-4a72-adb6-c07932c84e32"),
        fencing_token=1,
        execution_status="succeeded",
        session_status="closed",
        trace_event_counts={
            "input.accepted": 1,
            "progress.updated": 295,
            "session.heartbeat": 1,
        },
        trace_last_sequence=297,
        workspace_ref="r5.3.5-real-git-worktree",
        repository_revision="a" * 40,
        provider="opencode",
        model="opencode-go/MiMo-V2.5",
        interface_version="sidera.worker-adapter.v1",
        config_version=1,
        planner_provider="opencode-acp-planner",
        planner_model="opencode-go/MiMo-V2.5",
        planner_interface_version="sidera.planner.v1",
        planner_config_version=1,
        workspace_provider="git-worktree",
        workspace_interface_version="sidera.workspace-provider.v1",
        workspace_config_version=1,
        opencode_version="1.18.19",
        planning_profile_id=module.UUID("b4000000-0000-0000-0000-000000000001"),
        planning_profile_version=1,
    )

    document = module.json.loads(report)

    assert document["schema_version"] == 1
    assert document["scope"]["execution_id"] == (
        "c9ab4cd6-baf5-4d29-8477-e4827d3b0bf8"
    )
    assert document["runtime"]["worker"] == {
        "config_version": 1,
        "interface_version": "sidera.worker-adapter.v1",
        "model": "opencode-go/MiMo-V2.5",
        "provider": "opencode",
    }
    assert document["runtime"]["planner"]["provider"] == "opencode-acp-planner"
    assert document["trace"] == {
        "event_count": 297,
        "event_type_counts": {
            "input.accepted": 1,
            "progress.updated": 295,
            "session.heartbeat": 1,
        },
        "last_sequence": 297,
    }
    assert document["workspace"]["repository_clean"] is True
    assert "payload" not in document
    assert "sk-" not in report.decode("utf-8")


def test_real_trace_summary_accepts_string_pagination_cursors() -> None:
    module = _load_runner_module()
    responses = iter(
        (
            {"items": [{"sequence": 1, "event_type": "input.accepted"}], "next_cursor": "1"},
            {"items": [{"sequence": 2, "event_type": "session.closed"}], "next_cursor": None},
        )
    )
    runner = object.__new__(module.VerticalLoopRunner)
    runner.config = SimpleNamespace(
        workspace_id=module.UUID("20000000-0000-0000-0000-000000000001")
    )
    runner._cp_get = lambda path, *, params, stage: next(responses)

    summary = runner._real_trace_summary(
        module.UUID("c9ab4cd6-baf5-4d29-8477-e4827d3b0bf8"),
        module.UUID("f4c07678-3d36-4853-9382-ed882860ec86"),
    )

    assert summary == {
        "event_count": 2,
        "event_type_counts": {
            "input.accepted": 1,
            "session.closed": 1,
        },
        "last_sequence": 2,
    }


def test_runner_replan_reference_replaces_section_identity_with_revision() -> None:
    module = _load_runner_module()
    knowledge = {
        "section_key": "overview",
        "section_hash": "a" * 64,
        "source_ids": ["33333333-3333-4333-8333-333333333333"],
        "initial_revision_id": "11111111-1111-4111-8111-111111111111",
        "initial_revision_hash": "b" * 64,
    }
    worker_candidate = {
        "revision_id": "22222222-2222-4222-8222-222222222222",
        "revision_hash": "c" * 64,
        "source_ids": ["44444444-4444-4444-8444-444444444444"],
    }

    revised = module.VerticalLoopRunner._knowledge_ref_after_revision(
        knowledge,
        worker_candidate,
        section_key="worker-result",
        section_hash="d" * 64,
        source_ids=worker_candidate["source_ids"],
    )

    assert revised["initial_revision_id"] == worker_candidate["revision_id"]
    assert revised["initial_revision_hash"] == worker_candidate["revision_hash"]
    assert revised["section_key"] == "worker-result"
    assert revised["section_hash"] == "d" * 64
    assert revised["source_ids"] == worker_candidate["source_ids"]


def test_deterministic_artifact_storage_ref_is_run_scoped_and_stable() -> None:
    first = _deterministic_artifact_storage_ref(
        "r5-3-deterministic-worker-aaaaaaaaaaaa"
    )
    second = _deterministic_artifact_storage_ref(
        "r5-3-deterministic-worker-bbbbbbbbbbbb"
    )

    assert first == _deterministic_artifact_storage_ref(
        "r5-3-deterministic-worker-aaaaaaaaaaaa"
    )
    assert first != second


def test_runner_secret_scan_scopes_outbox_through_event_workspace() -> None:
    module = _load_runner_module()
    statements: list[str] = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            del exc_type, exc_value, traceback
            return False

        def exec_driver_sql(self, statement, parameters):
            del parameters
            statements.append(statement)
            return SimpleNamespace(all=lambda: ())

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    runner = object.__new__(module.VerticalLoopRunner)
    runner.identity = module.RunIdentity(
        module.UUID("55555555-5555-4555-8555-555555555555")
    )
    runner.config = SimpleNamespace(
        workspace_id=module.UUID("66666666-6666-4666-8666-666666666666")
    )
    runner.database = SimpleNamespace(engine=FakeEngine())

    runner._assert_no_marker_in_control_plane()

    assert len(statements) == 1
    assert "JOIN events" in statements[0]
    assert "outbox_messages WHERE workspace_id" not in statements[0]


def test_runner_reports_safe_status_when_poll_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()

    class ExpiredAfterFetch:
        remaining = 0

        def __init__(self, *, seconds: float):
            del seconds

        def require(self, stage: str):
            del stage

        def sleep(self, seconds: float):
            del seconds

    runner = object.__new__(module.VerticalLoopRunner)
    runner.config = SimpleNamespace(
        deadline_seconds=1,
        poll_interval_seconds=0.1,
    )
    monkeypatch.setattr(module, "Deadline", ExpiredAfterFetch)

    with pytest.raises(module.GateFailure, match="deadline_exceeded_queued"):
        runner._wait_for(
            "planning-proposal",
            lambda: {"status": "queued"},
            lambda value: False,
        )
