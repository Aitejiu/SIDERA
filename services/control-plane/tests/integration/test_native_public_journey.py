import json
import re
import subprocess
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sidera_agents.contracts import (
    EffectiveConfig,
    EventPage,
    ProfileDraft,
    RunEvent,
    RunSnapshot,
    RuntimeRef,
    RuntimeSnapshot,
    StartLookup,
    StartReceipt,
    StopReceipt,
)
from sidera_agents.errors import AgentError
from sqlalchemy import func, select

from sidera_control_plane.adapters.knowledge_http import KnowledgeHttpAdapter
from sidera_control_plane.application.planner_contract import _proposal_shape_hint
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from sidera_control_plane.application.worker_gateway import WorkerGateway
from sidera_control_plane.composition.native_runtime import create_control_plane_runtime
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.context_packages import ResultInputManifestV1
from sidera_control_plane.persistence.models import (
    CodeResultSnapshotModel,
    CollaborationEvaluationModel,
    CollaborationInputSelectionModel,
    CollaborationRunModel,
    CommandModel,
    ExecutionRequestModel,
    RuntimeDecisionModel,
    RuntimeProjectionSnapshotModel,
    TaskModel,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.integration.test_native_workspace_setup import (
    SetupCatalog,
    repository_facts,
)
from tests.support.native_agents import (
    PublicAgentRuntimes,
    native_cp_guarded_database,  # noqa: F401
    native_cp_test_cleanup,  # noqa: F401
    native_cp_test_target_guard,  # noqa: F401
)
from tests.unit.test_git_workspace import _repository
from tests.unit.test_native_workspace_setup import setup_request

pytestmark = pytest.mark.integration

FUNCTIONAL_STAGE_SECONDS = 300
FUNCTIONAL_OVERALL_SECONDS = 900
READ_POLL_SECONDS = 0.1


def proposal_body():
    body = _proposal_shape_hint(6)
    body.pop("usage")
    body.pop("knowledge_candidates")
    proposal = body["proposal"]
    proposal["root_task"].update(
        title="Public native delivery", goal_summary="Build the checked result"
    )
    worker = deepcopy(proposal["worker_items"][0])
    intent = deepcopy(proposal["execution_intents"][0])
    proposal["worker_items"], proposal["execution_intents"] = [], []
    for key, upstream in (
        ("source", None),
        ("hard-only", "source"),
        ("integration", "hard-only"),
    ):
        item = deepcopy(worker)
        item.update(key=key, title=key, responsibility=key, role=key)
        proposal["worker_items"].append(item)
        entry = deepcopy(intent)
        entry.update(worker_item_key=key, purpose=key, adapter_preferences=[])
        entry["activation_dependencies"] = (
            []
            if upstream is None
            else [
                {
                    "upstream_worker_item_key": upstream,
                    "downstream_worker_item_key": key,
                    "kind": "hard",
                    "required_artifact_refs": ["artifact_type:diff"],
                }
            ]
        )
        proposal["execution_intents"].append(entry)
    group = proposal["collaboration_policy_request"]["input_groups"][0]
    group.update(
        consumer_worker_item_key="integration",
        candidates=[{"worker_item_key": "hard-only", "priority": 1}],
    )
    group["requirements"]["required_evidence_refs"] = []
    spec = proposal["integration_spec"]
    spec["integration_worker_item_key"] = "integration"
    spec["rework_policy"]["allowed_worker_item_keys"] = [
        "source",
        "hard-only",
        "integration",
    ]
    spec["criteria"][0]["check_keys"] = ["owned-check"]
    spec["checks"][0]["key"] = "owned-check"
    return json.dumps(body)


class PublicJourneyExecution:
    def __init__(self, profile):
        self.profile = profile
        self.reader = None
        self.starts, self.sources, self.stops, self.reads = [], [], [], []
        self.receipts, self.events, self.requests, self.stop_receipts = {}, {}, {}, {}
        self.facts, self.consumed, self.failures = {}, [], []

    def read(self, *args):
        pytest.fail("The unbound host source reader must not be used for CP sources")

    def start(self, request):
        try:
            return self._start(request)
        except Exception as exc:
            frames, trace = [], exc.__traceback__
            while trace is not None:
                if trace.tb_frame.f_globals.get("__name__") == __name__:
                    frames.append(
                        {
                            "function": trace.tb_frame.f_code.co_name,
                            "line": trace.tb_lineno,
                        }
                    )
                trace = trace.tb_next
            self.failures.append(
                {
                    "exception": type(exc).__name__,
                    "operation_id": str(request.operation_id),
                    "frames": frames[-4:],
                }
            )
            raise

    def _consume_results(self, request, result_sources, *, ordinal):
        assert len(result_sources) == 1
        value = json.loads(result_sources[0].text)
        assert set(value) == {"manifest", "inputs"}
        manifest = ResultInputManifestV1.from_mapping(value["manifest"])
        original = self.starts[ordinal - 1]
        original_facts = self.facts[original.operation_id]
        expected_source = "source" if ordinal == 2 else "hard-only"
        expected_request = original_facts["execution_request"]["id"]
        expected_hard_ref = "request:" + expected_request
        expected_groups = () if ordinal == 2 else ("implementation-inputs",)
        assert tuple(item.group_key for item in manifest.groups) == expected_groups
        assert tuple(item.dependency_key for item in manifest.hard_inputs) == (
            expected_hard_ref,
        )
        artifacts = {str(item.artifact_id): item for item in manifest.artifacts}
        materialized = {}
        for item in value["inputs"]:
            artifact = artifacts[item["artifact_id"]]
            for field in (
                "content_hash",
                "size_bytes",
                "media_type",
                "sources",
                "group_keys",
                "hard_refs",
            ):
                assert item[field] == artifact.to_mapping()[field]
            body = item["body_text"].encode("utf-8")
            assert len(body) == artifact.size_bytes
            assert sha256(body).hexdigest() == artifact.content_hash
            assert artifact.group_keys == expected_groups
            assert artifact.hard_refs == (expected_hard_ref,)
            assert len(artifact.sources) == 1
            source = artifact.sources[0]
            assert source.worker_item_key == expected_source
            assert str(source.worker_item_id) == original_facts["worker_item"]["id"]
            assert str(source.effective_request_id) == expected_request
            assert (
                str(source.effective_execution_id) == original_facts["execution"]["id"]
            )
            assert (
                str(source.effective_lineage_id)
                == original_facts["execution"]["lineage_id"]
            )
            assert source.allowed_lineage_ids == (source.effective_lineage_id,)
            assert source.attempt == original_facts["execution"]["attempt"]
            assert not source.inheritance_refs
            previous = materialized.setdefault(item["artifact_id"], item)
            assert previous == item
        assert materialized.keys() == artifacts.keys()
        patches = [
            item
            for artifact_id, item in materialized.items()
            if artifacts[artifact_id].artifact_type == "diff"
        ]
        assert len(patches) == 1
        for item in patches:
            body = item["body_text"].encode("utf-8")
            for options in (("--check",), ()):
                subprocess.run(
                    ["git", "apply", *options, "-"],
                    cwd=request.workspace.directory,
                    input=body,
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
            self.consumed.append(
                {
                    "operation_id": str(request.operation_id),
                    "artifact_id": item["artifact_id"],
                    "content_hash": item["content_hash"],
                    "size_bytes": len(body),
                    "source": expected_source,
                    "groups": list(expected_groups),
                    "hard_ref": expected_hard_ref,
                }
            )

    def _start(self, request):
        assert request.profile_ref == self.profile.ref
        assert request.required_capabilities == ()
        result_sources = []
        for item in request.context:
            if item.source_ref is not None:
                content = self.reader.read(request.grant_id, item.source_ref)
                assert content.actual_version == item.version
                assert content.content_hash == item.content_hash
                assert (
                    content.content_hash
                    == "sha256:" + sha256(content.text.encode()).hexdigest()
                )
                self.sources.append((request.operation_id, item, content))
                if item.item_id == "facts":
                    self.facts[request.operation_id] = json.loads(content.text)
                elif item.item_id == "results":
                    result_sources.append(content)
        ordinal = len(self.starts)
        self.starts.append(request)
        run_id, response_id = uuid4(), uuid4()
        self.requests[run_id] = request
        directory = Path(request.workspace.directory)
        if ordinal:
            assert ordinal in {1, 2, 3}
            assert request.text == "a concrete implementation step"
            assert len(request.instructions) == 1
            instructions = json.loads(request.instructions[0])
            expected_role = ("source", "hard-only", "integration")[ordinal - 1]
            assert instructions["role"] == expected_role
            assert instructions["responsibility"] == expected_role
        if ordinal == 0:
            text = proposal_body()
        elif ordinal == 1:
            (directory / "native_value.py").write_text("VALUE = 42\n")
            text = "Produced the original physical result"
        elif ordinal == 2:
            self._consume_results(request, result_sources, ordinal=ordinal)
            assert (directory / "native_value.py").read_text() == "VALUE = 42\n"
            (directory / "hard_value.py").write_text(
                "from native_value import VALUE\nANSWER = VALUE\n"
            )
            text = "Consumed the hard-only original physical result"
        else:
            assert ordinal == 3
            self._consume_results(request, result_sources, ordinal=ordinal)
            assert (
                directory / "hard_value.py"
            ).read_text() == "from native_value import VALUE\nANSWER = VALUE\n"
            (directory / "integration.txt").write_text("Integrated 42\n")
            text = "Integrated the original physical result"
        now = datetime.now(UTC)
        values = (
            ("run.started", {}),
            ("response.started", {}),
            ("progress.updated", {"update_type": "agent_message_chunk", "text": text}),
            ("response.completed", {}),
            ("run.closed", {"status": "completed"}),
        )
        self.events[run_id] = tuple(
            RunEvent(
                run_id=run_id,
                sequence=sequence,
                kind=kind,
                occurred_at=now,
                operation_id=request.operation_id,
                response_id=None
                if kind in {"run.started", "run.closed"}
                else response_id,
                data=data,
            )
            for sequence, (kind, data) in enumerate(values, 1)
        )
        receipt = StartReceipt(
            operation_id=request.operation_id,
            status="accepted",
            run_id=run_id,
            reason_code=None,
        )
        self.receipts[request.operation_id] = receipt
        return receipt

    def get_start(self, workspace_id, operation_id, *, grant_id):
        receipt = self.receipts.get(operation_id)
        return StartLookup(found=receipt is not None, receipt=receipt)

    def get_run(self, workspace_id, run_id, *, grant_id):
        request = self.requests[run_id]
        assert (
            workspace_id == request.profile_ref.workspace_id
            and grant_id == request.grant_id
        )
        self.reads.append(("run", run_id))
        return RunSnapshot(
            run_id=run_id,
            workspace_id=workspace_id,
            profile_ref=request.profile_ref,
            policy=request.policy,
            state="completed",
            input_open=False,
            active_response_id=None,
            last_event_sequence=len(self.events[run_id]),
            capabilities={},
            reason_code=None,
            effective_config=EffectiveConfig(
                profile_ref=self.profile.ref,
                config_hash=self.profile.config_hash,
                runtime_ref=self.profile.draft.runtime_ref,
                actual_runtime_version="1",
                selected_model=None,
                observed_model=None,
                inherited_fields=(),
                applied_fields=(),
                resource_refs=(),
                context_receipts=(),
            ),
        )

    def list_events(self, workspace_id, run_id, after_sequence, limit=100, *, grant_id):
        request = self.requests[run_id]
        assert (
            workspace_id == request.profile_ref.workspace_id
            and grant_id == request.grant_id
        )
        values = tuple(
            event for event in self.events[run_id] if event.sequence > after_sequence
        )
        visible = values[:limit]
        return EventPage(
            events=visible,
            next_sequence=visible[-1].sequence if visible else after_sequence,
            has_more=len(values) > limit,
        )

    def stop(self, request):
        self.stops.append(request)
        receipt = StopReceipt(
            operation_id=request.operation_id,
            run_id=request.run_id,
            status="stopped",
            reason_code=None,
            evidence_sequence=len(self.events[request.run_id]),
        )
        self.stop_receipts[request.operation_id] = receipt
        return receipt

    def get_stop(self, workspace_id, run_id, operation_id, *, grant_id):
        request = self.requests[run_id]
        assert (
            workspace_id == request.profile_ref.workspace_id
            and grant_id == request.grant_id
        )
        assert self.stop_receipts[operation_id].run_id == run_id
        return self.stop_receipts.get(operation_id)

    def send_input(self, request):
        pytest.fail("Initial input must occur exactly once in native Start")


def diagnostic_value(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,160}", value):
        return value
    if isinstance(value, list):
        return [diagnostic_value(item) for item in value[:20]]
    return "redacted"


def diagnostic_fields(mapping, names):
    return {name: diagnostic_value(mapping.get(name)) for name in names}


def public_failure_diagnostics(client, database, workspace_id, root_task_id, failures):
    diagnostic = {"background_failures": failures[-5:]}
    prefix = f"/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration"
    detail = client.get(prefix)
    diagnostic["collaboration_http_status"] = detail.status_code
    if detail.status_code == 200:
        body = detail.json()
        diagnostic["collaboration"] = diagnostic_fields(
            body,
            (
                "status",
                "run_id",
                "released_count",
                "running_count",
                "waiting_count",
                "completed_count",
                "blocked_subtask_count",
            ),
        )
        diagnostic["collaboration"]["blocked_subtasks"] = [
            diagnostic_fields(item, ("subtask_key", "reason"))
            for item in body.get("blocked_subtasks", [])[:5]
        ]
    decisions = client.get(prefix + "/decisions?limit=5")
    diagnostic["decisions_http_status"] = decisions.status_code
    if decisions.status_code == 200:
        diagnostic["decisions"] = []
        for item in decisions.json().get("items", [])[:5]:
            record = diagnostic_fields(
                item, ("decision_id", "sequence", "outcome", "reason_code")
            )
            record["applications"] = [
                diagnostic_fields(
                    application,
                    ("intent_id", "status", "result_code", "error_code", "release_id"),
                )
                for application in item.get("applications", [])[:5]
            ]
            diagnostic["decisions"].append(record)
    with SqlAlchemyUnitOfWork(database) as uow:
        run = uow.session.scalar(
            select(CollaborationRunModel)
            .where(
                CollaborationRunModel.workspace_id == workspace_id,
                CollaborationRunModel.root_task_id == root_task_id,
            )
            .order_by(
                CollaborationRunModel.created_at.desc(), CollaborationRunModel.id.desc()
            )
            .limit(1)
        )
        diagnostic["latest_target_cursor"] = (
            uow.projection_changes.latest_target_cursor(
                workspace_id, "collaboration_runtime", root_task_id
            )
        )
        requests = tuple(
            uow.session.scalars(
                select(ExecutionRequestModel)
                .where(
                    ExecutionRequestModel.workspace_id == workspace_id,
                    ExecutionRequestModel.root_task_id == root_task_id,
                )
                .order_by(
                    ExecutionRequestModel.worker_item_key, ExecutionRequestModel.id
                )
                .limit(20)
            )
        )
        diagnostic["requests"] = [
            {
                "id": str(item.id),
                "worker_item_key": diagnostic_value(item.worker_item_key),
                "status": item.status,
                "gate_reason": diagnostic_value(item.gate_reason),
                "gate_mode": item.collaboration_gate_mode,
                "version": item.version,
                "first_execution_id": str(item.first_execution_id)
                if item.first_execution_id
                else None,
            }
            for item in requests
        ]
        execution_ids = [
            item.first_execution_id for item in requests if item.first_execution_id
        ]
        commands = tuple(
            uow.session.scalars(
                select(CommandModel)
                .where(
                    CommandModel.workspace_id == workspace_id,
                    CommandModel.target_id.in_(execution_ids),
                    CommandModel.command_type.in_(
                        ("ReportExecutionSucceeded", "ReportExecutionFailed")
                    ),
                )
                .order_by(CommandModel.created_at.desc(), CommandModel.id.desc())
                .limit(10)
            )
        )
        diagnostic["completion_commands"] = [
            {
                "id": str(item.id),
                "command_type": item.command_type,
                "target_id": str(item.target_id),
                "status": item.status,
                "expected_version": item.expected_version,
                "rejection_code": diagnostic_value(item.rejection_reason),
                "target_command_count": sum(
                    other.target_id == item.target_id for other in commands
                ),
            }
            for item in commands
        ]
        if run is not None:
            diagnostic["run"] = {
                "id": str(run.id),
                "status": run.status,
                "blocked_reason": diagnostic_value(run.blocked_reason),
                "last_evaluated_change_cursor": run.last_evaluated_change_cursor,
                "last_projection_snapshot_id": str(run.last_projection_snapshot_id)
                if run.last_projection_snapshot_id
                else None,
            }
            evaluations = tuple(
                uow.session.scalars(
                    select(CollaborationEvaluationModel)
                    .where(
                        CollaborationEvaluationModel.workspace_id == workspace_id,
                        CollaborationEvaluationModel.run_id == run.id,
                    )
                    .order_by(
                        CollaborationEvaluationModel.created_at.desc(),
                        CollaborationEvaluationModel.id.desc(),
                    )
                    .limit(5)
                )
            )
            diagnostic["evaluations"] = [
                {
                    "id": str(item.id),
                    "status": item.status,
                    "attempt": item.attempt,
                    "last_error_code": diagnostic_value(item.last_error_code),
                    "lease_deadline": item.lease_expires_at.isoformat()
                    if item.lease_expires_at
                    else None,
                }
                for item in evaluations
            ]
            decision = uow.session.scalar(
                select(RuntimeDecisionModel)
                .where(
                    RuntimeDecisionModel.workspace_id == workspace_id,
                    RuntimeDecisionModel.run_id == run.id,
                )
                .order_by(RuntimeDecisionModel.sequence.desc())
                .limit(1)
            )
            diagnostic["latest_decision"] = (
                None
                if decision is None
                else {
                    "id": str(decision.id),
                    "sequence": decision.sequence,
                    "outcome": decision.outcome,
                    "reason_code": diagnostic_value(decision.reason_code),
                }
            )
            projection = uow.session.scalar(
                select(RuntimeProjectionSnapshotModel)
                .where(
                    RuntimeProjectionSnapshotModel.workspace_id == workspace_id,
                    RuntimeProjectionSnapshotModel.run_id == run.id,
                )
                .order_by(
                    RuntimeProjectionSnapshotModel.created_at.desc(),
                    RuntimeProjectionSnapshotModel.id.desc(),
                )
                .limit(1)
            )
            if projection is not None:
                body = projection.payload_json
                diagnostic["projection"] = {
                    "id": str(projection.id),
                    "fact_cursor": projection.fact_cursor,
                }
                for collection, names in (
                    (
                        "execution_requests",
                        (
                            "id",
                            "worker_item_key",
                            "status",
                            "eligible",
                            "eligibility_reason",
                            "gate_reason",
                        ),
                    ),
                    (
                        "dependencies",
                        (
                            "dependency_type",
                            "upstream_key",
                            "downstream_key",
                            "upstream_request_id",
                            "downstream_request_id",
                            "enforcement",
                            "required_artifact_refs",
                            "required_evidence_refs",
                        ),
                    ),
                    ("executions", ("id", "worker_item_id", "status", "version")),
                    ("artifacts", ("execution_id", "artifact_type")),
                    ("releases", ("execution_request_id", "decision_id")),
                ):
                    diagnostic["projection"][collection] = [
                        diagnostic_fields(item, names)
                        for item in body.get(collection, [])[:20]
                    ]
                diagnostic["projection"]["result_input_evaluations"] = [
                    {
                        **diagnostic_fields(
                            item, ("consumer_execution_request_id", "group_key")
                        ),
                        **diagnostic_fields(
                            item.get("evaluation", {}),
                            (
                                "satisfied",
                                "reason_code",
                                "ready_count",
                                "required_count",
                            ),
                        ),
                    }
                    for item in body.get("result_input_evaluations", [])[:20]
                ]
    return diagnostic


def wait_for(read, ready, *, stage, overall_deadline, stage_times, on_failure=None):
    started = time.monotonic()
    deadline = min(started + FUNCTIONAL_STAGE_SECONDS, overall_deadline)
    latest = None
    while time.monotonic() < deadline:
        latest = read()
        if ready(latest):
            stage_times[stage] = round(time.monotonic() - started, 3)
            return latest
        time.sleep(READ_POLL_SECONDS)
    stage_times[stage] = round(time.monotonic() - started, 3)
    diagnostic = None
    if on_failure is not None:
        try:
            diagnostic = on_failure()
        except Exception as exc:  # noqa: BLE001 - Diagnostics must preserve the original failure.
            diagnostic = {
                "diagnostic_error_class": type(exc).__name__,
                "diagnostic_error_code": diagnostic_value(getattr(exc, "code", None)),
            }
    pytest.fail(
        f"Public journey did not reach its boundary: {latest}; diagnostics={diagnostic}"
    )


def test_public_setup_background_hard_only_and_actual_result_validation(
    database, tmp_path, monkeypatch
):
    workspace_id, user_grant, system_grant = uuid4(), uuid4(), uuid4()
    repository, _ = _repository(tmp_path)
    (repository / "check.py").write_text(
        "from native_value import VALUE\nfrom hard_value import ANSWER\n"
        "from pathlib import Path\nassert VALUE == ANSWER == 42\n"
        "assert Path('integration.txt').read_text() == 'Integrated 42\\n'\n"
    )
    subprocess.run(
        ["git", "add", "check.py"], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Controlled verification"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    before = repository_facts(repository)
    user = ControlPlaneIdentity("operator", user_grant, "user")
    system = ControlPlaneIdentity("operator", system_grant, "system")

    def require(grant_id, action, target):
        if (
            grant_id not in {user_grant, system_grant}
            or getattr(target, "workspace_id", None) != workspace_id
        ):
            raise AgentError("permission_denied", "Controlled exact workspace denied")

    authority = SimpleNamespace(
        authenticate_request=lambda request: user,
        identity_for_grant=lambda grant: {user_grant: user, system_grant: system}.get(
            grant
        ),
        require=require,
    )
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    catalog = SetupCatalog(())
    profile = catalog.create_profile(
        workspace_id,
        ProfileDraft(
            name="Controlled public native Profile",
            description="",
            runtime_ref=runtime_ref,
            instructions="",
            resource_refs=(),
            model_id=None,
            thinking=None,
            max_active=2,
            environment={},
            secret_refs=(),
            native_settings={},
        ),
        grant_id=user_grant,
    )
    runtimes = PublicAgentRuntimes(
        RuntimeSnapshot(
            ref=runtime_ref,
            harness_id="controlled-opencode",
            executable="/unused",
            actual_version="1",
            installed=True,
            enabled=True,
            version=1,
            protocol_state="ready",
            auth_state="unconfirmed",
            checked_at=datetime.now(UTC),
            capabilities={
                key: {
                    "status": "supported",
                    "reason_code": "controlled",
                    "constraints": {},
                }
                for key in ("acp", "model", "native_tools")
            },
            limits={},
        )
    )
    external = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"items": [], "next_cursor": None})
                if request.method == "GET"
                and request.url.path.endswith("/knowledge/search")
                else pytest.fail("Unexpected controlled Knowledge request")
            )
        )
    )

    def knowledge_adapter(**options):
        return KnowledgeHttpAdapter(**options, client=external)

    for module in ("context_package_worker", "planning_worker"):
        monkeypatch.setattr(
            "sidera_control_plane." + module + ".KnowledgeHttpAdapter",
            knowledge_adapter,
        )
    owned_root = tmp_path / "owned-workspaces"
    owned_root.mkdir()
    execution = PublicJourneyExecution(profile)
    failures = []
    gateway_handle = WorkerGateway.handle

    def observe_gateway(gateway, message):
        try:
            return gateway_handle(gateway, message)
        except Exception as exc:
            frames, trace = [], exc.__traceback__
            while trace is not None:
                module = trace.tb_frame.f_globals.get("__name__", "")
                if module.startswith("sidera_control_plane."):
                    frames.append(
                        {
                            "module": module,
                            "function": trace.tb_frame.f_code.co_name,
                            "line": trace.tb_lineno,
                        }
                    )
                trace = trace.tb_next
            record = {
                "exception": type(exc).__name__,
                "code": diagnostic_value(getattr(exc, "code", None)),
                "frames": frames[-4:],
            }
            if record not in failures:
                failures.append(record)
                if len(failures) > 10:
                    failures.pop(0)
            raise

    monkeypatch.setattr(WorkerGateway, "handle", observe_gateway)
    runtime = create_control_plane_runtime(
        Settings(
            database_url=database.engine.url.render_as_string(hide_password=False),
            local_actor_id="operator",
            local_actor_type="user",
            workspace_root=owned_root,
            planning_temp_root=tmp_path / "planning",
            result_repository_path=None,
            result_store_root=tmp_path / "results",
            artifact_root=tmp_path / "artifacts",
            knowledge_candidate_spool_dir=tmp_path / "spool",
            result_inputs_enabled=True,
            result_integration_enabled=True,
            scheduler_poll_interval_seconds=0.03,
            activation_poll_interval_seconds=0.03,
            gateway_poll_interval_seconds=0.03,
            planning_poll_interval_seconds=0.03,
            plan_transition_poll_interval_seconds=0.03,
            reconciler_poll_interval_seconds=0.1,
            handoff_poll_interval_seconds=0.1,
            collaboration_poll_seconds=0.03,
            assignment_lease_renew_interval_seconds=0.1,
        ),
        execution,
        catalog,
        execution,
        execution,
        authority,
        agent_runtimes=runtimes,
    )
    reader = runtime.context_source_reader
    execution.reader = reader
    functional_started = time.monotonic()
    overall_deadline = functional_started + FUNCTIONAL_OVERALL_SECONDS
    stage_times, observed_stages, terminal_facts = {}, {}, {}
    try:
        assert runtime.readiness() is True and execution.starts == []
        runtime.start_background(background_identity=system)
        request = setup_request(workspace_id, repository)
        request = replace(
            request,
            planner_agent_ref=profile.ref,
            verification_checks=tuple(
                replace(check, argv=("python3", "-B", "check.py"))
                for check in request.verification_checks
            ),
        )
        receipt = runtime.configure_workspace(request, grant_id=user_grant)
        assert reader is runtime.context_source_reader and not execution.starts
        with TestClient(runtime.app) as client:

            def command(kind, payload, *, target=None, version=None):
                command_id = str(uuid4())
                response = client.post(
                    "/api/v1/commands",
                    json={
                        "command_id": command_id,
                        "workspace_id": str(workspace_id),
                        "command_type": kind,
                        "target_type": "planning_request" if target else "workspace",
                        "target_id": target,
                        "expected_version": version,
                        "actor": {"type": "user", "id": "operator"},
                        "idempotency_key": command_id,
                        "correlation_id": command_id,
                        "payload": payload,
                    },
                )
                assert response.status_code in {200, 201}, response.json()
                return response.json()

            submitted = command(
                "SubmitTaskGoal",
                {
                    "title": "Public native delivery",
                    "objective": "Build the checked result",
                    "acceptance_criteria": ["acceptance:one"],
                    "requested_outputs": ["diff"],
                    "context_refs": [],
                    "planning_profile_id": str(receipt.planning_policy_ref.policy_id),
                    "planning_profile_version": receipt.planning_policy_ref.version,
                    "planner_agent_ref": profile.ref.model_dump(mode="json"),
                },
            )
            planning_id = submitted["data"]["planning_request_id"]
            root_id = submitted["aggregate_id"]

            def get(path):
                response = client.get(f"/api/v1/workspaces/{workspace_id}/" + path)
                assert response.status_code == 200, response.json()
                return response.json()

            detail = wait_for(
                lambda: get("planning-requests/" + planning_id),
                lambda item: item["status"] == "proposal_ready",
                stage="planning",
                overall_deadline=overall_deadline,
                stage_times=stage_times,
            )
            proposal = get(f"planning-requests/{planning_id}/proposals/1")
            choices = [
                {
                    "target_type": "subtask",
                    "target_key": "subtask-one",
                    "assignee_kind": "human",
                    "assignee_id": "operator",
                    "profile_revision": None,
                    "expected_binding_version": None,
                }
            ]
            choices += [
                {
                    "target_type": "worker_item",
                    "target_key": key,
                    "assignee_kind": "profile",
                    "assignee_id": str(profile.ref.profile_id),
                    "profile_revision": profile.ref.revision,
                    "expected_binding_version": None,
                }
                for key in ("source", "hard-only", "integration")
            ]
            command(
                "ConfirmPlanningProposal",
                {
                    "proposal_revision": 1,
                    "proposal_content_hash": proposal["content_hash"],
                    "profile_id": str(receipt.planning_policy_ref.policy_id),
                    "profile_version": 1,
                    "assignee_bindings": choices,
                },
                target=planning_id,
                version=detail["version"],
            )
            materialization = get(f"planning-requests/{planning_id}/materialization")

            def read_result():
                observed = {
                    "result": get(f"tasks/{root_id}/results"),
                    "starts": len(execution.starts),
                    "workers": {
                        key: get(f"worker-items/{item_id}/workspace")
                        for key, item_id in materialization[
                            "worker_item_mapping"
                        ].items()
                    },
                }
                if execution.failures:
                    pytest.fail(
                        "Controlled Start fixture failed: "
                        + json.dumps(execution.failures, sort_keys=True)
                    )
                for key, worker in observed["workers"].items():
                    current = worker.get("current_runtime")
                    if current is not None:
                        label = key + "." + current["execution_status"]
                        observed_stages.setdefault(
                            label, round(time.monotonic() - functional_started, 3)
                        )
                terminal_facts.update(
                    result_stage=observed["result"]["result_stage"],
                    verification_statuses=[
                        item["verification_status"]
                        for item in observed["result"]["criteria"]
                    ],
                    worker_statuses={
                        key: (
                            worker["current_runtime"]["execution_status"]
                            if worker.get("current_runtime") is not None
                            else None
                        )
                        for key, worker in observed["workers"].items()
                    },
                )
                if any(
                    item["verification_status"] in {"blocked", "failed"}
                    for item in observed["result"]["criteria"]
                ):
                    candidate = observed["result"]["current_candidate"]
                    history = (
                        []
                        if candidate is None
                        else get(
                            f"tasks/{root_id}/results/"
                            f"{candidate['candidate_id']}/verifications"
                        )["items"]
                    )
                    terminal_facts["verification_history"] = [
                        diagnostic_fields(
                            item,
                            (
                                "check_key",
                                "check_version",
                                "status",
                                "failure_code",
                                "exit_code",
                            ),
                        )
                        for item in history[:20]
                    ]
                    pytest.fail(
                        "Public verification reached a terminal failure: "
                        + json.dumps(terminal_facts, sort_keys=True)
                    )
                return observed

            progress = wait_for(
                read_result,
                lambda item: item["result"]["result_stage"] == "awaiting_acceptance",
                stage="execution_and_verification",
                overall_deadline=overall_deadline,
                stage_times=stage_times,
                on_failure=lambda: public_failure_diagnostics(
                    client, database, workspace_id, UUID(root_id), failures
                ),
            )
            result = progress["result"]
            assert result["criteria"][0]["verification_status"] == "passed"
        assert len(execution.starts) == 4
        assert len(execution.consumed) == 2
        assert [item["source"] for item in execution.consumed] == [
            "source",
            "hard-only",
        ]
        assert all(
            any(
                operation == start.operation_id for operation, _, _ in execution.sources
            )
            for start in execution.starts
        )
        assert repository_facts(repository) == before
        with SqlAlchemyUnitOfWork(database) as uow:
            requests = tuple(
                uow.session.scalars(
                    select(ExecutionRequestModel).where(
                        ExecutionRequestModel.workspace_id == workspace_id
                    )
                )
            )
            target = next(
                item for item in requests if item.worker_item_key == "hard-only"
            )
            rows = tuple(
                uow.session.scalars(
                    select(CollaborationInputSelectionModel).where(
                        CollaborationInputSelectionModel.workspace_id == workspace_id,
                        CollaborationInputSelectionModel.target_execution_request_id
                        == target.id,
                    )
                )
            )
            assert (
                len(rows) == 1
                and rows[0].groups_json == []
                and len(rows[0].hard_inputs_json) == 1
            )
            selection = uow.input_selections.get(workspace_id, rows[0].id)
            assert selection.selection_hash == selection.compute_selection_hash()
            assert selection.hard_inputs[0].source.artifacts
            capture_count = uow.session.scalar(
                select(func.count())
                .select_from(CodeResultSnapshotModel)
                .where(CodeResultSnapshotModel.workspace_id == workspace_id)
            )
            assert capture_count >= 3
            task_count = uow.session.scalar(
                select(func.count())
                .select_from(TaskModel)
                .where(TaskModel.workspace_id == workspace_id)
            )
            assert task_count == 2
            terminal_facts.update(
                captures=capture_count,
                tasks=task_count,
                hard_only_groups=len(selection.groups),
                hard_only_inputs=len(selection.hard_inputs),
                original_repository_unchanged=True,
            )
    except BaseException as exc:
        terminal_facts["functional_failure"] = type(exc).__name__
        raise
    finally:
        functional_elapsed = round(time.monotonic() - functional_started, 3)
        try:
            closed = runtime.close(grace_seconds=5)
            print(
                "N16_PUBLIC_JOURNEY "
                + json.dumps(
                    {
                        "stage_seconds": stage_times,
                        "observed_stage_seconds": observed_stages,
                        "functional_seconds": functional_elapsed,
                        "terminal": terminal_facts,
                        "starts": len(execution.starts),
                        "source_reads": len(execution.sources),
                        "consumed": execution.consumed,
                        "fixture_failures": execution.failures,
                        "gateway_failures": failures,
                        "close_5": closed,
                    },
                    sort_keys=True,
                )
            )
            if "functional_failure" not in terminal_facts:
                assert closed is True
        finally:
            external.close()
