from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.demo.contracts import DemoManifest
from sidera_control_plane.demo.environment import runtime_root_for
from sidera_control_plane.demo.faults import consume_fault, consume_fault_once
from sidera_control_plane.demo.sample_content import (
    BROKEN_INTEGRATION_SOURCE,
    CONTACTS_TSV,
    INITIAL_IMPORTER,
    INITIAL_REPORT,
    INITIAL_VALIDATION,
    INTEGRATION_SOURCE,
    OWNER_MARKER,
    REPORT_SOURCE,
    TSV_TESTS,
    VALIDATION_SOURCE,
)
from sidera_control_plane.demo.state import (
    DemoStateStore,
    OperationRecord,
    StateIdentityConflict,
    StoredEvent,
)
from sidera_control_plane.domain.artifacts import ArtifactCandidate, ArtifactType
from sidera_control_plane.domain.gateway import (
    AdapterSessionHandle,
    DeliveryOutcome,
    DeliveryResult,
)

PhonePolicy = Literal["optional", "required", "unsupported"]
_WORKER_ITEM_KEYS = {
    "validate",
    "report",
    "review-report",
    "integrate",
    "parse-tsv",
    "source-a",
    "source-b",
    "integration",
}
_WORKER_MODULE_KEYS = ("worker_primary", "worker_secondary")
_PROGRESS_EVENT = "progress.updated"
_GIT_OID_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_REPORT_FIXTURE_RESULTS = (
    {
        "row": {"name": "Alice", "email": "alice@example.com", "phone": "13800138000"},
        "row_number": 1,
        "valid": True,
        "errors": [],
    },
    {
        "row": {"name": "Bob", "email": "bob@example.com", "phone": ""},
        "row_number": 2,
        "valid": True,
        "errors": [],
    },
    {
        "row": {
            "name": "Bad Email",
            "email": "bob.example.com",
            "phone": "13800138001",
        },
        "row_number": 3,
        "valid": False,
        "errors": ["email_invalid"],
    },
    {
        "row": {"name": "Bad Phone", "email": "badphone@example.com", "phone": "123"},
        "row_number": 4,
        "valid": False,
        "errors": ["phone_invalid"],
    },
)
_VALIDATION_RUNNER = """\
import csv
import json
import sys
from pathlib import Path
from csv_importer.validation import validate_rows

options = json.load(sys.stdin)
with Path("fixtures/contacts.csv").open(encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream))
print(json.dumps(validate_rows(rows, phone_optional=options["phone_optional"]), ensure_ascii=False, sort_keys=True))
"""
_REPORT_RUNNER = """\
import json
import sys
from csv_importer.report import build_report

options = json.load(sys.stdin)
print(json.dumps(build_report(options["results"]), ensure_ascii=False, sort_keys=True))
"""


class DemoWorkerStepError(ValueError):
    """A deterministic demo Worker step cannot safely advance."""


def interpret_phone_policy(text: str) -> PhonePolicy:
    normalized = "".join(text.lower().split())
    optional = any(
        phrase in normalized
        for phrase in (
            "允许为空",
            "可以为空",
            "可为空",
            "手机号可选",
            "非必填",
            "optional",
        )
    )
    required = any(
        phrase in normalized
        for phrase in ("不能为空", "必须填写", "必须提供", "手机号必填", "required")
    )
    if optional == required:
        return "unsupported"
    return "optional" if optional else "required"


def advance_worker(manifest: DemoManifest, session_id: UUID) -> tuple[StoredEvent, ...]:
    state, session = _load_session(manifest, session_id)
    gate_state = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    before = state.events_after(session_id, 0)
    cursor = max((event.sequence for event in before), default=0)
    if not gate_state.read_gate("parallel"):
        return ()

    worker_item_key = _session_worker_key(session)
    _assert_workspace(manifest, session)
    start_operation_id = _session_start_operation_id(session)
    _progress(
        state,
        session_id,
        worker_item_key,
        "read-requirements",
        "assistant_summary",
        "读取要求",
        start_operation_id,
    )

    if worker_item_key == "validate":
        _advance_validation(manifest, state, session_id, session)
    elif worker_item_key == "report":
        _advance_report(manifest, state, session_id, session)
    elif worker_item_key == "review-report":
        _advance_review(manifest, state, session_id, session)
    elif worker_item_key == "parse-tsv":
        _advance_parse_tsv(manifest, state, session_id, session)
    elif worker_item_key == "integrate":
        _advance_integration(manifest, state, session_id, session)
    elif worker_item_key == "source-a":
        _advance_closure_source(manifest, state, session_id, session, "source-a")
    elif worker_item_key == "source-b":
        _advance_closure_source(manifest, state, session_id, session, "source-b")
    elif worker_item_key == "integration":
        _advance_closure_integration(manifest, state, session_id, session)
    else:
        raise DemoWorkerStepError("worker_item_key_not_supported")

    if gate_state.read_gate("before-artifact"):
        trigger_ids = _output_trigger_ids(state, session_id, worker_item_key, session)
        ready_outputs = tuple(
            (trigger_id, _read_output(state, session_id, trigger_id))
            for trigger_id in trigger_ids
        )
        for trigger_id, output in ready_outputs:
            if output is not None:
                _progress(
                    state,
                    session_id,
                    worker_item_key,
                    f"result-ready-{trigger_id.hex}",
                    "artifact",
                    "产出结果",
                    trigger_id,
                    input_operation_id=output.get("input_operation_id"),
                )
        if ready_outputs and all(output is not None for _, output in ready_outputs):
            operation_id = uuid5(
                _session_start_operation_id(session),
                f"sidera.demo.worker-session-completed:{session_id}",
            )
            state.append_event(
                session_id,
                operation_id,
                "session.completed",
                {
                    "execution_id": str(session["execution_id"]),
                    "worker_id": str(session["worker_id"]),
                    "worker_item_key": worker_item_key,
                    "adapter_module_id": str(session["adapter_module_id"]),
                    "fencing_token": session["fencing_token"],
                    "workspace_digest": str(session["workspace_digest"]),
                    "simulation": True,
                },
            )
    return state.events_after(session_id, cursor)


def artifact_candidates(
    manifest: DemoManifest, session_id: UUID
) -> tuple[ArtifactCandidate, ...]:
    state, session = _load_session(manifest, session_id)
    gate_state = DemoStateStore(runtime_root_for(manifest) / "state", manifest.run_id)
    if not gate_state.read_gate("parallel") or not gate_state.read_gate(
        "before-artifact"
    ):
        return ()
    worker_item_key = _session_worker_key(session)
    candidates: list[ArtifactCandidate] = []
    for trigger_id in _output_trigger_ids(state, session_id, worker_item_key, session):
        output = _read_output(state, session_id, trigger_id)
        if output is None:
            continue
        try:
            content = str(output["content"]).encode("utf-8")
            digest = str(output["content_hash"])
            artifact_type = ArtifactType(str(output["artifact_type"]))
            media_type = str(output["media_type"])
            raw_size = output["size_bytes"]
            acceptance_refs = output.get("acceptance_criterion_refs", [])
        except (KeyError, TypeError, ValueError) as exc:
            raise DemoWorkerStepError("worker_output_record_invalid") from exc
        if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0:
            raise DemoWorkerStepError("worker_output_record_invalid")
        size_bytes = raw_size
        if not isinstance(acceptance_refs, list) or any(
            not isinstance(item, str) or not item.strip() for item in acceptance_refs
        ):
            raise DemoWorkerStepError("worker_output_record_invalid")
        if hashlib.sha256(content).hexdigest() != digest or len(content) != size_bytes:
            raise DemoWorkerStepError("worker_output_integrity_mismatch")
        candidates.append(
            ArtifactCandidate(
                artifact_type=artifact_type,
                storage_ref=f"interview-demo://artifact/{session_id}/{digest}",
                content_hash=digest,
                size_bytes=size_bytes,
                media_type=media_type,
                provenance={
                    "simulation": True,
                    "worker_item_key": worker_item_key,
                    "input_operation_id": output.get("input_operation_id"),
                    "content_hash": digest,
                    "source_artifact_ids": output.get("source_artifact_ids", []),
                    "acceptance_criterion_refs": acceptance_refs,
                    "context_identity": session.get("context_identity"),
                },
                content=content,
            )
        )
    return tuple(candidates)


def receive_worker_input(
    manifest: DemoManifest,
    state: DemoStateStore,
    session: Mapping[str, object],
    handle: AdapterSessionHandle,
    text: str,
    attachment_refs: tuple[str, ...],
    operation_id: UUID,
) -> DeliveryResult:
    session_id = handle.execution_session_id
    if session.get("context_identity") is None:
        return _blocked_delivery(operation_id, session_id, "worker_context_missing")
    if not text.strip():
        return _blocked_delivery(operation_id, session_id, "empty_input")
    if attachment_refs:
        return _blocked_delivery(operation_id, session_id, "attachments_unsupported")
    if _session_worker_key(session) != "validate":
        return _blocked_delivery(operation_id, session_id, "input_target_not_supported")

    advance_worker(manifest, session_id)
    events = state.events_after(session_id, 0)
    if any(event.event_type == "session.stopped" for event in events):
        return _blocked_delivery(operation_id, session_id, "session_stopped")
    if not any(
        event.payload.get("step_key") == "waiting-confirmation" for event in events
    ):
        return _blocked_delivery(
            operation_id, session_id, "worker_not_waiting_for_input"
        )

    key = f"input/{session_id}/{operation_id}"
    request = {
        "session_id": str(session_id),
        "worker_item_key": "validate",
        "text": text,
        "attachment_refs": list(attachment_refs),
        "context_identity": session.get("context_identity"),
    }
    record = state.reserve(key, operation_id, request)
    if record.status in {"applied", "rejected"}:
        return _delivery_from_input_record(record, session_id)

    accepted_event = _append_event(
        state,
        session_id,
        operation_id,
        "input-accepted",
        "input.accepted",
        {
            "worker_item_key": "validate",
            "input_operation_id": str(operation_id),
            "text": text,
            "trace_summary": text,
            "simulation": True,
        },
    )
    if manifest.scenario == "recovery" and not any(
        event.event_type == "fault.injected"
        and event.payload.get("point") == "after-input-persist"
        and event.payload.get("input_operation_id") == str(operation_id)
        for event in state.events_after(session_id, 0)
    ) and consume_fault_once(manifest, "after-input-persist", operation_id):
        _append_event(
            state,
            session_id,
            operation_id,
            "after-input-persist",
            "fault.injected",
            {
                "point": "after-input-persist",
                "input_operation_id": str(operation_id),
                "simulation": True,
            },
        )
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.BLOCKED,
            accepted_sequence=accepted_event.sequence,
            failure_code="safe_boundary_unavailable",
            execution_session_id=session_id,
        )
    policy = interpret_phone_policy(text)
    if policy == "unsupported":
        _progress(
            state,
            session_id,
            "validate",
            f"input-unhandled-{operation_id.hex}",
            "assistant_summary",
            "输入未处理，Worker 保留原文并等待明确的手机号策略",
            operation_id,
            input_operation_id=str(operation_id),
            handled=False,
        )
        state.settle(
            key,
            operation_id,
            "rejected",
            {
                "outcome": DeliveryOutcome.BLOCKED.value,
                "failure_code": "input_not_supported",
                "accepted_sequence": accepted_event.sequence,
            },
        )
        return _blocked_delivery(
            operation_id,
            session_id,
            "input_not_supported",
            accepted_sequence=accepted_event.sequence,
        )

    advance_worker(manifest, session_id)
    output = _read_output(state, session_id, operation_id)
    if output is None:
        return DeliveryResult(
            operation_id=operation_id,
            outcome=DeliveryOutcome.UNKNOWN,
            accepted_sequence=None,
            failure_code="worker_output_not_persisted",
            execution_session_id=session_id,
        )
    state.settle(
        key,
        operation_id,
        "applied",
        {
            "outcome": DeliveryOutcome.DELIVERED.value,
            "accepted_sequence": accepted_event.sequence,
            "content_hash": output["content_hash"],
        },
    )
    return DeliveryResult(
        operation_id=operation_id,
        outcome=DeliveryOutcome.DELIVERED,
        accepted_sequence=accepted_event.sequence,
        failure_code=None,
        execution_session_id=session_id,
    )


def _advance_validation(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
) -> None:
    _implement(
        manifest,
        state,
        session_id,
        session,
        "validate",
        {"csv_importer/validation.py": (INITIAL_VALIDATION, VALIDATION_SOURCE)},
    )
    _progress(
        state,
        session_id,
        "validate",
        "waiting-confirmation",
        "assistant_summary",
        "等待确认手机号为空时的处理规则",
        _session_start_operation_id(session),
        requires_user_input=True,
    )
    for event in _input_events(state, session_id, "validate"):
        input_operation_id = UUID(str(event.payload["input_operation_id"]))
        policy = interpret_phone_policy(str(event.payload["text"]))
        if policy == "unsupported":
            _progress(
                state,
                session_id,
                "validate",
                f"input-unhandled-{input_operation_id.hex}",
                "assistant_summary",
                "输入未处理，Worker 保留原文并等待明确的手机号策略",
                input_operation_id,
                input_operation_id=str(input_operation_id),
                handled=False,
            )
            continue
        body = _run_validation(
            _workspace_path(session), phone_optional=policy == "optional"
        )
        rows = body.get("rows")
        if not isinstance(rows, list):
            raise DemoWorkerStepError("validation_result_invalid")
        _save_output(
            state,
            session_id,
            session=session,
            worker_item_key="validate",
            trigger_id=input_operation_id,
            input_operation_id=input_operation_id,
            artifact_type=ArtifactType.REPORT,
            media_type="application/json",
            content=_canonical_json(body),
            source_artifact_ids=(),
        )
        _progress(
            state,
            session_id,
            "validate",
            f"update-validation-{input_operation_id.hex}",
            "tool",
            "更新校验并执行样例 CSV",
            input_operation_id,
            input_operation_id=str(input_operation_id),
            phone_policy=policy,
            rows_checked=len(rows),
        )


def _advance_closure_source(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
    worker_item_key: Literal["source-a", "source-b"],
) -> None:
    trigger_id = _session_start_operation_id(session)
    if _read_output(state, session_id, trigger_id) is not None:
        return
    if worker_item_key == "source-a":
        relative_path = "out/source-a.json"
        payload = {
            "marker": "SIDERA_CLOSURE_SOURCE_A_V1",
            "items": [
                {"id": "api", "status": "done"},
                {"id": "ui", "status": "queued"},
            ],
        }
    else:
        relative_path = "out/source-b.json"
        payload = {
            "marker": "SIDERA_CLOSURE_SOURCE_B_V1",
            "items": [
                {"id": "tests", "status": "running"},
                {"id": "docs", "status": "blocked"},
            ],
        }
    content = _canonical_json(payload)
    _implement(
        manifest,
        state,
        session_id,
        session,
        worker_item_key,
        {relative_path: (None, content.decode("utf-8"))},
    )
    _save_output(
        state,
        session_id,
        session=session,
        worker_item_key=worker_item_key,
        trigger_id=trigger_id,
        input_operation_id=None,
        artifact_type=ArtifactType.EXTERNAL,
        media_type="application/json",
        content=content,
        source_artifact_ids=(),
    )


def _advance_closure_integration(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
) -> None:
    trigger_id = _session_start_operation_id(session)
    if _read_output(state, session_id, trigger_id) is not None:
        return
    source_a = _result_input(session, "source-a")
    source_b = _result_input(session, "source-b")
    source_a_body = _closure_source_body("source-a", source_a["body_text"])
    source_b_body = _closure_source_body("source-b", source_b["body_text"])
    if source_a_body is None or source_b_body is None:
        raise DemoWorkerStepError("selected_closure_source_invalid")
    items = [*_closure_items(source_a_body), *_closure_items(source_b_body)]
    statuses = [str(item["status"]) for item in items]
    expected_statuses = {"done", "queued", "running", "blocked"}
    if set(statuses) != expected_statuses or len(statuses) != 4:
        raise DemoWorkerStepError("closure_summary_statuses_invalid")
    summary = {
        "total": len(items),
        "counts": {status: statuses.count(status) for status in sorted(expected_statuses)},
        "source_markers": [
            source_a_body["marker"],
            source_b_body["marker"],
        ],
    }
    content = _canonical_json(summary)
    _implement(
        manifest,
        state,
        session_id,
        session,
        "integration",
        {
            "out/source-a.json": (None, str(source_a["body_text"])),
            "out/source-b.json": (None, str(source_b["body_text"])),
            "out/summary.json": (None, content.decode("utf-8")),
        },
    )
    _save_output(
        state,
        session_id,
        session=session,
        worker_item_key="integration",
        trigger_id=trigger_id,
        input_operation_id=None,
        artifact_type=ArtifactType.REPORT,
        media_type="application/json",
        content=content,
        source_artifact_ids=(
            str(source_a["artifact_id"]),
            str(source_b["artifact_id"]),
        ),
    )


def _advance_report(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
) -> None:
    _implement(
        manifest,
        state,
        session_id,
        session,
        "report",
        {"csv_importer/report.py": (INITIAL_REPORT, REPORT_SOURCE)},
    )
    trigger_id = _session_start_operation_id(session)
    if _read_output(state, session_id, trigger_id) is not None:
        return
    report = _run_report(_workspace_path(session), _REPORT_FIXTURE_RESULTS)
    body = {
        "schema_version": 1,
        "simulation": True,
        "sample_source": "fixtures/contacts.csv",
        "report": report,
    }
    _save_output(
        state,
        session_id,
        session=session,
        worker_item_key="report",
        trigger_id=trigger_id,
        input_operation_id=None,
        artifact_type=ArtifactType.REPORT,
        media_type="application/json",
        content=_canonical_json(body),
        source_artifact_ids=(),
    )


def _advance_review(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
) -> None:
    trigger_id = _session_start_operation_id(session)
    if _read_output(state, session_id, trigger_id) is not None:
        return
    source = _result_input(session, "report")
    try:
        report_body = json.loads(str(source["body_text"]))
        report = report_body["report"]
        accepted = report["accepted"]
        rejected = report["rejected"]
        errors = report["errors"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DemoWorkerStepError("selected_report_input_invalid") from exc
    if (
        not isinstance(accepted, int)
        or isinstance(accepted, bool)
        or not isinstance(rejected, int)
        or isinstance(rejected, bool)
        or not isinstance(errors, list)
        or accepted < 0
        or rejected < 0
    ):
        raise DemoWorkerStepError("selected_report_counts_invalid")
    passed = len(errors) == rejected
    source_id = str(source["artifact_id"])
    review = (
        "# CSV import review\n\n"
        f"Source artifact: `{source_id}`\n\n"
        f"Accepted rows: {accepted}\n\n"
        f"Rejected rows: {rejected}\n\n"
        f"Row-level errors: {len(errors)}\n\n"
        f"Review: {'passed' if passed else 'needs changes'}\n"
    )
    _implement(
        manifest,
        state,
        session_id,
        session,
        "review-report",
        {"artifacts/review.md": (None, review)},
    )
    _save_output(
        state,
        session_id,
        session=session,
        worker_item_key="review-report",
        trigger_id=trigger_id,
        input_operation_id=None,
        artifact_type=ArtifactType.REPORT,
        media_type="text/plain",
        content=review.encode("utf-8"),
        source_artifact_ids=(source_id,),
    )


def _advance_parse_tsv(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
) -> None:
    trigger_id = _session_start_operation_id(session)
    if _read_output(state, session_id, trigger_id) is not None:
        return
    _implement(
        manifest,
        state,
        session_id,
        session,
        "parse-tsv",
        {
            "fixtures/contacts.tsv": (None, CONTACTS_TSV),
            "tests/test_tsv.py": (None, TSV_TESTS),
        },
    )
    body = {
        "schema_version": 1,
        "simulation": True,
        "format": "tsv",
        "delimiter": "\\t",
        "sample_source": "fixtures/contacts.tsv",
        "rows": 4,
    }
    _save_output(
        state,
        session_id,
        session=session,
        worker_item_key="parse-tsv",
        trigger_id=trigger_id,
        input_operation_id=None,
        artifact_type=ArtifactType.REPORT,
        media_type="application/json",
        content=_canonical_json(body),
        source_artifact_ids=(),
    )


def _advance_integration(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
) -> None:
    trigger_id = _session_start_operation_id(session)
    if _read_output(state, session_id, trigger_id) is not None:
        return
    rework_execution = session.get("result_derivation_kind") == "rework"
    if rework_execution:
        validation_input = None
        report_input = None
        tsv_input = None
        optional = True
        policy = "optional"
    else:
        validation_input = _result_input(session, "validate")
        report_input = _result_input(session, "report")
        tsv_input = _result_input_if_present(session, "parse-tsv")
        try:
            validation = json.loads(str(validation_input["body_text"]))
            policy = str(validation["phone_policy"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DemoWorkerStepError("selected_validation_input_invalid") from exc
        if policy not in {"optional", "required"}:
            raise DemoWorkerStepError("selected_phone_policy_invalid")
        _result_input(session, "report")
        optional = policy == "optional"
    importer_source = INTEGRATION_SOURCE.replace(
        "phone_optional=True", f"phone_optional={optional}"
    )
    if importer_source == INTEGRATION_SOURCE and not optional:
        raise DemoWorkerStepError("integration_policy_template_mismatch")
    if manifest.scenario == "rework" and not rework_execution and consume_fault(
        manifest,
        "verification-fails-once",
        trigger_id,
    ):
        importer_source = BROKEN_INTEGRATION_SOURCE
    _implement(
        manifest,
        state,
        session_id,
        session,
        "integrate",
        {
            "csv_importer/validation.py": (INITIAL_VALIDATION, VALIDATION_SOURCE),
            "csv_importer/report.py": (INITIAL_REPORT, REPORT_SOURCE),
            "csv_importer/importer.py": (INITIAL_IMPORTER, importer_source),
            **(
                {
                    "fixtures/contacts.tsv": (None, CONTACTS_TSV),
                    "tests/test_tsv.py": (None, TSV_TESTS),
                }
                if tsv_input is not None
                else {}
            ),
        },
    )
    workspace = _workspace_path(session)
    _run_git(("diff", "--check", manifest.base_commit, "--"), cwd=workspace)
    diff = _run_git(
        ("diff", "--binary", manifest.base_commit, "--"), cwd=workspace
    ).stdout
    if not diff:
        raise DemoWorkerStepError("integration_diff_empty")
    source_artifact_ids: list[str] = []
    if validation_input is not None and report_input is not None:
        source_artifact_ids.extend(
            [str(validation_input["artifact_id"]), str(report_input["artifact_id"])]
        )
        if tsv_input is not None:
            source_artifact_ids.append(str(tsv_input["artifact_id"]))
    _save_output(
        state,
        session_id,
        session=session,
        worker_item_key="integrate",
        trigger_id=trigger_id,
        input_operation_id=None,
        artifact_type=ArtifactType.DIFF,
        media_type="text/x-diff",
        content=diff,
        source_artifact_ids=tuple(source_artifact_ids),
    )


def _implement(
    manifest: DemoManifest,
    state: DemoStateStore,
    session_id: UUID,
    session: Mapping[str, object],
    worker_item_key: str,
    files: Mapping[str, tuple[str | None, str]],
) -> None:
    if any(
        event.payload.get("step_key") == "implementation"
        for event in state.events_after(session_id, 0)
    ):
        return
    root = _workspace_path(session)
    for relative, (initial, content) in files.items():
        _write_workspace_file(root, relative, initial, content)
    _progress(
        state,
        session_id,
        worker_item_key,
        "implementation",
        "tool",
        "实现 WorkerItem 负责的样例步骤",
        _session_start_operation_id(session),
        files=sorted(files),
        status="completed",
    )


def _run_validation(workspace: Path, *, phone_optional: bool) -> dict[str, object]:
    completed = _run_process(
        (sys.executable, "-c", _VALIDATION_RUNNER),
        cwd=workspace,
        input_text=json.dumps({"phone_optional": phone_optional}),
    )
    if completed.returncode != 0:
        raise DemoWorkerStepError("sample_validation_failed")
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DemoWorkerStepError("sample_validation_output_invalid") from exc
    if not isinstance(rows, list):
        raise DemoWorkerStepError("sample_validation_output_invalid")
    accepted = sum(
        1 for row in rows if isinstance(row, dict) and row.get("valid") is True
    )
    rejected = sum(
        1 for row in rows if isinstance(row, dict) and row.get("valid") is False
    )
    return {
        "schema_version": 1,
        "phone_policy": "optional" if phone_optional else "required",
        "rows": rows,
        "accepted": accepted,
        "rejected": rejected,
    }


def _run_report(
    workspace: Path, results: tuple[dict[str, object], ...]
) -> dict[str, object]:
    completed = _run_process(
        (sys.executable, "-c", _REPORT_RUNNER),
        cwd=workspace,
        input_text=json.dumps({"results": results}, ensure_ascii=False),
    )
    if completed.returncode != 0:
        raise DemoWorkerStepError("sample_report_failed")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DemoWorkerStepError("sample_report_output_invalid") from exc
    if not isinstance(report, dict):
        raise DemoWorkerStepError("sample_report_output_invalid")
    return report


def _save_output(
    state: DemoStateStore,
    session_id: UUID,
    *,
    session: Mapping[str, object],
    worker_item_key: str,
    trigger_id: UUID,
    input_operation_id: UUID | None,
    artifact_type: ArtifactType,
    media_type: str,
    content: bytes,
    source_artifact_ids: tuple[str, ...],
) -> None:
    digest = hashlib.sha256(content).hexdigest()
    acceptance_refs = _session_acceptance_criteria(session)
    key = f"worker-output/{session_id}/{trigger_id}"
    operation_id = uuid5(
        NAMESPACE_URL,
        f"sidera.demo.worker-output:{session_id}:{trigger_id}",
    )
    request = {
        "session_id": str(session_id),
        "worker_item_key": worker_item_key,
        "trigger_operation_id": str(trigger_id),
        "content_hash": digest,
        "artifact_type": artifact_type.value,
        "acceptance_criterion_refs": list(acceptance_refs),
    }
    record = state.reserve(key, operation_id, request)
    result = {
        "content": content.decode("utf-8"),
        "content_hash": digest,
        "size_bytes": len(content),
        "artifact_type": artifact_type.value,
        "media_type": media_type,
        "input_operation_id": (
            str(input_operation_id) if input_operation_id is not None else None
        ),
        "source_artifact_ids": list(source_artifact_ids),
        "acceptance_criterion_refs": list(acceptance_refs),
    }
    if record.status == "pending":
        state.settle(key, operation_id, "applied", result)
    elif record.status != "applied" or record.result != result:
        raise StateIdentityConflict("worker_output_replay_mismatch")


def _read_output(
    state: DemoStateStore, session_id: UUID, trigger_id: UUID
) -> dict[str, object] | None:
    raw = state.read(f"worker-output/{session_id}/{trigger_id}")
    if raw is None:
        return None
    record = OperationRecord.from_dict(raw)
    return record.result if record.status == "applied" else None


def _session_acceptance_criteria(session: Mapping[str, object]) -> tuple[str, ...]:
    raw = session.get("acceptance_criteria", [])
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise DemoWorkerStepError("worker_acceptance_refs_invalid")
    return tuple(raw)


def _output_trigger_ids(
    state: DemoStateStore,
    session_id: UUID,
    worker_item_key: str,
    session: Mapping[str, object],
) -> tuple[UUID, ...]:
    if worker_item_key != "validate":
        return (_session_start_operation_id(session),)
    triggers: list[UUID] = []
    for event in _input_events(state, session_id, "validate"):
        try:
            operation_id = UUID(str(event.payload["input_operation_id"]))
        except (KeyError, ValueError) as exc:
            raise DemoWorkerStepError("stored_input_identity_invalid") from exc
        if _read_output(state, session_id, operation_id) is not None:
            triggers.append(operation_id)
    return tuple(triggers[-1:])


def _input_events(
    state: DemoStateStore, session_id: UUID, worker_item_key: str
) -> tuple[StoredEvent, ...]:
    return tuple(
        event
        for event in state.events_after(session_id, 0)
        if event.event_type == "input.accepted"
        and event.payload.get("worker_item_key") == worker_item_key
        and isinstance(event.payload.get("input_operation_id"), str)
        and isinstance(event.payload.get("text"), str)
    )


def _result_input(
    session: Mapping[str, object], worker_item_key: str
) -> dict[str, object]:
    raw_inputs = session.get("result_inputs")
    if not isinstance(raw_inputs, list):
        raise DemoWorkerStepError("selected_result_input_missing")
    matches: list[dict[str, object]] = []
    for raw in raw_inputs:
        if not isinstance(raw, dict):
            raise DemoWorkerStepError("selected_result_input_invalid")
        sources = raw.get("sources")
        if not isinstance(sources, list):
            continue
        if any(
            isinstance(source, dict)
            and source.get("worker_item_key") == worker_item_key
            for source in sources
        ) and _is_worker_result_body(worker_item_key, raw.get("body_text")):
            matches.append(raw)
    if len(matches) != 1:
        raise DemoWorkerStepError("selected_result_input_ambiguous")
    selected = matches[0]
    try:
        body = str(selected["body_text"])
        digest = str(selected["content_hash"])
        raw_size = selected["size_bytes"]
        UUID(str(selected["artifact_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoWorkerStepError("selected_result_input_invalid") from exc
    if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0:
        raise DemoWorkerStepError("selected_result_input_invalid")
    size = raw_size
    content = body.encode("utf-8")
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise DemoWorkerStepError("selected_result_input_integrity_mismatch")
    return selected


def _result_input_if_present(
    session: Mapping[str, object], worker_item_key: str
) -> dict[str, object] | None:
    raw_inputs = session.get("result_inputs")
    if not isinstance(raw_inputs, list):
        raise DemoWorkerStepError("selected_result_input_missing")
    has_source = any(
        isinstance(raw, dict)
        and isinstance(raw.get("sources"), list)
        and any(
            isinstance(source, dict)
            and source.get("worker_item_key") == worker_item_key
            for source in raw["sources"]
        )
        for raw in raw_inputs
    )
    return _result_input(session, worker_item_key) if has_source else None


def _is_worker_result_body(worker_item_key: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        body = json.loads(value)
    except json.JSONDecodeError:
        return worker_item_key == "review-report" and value.startswith(
            "# CSV import review"
        )
    if not isinstance(body, dict):
        return False
    if worker_item_key == "validate":
        return isinstance(body.get("rows"), list) and isinstance(
            body.get("phone_policy"), str
        )
    if worker_item_key == "report":
        return isinstance(body.get("report"), dict)
    if worker_item_key == "parse-tsv":
        return (
            body.get("format") == "tsv"
            and body.get("delimiter") == "\\t"
            and isinstance(body.get("sample_source"), str)
            and isinstance(body.get("rows"), int)
            and not isinstance(body.get("rows"), bool)
        )
    if worker_item_key in {"source-a", "source-b"}:
        return _closure_source_body(worker_item_key, value) is not None
    return False


def _closure_source_body(
    worker_item_key: str,
    value: object,
) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        body = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    expected_marker = {
        "source-a": "SIDERA_CLOSURE_SOURCE_A_V1",
        "source-b": "SIDERA_CLOSURE_SOURCE_B_V1",
    }.get(worker_item_key)
    items = body.get("items")
    if (
        expected_marker is None
        or body.get("marker") != expected_marker
        or not isinstance(items, list)
        or len(items) != 2
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("status"), str)
            for item in items
        )
    ):
        return None
    return {"marker": expected_marker, "items": items}


def _closure_items(body: Mapping[str, object]) -> list[Mapping[str, object]]:
    items = body.get("items")
    if not isinstance(items, list) or any(
        not isinstance(item, Mapping) for item in items
    ):
        raise DemoWorkerStepError("selected_closure_source_items_invalid")
    return [item for item in items if isinstance(item, Mapping)]


def _load_session(
    manifest: DemoManifest, session_id: UUID
) -> tuple[DemoStateStore, dict[str, object]]:
    matches: list[tuple[DemoStateStore, dict[str, object]]] = []
    for module_key in _WORKER_MODULE_KEYS:
        module_id = manifest.module_ids[module_key]
        root = runtime_root_for(manifest) / "worker-state" / module_id.hex
        if not (root / "operations.json").is_file():
            continue
        state = DemoStateStore(root, manifest.run_id)
        loaded = _load_session_from_store(state, session_id, missing_ok=True)
        if loaded is not None:
            matches.append((state, loaded[1]))
    if len(matches) != 1:
        raise DemoWorkerStepError("worker_session_not_found_or_ambiguous")
    state, session = matches[0]
    if session.get("workspace_id") != str(manifest.workspace_id):
        raise DemoWorkerStepError("worker_session_workspace_mismatch")
    return state, session


def _load_session_from_store(
    state: DemoStateStore,
    session_id: UUID,
    *,
    missing_ok: bool = False,
) -> tuple[DemoStateStore, dict[str, object]] | None:
    raw_index = state.read(f"session-index/{session_id}")
    if raw_index is None:
        if missing_ok:
            return None
        raise DemoWorkerStepError("worker_session_index_missing")
    index = OperationRecord.from_dict(raw_index)
    if index.status != "applied":
        raise DemoWorkerStepError("worker_session_index_incomplete")
    session_key = index.result.get("session_key")
    if not isinstance(session_key, str):
        raise DemoWorkerStepError("worker_session_index_invalid")
    raw_session = state.read(session_key)
    if raw_session is None:
        raise DemoWorkerStepError("worker_session_state_missing")
    session_record = OperationRecord.from_dict(raw_session)
    session = session_record.result
    if session.get("execution_session_id") != str(session_id):
        raise DemoWorkerStepError("worker_session_identity_mismatch")
    if not isinstance(session.get("worker_item_key"), str):
        raise DemoWorkerStepError("worker_item_identity_missing")
    return state, session


def _session_worker_key(session: Mapping[str, object]) -> str:
    worker_item_key = session.get("worker_item_key")
    if not isinstance(worker_item_key, str) or worker_item_key not in _WORKER_ITEM_KEYS:
        raise DemoWorkerStepError("worker_item_key_not_supported")
    return worker_item_key


def _session_start_operation_id(session: Mapping[str, object]) -> UUID:
    try:
        return UUID(str(session["start_operation_id"]))
    except (KeyError, ValueError) as exc:
        raise DemoWorkerStepError("worker_start_operation_missing") from exc


def _progress(
    state: DemoStateStore,
    session_id: UUID,
    worker_item_key: str,
    step_key: str,
    category: str,
    summary: str,
    base_operation_id: UUID,
    **details: object,
) -> StoredEvent:
    payload: dict[str, object] = {
        "worker_item_key": worker_item_key,
        "step_key": step_key,
        "trace_category": category,
        "trace_summary": summary,
        "simulation": True,
    }
    payload.update(details)
    operation_id = uuid5(
        base_operation_id,
        f"sidera.demo.worker-step:{session_id}:{step_key}",
    )
    return state.append_event(
        session_id,
        operation_id,
        _PROGRESS_EVENT,
        payload,
    )


def _append_event(
    state: DemoStateStore,
    session_id: UUID,
    operation_id: UUID,
    step_key: str,
    event_type: str,
    payload: dict[str, object],
) -> StoredEvent:
    return state.append_event(
        session_id,
        uuid5(
            operation_id,
            f"sidera.demo.worker-event:{session_id}:{step_key}",
        ),
        event_type,
        payload,
    )


def _write_workspace_file(
    workspace: Path,
    relative_path: str,
    initial_content: str | None,
    content: str,
) -> None:
    target = workspace / relative_path
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise DemoWorkerStepError("workspace_file_path_escape") from exc
    if target.is_symlink():
        raise DemoWorkerStepError("workspace_file_symlink_rejected")
    if target.exists():
        if not target.is_file():
            raise DemoWorkerStepError("workspace_file_not_regular")
        existing = target.read_text(encoding="utf-8")
        if existing == content:
            return
        if initial_content is None or existing != initial_content:
            raise DemoWorkerStepError("workspace_file_conflict")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _workspace_path(session: Mapping[str, object]) -> Path:
    raw = session.get("workspace_path")
    if not isinstance(raw, str):
        raise DemoWorkerStepError("worker_workspace_path_missing")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise DemoWorkerStepError("worker_workspace_unavailable")
    return path.resolve(strict=True)


def _assert_workspace(manifest: DemoManifest, session: Mapping[str, object]) -> None:
    record = session.get("workspace_manifest")
    if not isinstance(record, dict):
        raise DemoWorkerStepError("worker_workspace_manifest_missing")
    if (
        record.get("workspace_id") != str(manifest.workspace_id)
        or record.get("repository_ref") not in _allowed_repository_refs(manifest)
        or record.get("base_revision") != manifest.base_commit
        or record.get("task_id") != session.get("task_id")
        or record.get("execution_id") != session.get("execution_id")
        or record.get("fencing_token") != session.get("fencing_token")
    ):
        raise DemoWorkerStepError("worker_workspace_manifest_mismatch")
    workspace = _workspace_path(session)
    repository = manifest.repository.resolve(strict=True)
    owner_marker = repository / ".sidera-interview-demo-owner"
    git_directory = repository / ".git"
    if (
        owner_marker.is_symlink()
        or not owner_marker.is_file()
        or owner_marker.read_text(encoding="utf-8") != OWNER_MARKER
        or git_directory.is_symlink()
        or not git_directory.is_dir()
        or (git_directory / "config").is_symlink()
    ):
        raise DemoWorkerStepError("sample_repository_not_owned")
    if workspace == repository:
        raise DemoWorkerStepError("sample_repository_invalid")
    checkout = record.get("checkout")
    if (
        not isinstance(checkout, dict)
        or checkout.get("repository_ref") != record.get("repository_ref")
        or checkout.get("repository_ref") not in _allowed_repository_refs(manifest)
        or checkout.get("base_revision") != manifest.base_commit
        or checkout.get("current_head") != manifest.base_commit
    ):
        raise DemoWorkerStepError("worker_workspace_base_mismatch")
    top_level = (
        _run_git(("rev-parse", "--show-toplevel"), cwd=workspace)
        .stdout.decode()
        .strip()
    )
    if Path(top_level).resolve(strict=True) != workspace:
        raise DemoWorkerStepError("worker_workspace_root_mismatch")
    head = _run_git(("rev-parse", "HEAD"), cwd=workspace).stdout.decode().strip()
    expected_runtime_head = session.get("workspace_runtime_head")
    if not isinstance(expected_runtime_head, str) or _GIT_OID_PATTERN.fullmatch(
        expected_runtime_head.strip()
    ) is None:
        expected_runtime_head = manifest.base_commit
    if head.lower() != expected_runtime_head.strip().lower():
        raise DemoWorkerStepError("worker_workspace_head_mismatch")
    if _run_git(("remote",), cwd=repository).stdout.strip():
        raise DemoWorkerStepError("sample_repository_remote_rejected")
    source_common = (
        _run_git(("rev-parse", "--git-common-dir"), cwd=repository)
        .stdout.decode()
        .strip()
    )
    workspace_common = (
        _run_git(("rev-parse", "--git-common-dir"), cwd=workspace)
        .stdout.decode()
        .strip()
    )
    source_common_path = Path(source_common)
    workspace_common_path = Path(workspace_common)
    if not source_common_path.is_absolute():
        source_common_path = repository / source_common_path
    if not workspace_common_path.is_absolute():
        workspace_common_path = workspace / workspace_common_path
    if source_common_path.resolve(strict=True) != workspace_common_path.resolve(
        strict=True
    ):
        raise DemoWorkerStepError("worker_workspace_repository_mismatch")


def _run_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    inherited = os.environ
    env = {
        key: inherited[key]
        for key in ("PATH", "TMPDIR", "LANG", "LC_ALL")
        if key in inherited
    }
    env["PYTHONNOUSERSITE"] = "1"
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise DemoWorkerStepError("sample_process_timed_out") from exc


def _run_git(
    arguments: tuple[str, ...], *, cwd: Path, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise DemoWorkerStepError("sample_git_timed_out") from exc
    if completed.returncode != 0:
        raise DemoWorkerStepError("sample_git_command_failed")
    return completed


def _allowed_repository_refs(manifest: DemoManifest) -> frozenset[str]:
    repository = manifest.repository.resolve(strict=False)
    return frozenset(
        {
            f"repo://interview/{manifest.run_id}",
            str(repository),
            opaque_repository_ref(repository),
        }
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _blocked_delivery(
    operation_id: UUID,
    session_id: UUID,
    failure_code: str,
    *,
    accepted_sequence: int | None = None,
) -> DeliveryResult:
    return DeliveryResult(
        operation_id=operation_id,
        outcome=DeliveryOutcome.BLOCKED,
        accepted_sequence=accepted_sequence,
        failure_code=failure_code,
        execution_session_id=session_id,
    )


def _delivery_from_input_record(
    record: OperationRecord, session_id: UUID
) -> DeliveryResult:
    try:
        outcome = DeliveryOutcome(str(record.result["outcome"]))
        sequence_value = record.result.get("accepted_sequence")
        if sequence_value is not None and (
            not isinstance(sequence_value, int)
            or isinstance(sequence_value, bool)
            or sequence_value < 1
        ):
            raise DemoWorkerStepError("persisted_input_result_invalid")
        sequence = sequence_value
        failure_code = record.result.get("failure_code")
        if failure_code is not None:
            failure_code = str(failure_code)
    except (KeyError, TypeError, ValueError) as exc:
        raise DemoWorkerStepError("persisted_input_result_invalid") from exc
    return DeliveryResult(
        operation_id=record.operation_id,
        outcome=outcome,
        accepted_sequence=sequence,
        failure_code=failure_code,
        execution_session_id=session_id,
    )
