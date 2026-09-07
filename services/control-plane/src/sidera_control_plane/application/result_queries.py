from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar
from uuid import UUID

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.result_acceptance import (
    build_acceptance_basis_hash,
)
from sidera_control_plane.domain.code_results import (
    CodeResultSnapshotV1,
    IntegrationCandidateV1,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.result_operations import (
    ResultOperationStatus,
    ReworkRecordV1,
    TaskResultHead,
)
from sidera_control_plane.domain.result_verification import (
    CriterionDecisionV1,
    CriterionObservedStatus,
    TaskAcceptanceDecisionV1,
    VerificationResultV1,
    VerificationStatus,
    evaluate_criteria,
)


class ResultCursorInvalid(ValidationError):
    code = "result_cursor_invalid"


@dataclass(frozen=True, slots=True)
class ResultCursor:
    resource: str
    workspace_id: UUID
    root_task_id: UUID
    candidate_id: UUID | None
    created_at: datetime
    item_id: UUID


@dataclass(frozen=True, slots=True)
class ResultPageMarker:
    created_at: datetime
    item_id: UUID


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ResultPage[T]:
    items: tuple[T, ...]
    next_marker: ResultPageMarker | None


@dataclass(frozen=True, slots=True)
class ResultTaskFact:
    task_id: UUID
    workspace_id: UUID
    title: str
    status: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ResultOperationFact:
    operation_id: UUID
    operation_kind: str
    status: str
    attempt: int
    failure_code: str | None
    receipt_ref: str | None
    version: int
    candidate_id: UUID | None


@dataclass(frozen=True, slots=True)
class ResultApplyFact:
    operation_id: UUID
    candidate_id: UUID
    status: str
    attempt: int
    target_ref: str
    accepted_commit: str
    receipt_ref: str | None
    failure_code: str | None
    version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResultArtifactFact:
    artifact_id: UUID
    artifact_type: str
    content_hash: str
    size_bytes: int
    media_type: str
    content_available: bool


@dataclass(frozen=True, slots=True)
class ResultRootSnapshot:
    task: ResultTaskFact
    head: TaskResultHead | None
    plan: TaskPlanSnapshot | None
    current_candidate: IntegrationCandidateV1 | None
    current_sources: tuple[CodeResultSnapshotV1, ...]
    current_verifications: tuple[VerificationResultV1, ...]
    current_rework: tuple[ReworkRecordV1, ...]
    acceptance: TaskAcceptanceDecisionV1 | None
    active_operation: ResultOperationFact | None
    candidate_history: ResultPage[IntegrationCandidateV1]
    artifacts: tuple[ResultArtifactFact, ...]
    last_change_cursor: int
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ResultDetailSnapshot:
    task: ResultTaskFact
    head: TaskResultHead | None
    plan: TaskPlanSnapshot | None
    candidate: IntegrationCandidateV1
    source_results: tuple[CodeResultSnapshotV1, ...]
    verifications: ResultPage[VerificationResultV1]
    rework: ResultPage[ReworkRecordV1]
    acceptance: TaskAcceptanceDecisionV1 | None
    apply_operations: tuple[ResultApplyFact, ...]
    artifacts: tuple[ResultArtifactFact, ...]
    last_change_cursor: int
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ResultAcceptanceBasisSnapshot:
    candidate: IntegrationCandidateV1
    plan: TaskPlanSnapshot | None
    verifications: tuple[VerificationResultV1, ...]
    result_version: int


class ResultQueryStore(Protocol):
    def load_root(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        cursor: ResultCursor | None,
        limit: int,
    ) -> ResultRootSnapshot | None:
        raise NotImplementedError

    def load_detail(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        verification_cursor: ResultCursor | None,
        rework_cursor: ResultCursor | None,
        limit: int,
    ) -> ResultDetailSnapshot | None:
        raise NotImplementedError

    def list_verifications(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        cursor: ResultCursor | None,
        limit: int,
    ) -> ResultPage[VerificationResultV1] | None:
        raise NotImplementedError

    def list_rework(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        cursor: ResultCursor | None,
        limit: int,
    ) -> ResultPage[ReworkRecordV1] | None:
        raise NotImplementedError

    def load_acceptance_basis(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
    ) -> ResultAcceptanceBasisSnapshot | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ResultArtifactView:
    artifact_id: UUID
    artifact_type: str
    content_hash: str
    size_bytes: int
    media_type: str
    content_available: bool


@dataclass(frozen=True, slots=True)
class ResultSourceView:
    snapshot_id: UUID
    worker_item_id: UUID
    request_id: UUID
    execution_id: UUID
    result_commit: str
    result_tree: str
    content_hash: str
    artifact_ids: tuple[UUID, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResultCandidateView:
    candidate_id: UUID
    candidate_hash: str
    plan_snapshot_id: UUID
    plan_content_hash: str
    integration_execution_id: UUID
    base_commit: str
    result_commit: str | None
    result_tree: str | None
    source_result_ids: tuple[UUID, ...]
    code_result_id: UUID
    previous_candidate_id: UUID | None
    created_at: datetime
    is_current: bool
    artifacts: tuple[ResultArtifactView, ...]


@dataclass(frozen=True, slots=True)
class ResultCriterionView:
    criterion_key: str
    text: str
    mode: str
    check_keys: tuple[str, ...]
    observed_status: str
    verification_status: str
    verification_result_ids: tuple[UUID, ...]
    decision: str | None
    decision_reason: str | None


@dataclass(frozen=True, slots=True)
class ResultAcceptanceView:
    acceptance_id: UUID
    candidate_id: UUID
    candidate_hash: str
    plan_content_hash: str
    acceptance_basis_hash: str
    result_version: int
    criteria: tuple[CriterionDecisionV1, ...]
    actor_type: str
    actor_id: str
    command_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResultApplyPolicyView:
    apply_on_accept: bool
    mode: str
    target_ref: str | None


@dataclass(frozen=True, slots=True)
class ResultAcceptanceBasisView:
    candidate_id: UUID
    candidate_hash: str
    expected_result_version: int
    acceptance_basis_hash: str


@dataclass(frozen=True, slots=True)
class ResultOperationView:
    operation_id: UUID
    operation_kind: str
    status: str
    attempt: int
    failure_code: str | None
    receipt_ref: str | None
    version: int
    candidate_id: UUID | None


@dataclass(frozen=True, slots=True)
class ResultActionView:
    action: str
    command_type: str
    target_type: str
    target_id: UUID
    expected_version: int
    expected_result_version: int
    expected_fingerprint: str | None
    candidate_id: UUID | None
    candidate_hash: str | None
    plan_content_hash: str | None
    requires_reason: bool


@dataclass(frozen=True, slots=True)
class ResultVerificationView:
    verification_id: UUID
    candidate_id: UUID
    execution_id: UUID
    check_key: str
    check_version: int
    environment_ref: str
    actual_commit: str
    actual_tree: str
    operation_id: UUID
    attempt: int
    status: str
    exit_code: int | None
    started_at: datetime | None
    finished_at: datetime | None
    artifact_ids: tuple[UUID, ...]
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class ResultReworkView:
    rework_id: UUID
    candidate_id: UUID
    source_issue_keys: tuple[str, ...]
    target_worker_item_ids: tuple[UUID, ...]
    impact_worker_item_ids: tuple[UUID, ...]
    reason: str
    mode: str
    round_number: int | None
    request_ids: tuple[UUID, ...]
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResultApplyView:
    operation_id: UUID
    candidate_id: UUID
    status: str
    attempt: int
    target_ref: str
    accepted_commit: str
    receipt_ref: str | None
    failure_code: str | None
    version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResultHistoryView[T]:
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TaskResultOverview:
    workspace_id: UUID
    root_task_id: UUID
    task_title: str
    task_status: str
    task_version: int
    result_version: int | None
    result_stage: str
    current_candidate: ResultCandidateView | None
    acceptance: ResultAcceptanceView | None
    apply_policy: ResultApplyPolicyView | None
    active_operation: ResultOperationView | None
    criteria: tuple[ResultCriterionView, ...]
    blocking_reasons: tuple[str, ...]
    available_actions: tuple[ResultActionView, ...]
    candidate_history: ResultHistoryView[ResultCandidateView]
    last_change_cursor: int
    as_of: datetime


@dataclass(frozen=True, slots=True)
class TaskResultDetail:
    workspace_id: UUID
    root_task_id: UUID
    candidate: ResultCandidateView
    source_results: tuple[ResultSourceView, ...]
    verifications: ResultHistoryView[ResultVerificationView]
    rework: ResultHistoryView[ResultReworkView]
    acceptance: ResultAcceptanceView | None
    apply_operations: tuple[ResultApplyView, ...]
    artifacts: tuple[ResultArtifactView, ...]
    last_change_cursor: int
    as_of: datetime


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class ResultQueryService:
    def __init__(self, store: ResultQueryStore) -> None:
        self._store = store

    def get_overview(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> TaskResultOverview:
        _validate_limit(limit)
        decoded = _decode_cursor(
            cursor,
            resource="candidate_history",
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            candidate_id=None,
        )
        snapshot = self._store.load_root(
            workspace_id,
            root_task_id,
            cursor=decoded,
            limit=limit,
        )
        if snapshot is None:
            raise NotFound("task result not found")
        return _overview(snapshot, workspace_id, root_task_id)

    def get_detail(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        verification_cursor: str | None,
        rework_cursor: str | None,
        limit: int,
    ) -> TaskResultDetail:
        _validate_limit(limit)
        verification = _decode_cursor(
            verification_cursor,
            resource="verifications",
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            candidate_id=candidate_id,
        )
        rework = _decode_cursor(
            rework_cursor,
            resource="rework",
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            candidate_id=candidate_id,
        )
        snapshot = self._store.load_detail(
            workspace_id,
            root_task_id,
            candidate_id,
            verification_cursor=verification,
            rework_cursor=rework,
            limit=limit,
        )
        if snapshot is None:
            raise NotFound("task result candidate not found")
        return _detail(snapshot, workspace_id, root_task_id)

    def get_verifications(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> ResultHistoryView[ResultVerificationView]:
        _validate_limit(limit)
        decoded = _decode_cursor(
            cursor,
            resource="verifications",
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            candidate_id=candidate_id,
        )
        page = self._store.list_verifications(
            workspace_id,
            root_task_id,
            candidate_id,
            cursor=decoded,
            limit=limit,
        )
        if page is None:
            raise NotFound("task result candidate not found")
        return ResultHistoryView(
            items=tuple(_verification(item) for item in page.items),
            next_cursor=_next_cursor(
                page.next_marker,
                resource="verifications",
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                candidate_id=candidate_id,
            ),
        )

    def get_rework(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> ResultHistoryView[ResultReworkView]:
        _validate_limit(limit)
        decoded = _decode_cursor(
            cursor,
            resource="rework",
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            candidate_id=candidate_id,
        )
        page = self._store.list_rework(
            workspace_id,
            root_task_id,
            candidate_id,
            cursor=decoded,
            limit=limit,
        )
        if page is None:
            raise NotFound("task result candidate not found")
        return ResultHistoryView(
            items=tuple(_rework(item) for item in page.items),
            next_cursor=_next_cursor(
                page.next_marker,
                resource="rework",
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                candidate_id=candidate_id,
            ),
        )

    def get_acceptance_basis(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        candidate_id: UUID,
        *,
        criteria: Sequence[CriterionDecisionV1],
    ) -> ResultAcceptanceBasisView:
        snapshot = self._store.load_acceptance_basis(
            workspace_id,
            root_task_id,
            candidate_id,
        )
        if snapshot is None:
            raise NotFound("current task result candidate not found")
        plan = snapshot.plan
        spec = None if plan is None else plan.integration_spec
        if not isinstance(spec, IntegrationSpecV1):
            raise ValidationError("result acceptance basis is unavailable")
        if not criteria:
            raise ValidationError("acceptance criteria must not be empty")
        criterion_keys = {criterion.key for criterion in spec.criteria}
        decision_keys = {decision.criterion_key for decision in criteria}
        missing = sorted(criterion_keys - decision_keys)
        if missing:
            raise ValidationError(
                f"acceptance criteria missing: {', '.join(missing)}"
            )
        unknown = sorted(decision_keys - criterion_keys)
        if unknown:
            raise ValidationError(
                f"acceptance criteria unknown: {', '.join(unknown)}"
            )
        evaluate_criteria(spec.criteria, snapshot.verifications, criteria)
        return ResultAcceptanceBasisView(
            candidate_id=snapshot.candidate.id,
            candidate_hash=snapshot.candidate.content_hash,
            expected_result_version=snapshot.result_version,
            acceptance_basis_hash=build_acceptance_basis_hash(
                snapshot.candidate,
                snapshot.verifications,
                criteria,
                result_version=snapshot.result_version,
                integration_spec=spec,
            ),
        )


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("result query limit must be between 1 and 100")


def _decode_cursor(
    value: str | None,
    *,
    resource: str,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID | None,
) -> ResultCursor | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ResultCursorInvalid("result cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError
        if set(payload) != {
            "resource",
            "workspace_id",
            "root_task_id",
            "candidate_id",
            "created_at",
            "item_id",
        }:
            raise ValueError
        parsed = ResultCursor(
            resource=str(payload["resource"]),
            workspace_id=UUID(str(payload["workspace_id"])),
            root_task_id=UUID(str(payload["root_task_id"])),
            candidate_id=(
                None
                if payload["candidate_id"] is None
                else UUID(str(payload["candidate_id"]))
            ),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            item_id=UUID(str(payload["item_id"])),
        )
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError):
        raise ResultCursorInvalid("result cursor is invalid") from None
    if (
        parsed.resource != resource
        or parsed.workspace_id != workspace_id
        or parsed.root_task_id != root_task_id
        or parsed.candidate_id != candidate_id
        or parsed.created_at.tzinfo is None
        or parsed.created_at.utcoffset() is None
    ):
        raise ResultCursorInvalid("result cursor is invalid")
    return ResultCursor(
        resource=parsed.resource,
        workspace_id=parsed.workspace_id,
        root_task_id=parsed.root_task_id,
        candidate_id=parsed.candidate_id,
        created_at=parsed.created_at.astimezone(UTC),
        item_id=parsed.item_id,
    )


def _encode_cursor(
    marker: ResultPageMarker,
    *,
    resource: str,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID | None,
) -> str:
    payload = {
        "resource": resource,
        "workspace_id": str(workspace_id),
        "root_task_id": str(root_task_id),
        "candidate_id": None if candidate_id is None else str(candidate_id),
        "created_at": _as_utc(marker.created_at).isoformat(),
        "item_id": str(marker.item_id),
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _next_cursor(
    marker: ResultPageMarker | None,
    *,
    resource: str,
    workspace_id: UUID,
    root_task_id: UUID,
    candidate_id: UUID | None,
) -> str | None:
    if marker is None:
        return None
    return _encode_cursor(
        marker,
        resource=resource,
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        candidate_id=candidate_id,
    )


def _overview(
    snapshot: ResultRootSnapshot, workspace_id: UUID, root_task_id: UUID
) -> TaskResultOverview:
    head = snapshot.head
    current = snapshot.current_candidate
    acceptance = _acceptance(snapshot.acceptance)
    active_operation = _operation(snapshot.active_operation)
    criteria = _criteria(snapshot.plan, snapshot.current_verifications, snapshot.acceptance)
    stage = _stage(snapshot, criteria)
    history = ResultHistoryView(
        items=tuple(
            _candidate(
                item,
                is_current=head is not None and item.id == head.current_candidate_id,
                sources=(
                    snapshot.current_sources
                    if current is not None and item.id == current.id
                    else ()
                ),
                artifacts=(
                    snapshot.artifacts
                    if current is not None and item.id == current.id
                    else ()
                ),
            )
            for item in snapshot.candidate_history.items
        ),
        next_cursor=_next_cursor(
            snapshot.candidate_history.next_marker,
            resource="candidate_history",
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            candidate_id=None,
        ),
    )
    actions = _actions(snapshot, stage)
    return TaskResultOverview(
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        task_title=snapshot.task.title,
        task_status=snapshot.task.status,
        task_version=snapshot.task.version,
        result_version=None if head is None else head.result_version,
        result_stage=stage,
        current_candidate=(
            None
            if current is None
            else _candidate(
                current,
                is_current=True,
                sources=snapshot.current_sources,
                artifacts=snapshot.artifacts,
            )
        ),
        acceptance=acceptance,
        apply_policy=_apply_policy(snapshot.plan),
        active_operation=active_operation,
        criteria=criteria,
        blocking_reasons=_blocking_reasons(snapshot),
        available_actions=actions,
        candidate_history=history,
        last_change_cursor=snapshot.last_change_cursor,
        as_of=_as_utc(snapshot.as_of),
    )


def _detail(
    snapshot: ResultDetailSnapshot, workspace_id: UUID, root_task_id: UUID
) -> TaskResultDetail:
    current_id = None if snapshot.head is None else snapshot.head.current_candidate_id
    candidate = _candidate(
        snapshot.candidate,
        is_current=snapshot.candidate.id == current_id,
        sources=snapshot.source_results,
        artifacts=snapshot.artifacts,
    )
    return TaskResultDetail(
        workspace_id=workspace_id,
        root_task_id=root_task_id,
        candidate=candidate,
        source_results=tuple(_source(item) for item in snapshot.source_results),
        verifications=ResultHistoryView(
            items=tuple(_verification(item) for item in snapshot.verifications.items),
            next_cursor=_next_cursor(
                snapshot.verifications.next_marker,
                resource="verifications",
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                candidate_id=snapshot.candidate.id,
            ),
        ),
        rework=ResultHistoryView(
            items=tuple(_rework(item) for item in snapshot.rework.items),
            next_cursor=_next_cursor(
                snapshot.rework.next_marker,
                resource="rework",
                workspace_id=workspace_id,
                root_task_id=root_task_id,
                candidate_id=snapshot.candidate.id,
            ),
        ),
        acceptance=_acceptance(snapshot.acceptance),
        apply_operations=tuple(_apply(item) for item in snapshot.apply_operations),
        artifacts=tuple(_artifact(item) for item in snapshot.artifacts),
        last_change_cursor=snapshot.last_change_cursor,
        as_of=_as_utc(snapshot.as_of),
    )


def _candidate(
    candidate: IntegrationCandidateV1,
    *,
    is_current: bool,
    sources: Sequence[CodeResultSnapshotV1],
    artifacts: Sequence[ResultArtifactFact],
) -> ResultCandidateView:
    code_result = next(
        (item for item in sources if item.id == candidate.code_result_id), None
    )
    return ResultCandidateView(
        candidate_id=candidate.id,
        candidate_hash=candidate.content_hash,
        plan_snapshot_id=candidate.plan_snapshot_id,
        plan_content_hash=candidate.plan_content_hash,
        integration_execution_id=candidate.integration_execution_id,
        base_commit=candidate.base_commit,
        result_commit=None if code_result is None else code_result.result_commit,
        result_tree=None if code_result is None else code_result.result_tree,
        source_result_ids=candidate.source_result_ids,
        code_result_id=candidate.code_result_id,
        previous_candidate_id=candidate.previous_candidate_id,
        created_at=_as_utc(candidate.created_at),
        is_current=is_current,
        artifacts=tuple(_artifact(item) for item in artifacts),
    )


def _source(snapshot: CodeResultSnapshotV1) -> ResultSourceView:
    return ResultSourceView(
        snapshot_id=snapshot.id,
        worker_item_id=snapshot.worker_item_id,
        request_id=snapshot.request_id,
        execution_id=snapshot.execution_id,
        result_commit=snapshot.result_commit,
        result_tree=snapshot.result_tree,
        content_hash=snapshot.content_hash,
        artifact_ids=snapshot.artifact_ids,
        created_at=_as_utc(snapshot.created_at),
    )


def _criterion_status(
    check_keys: Sequence[str], results: Sequence[VerificationResultV1]
) -> tuple[str, tuple[UUID, ...]]:
    latest: dict[str, VerificationResultV1] = {}
    for result in results:
        if result.check_key in check_keys and (
            result.check_key not in latest or result.attempt > latest[result.check_key].attempt
        ):
            latest[result.check_key] = result
    matching = tuple(latest[key] for key in check_keys if key in latest)
    if not matching:
        return "not_run", ()
    status_priority = (
        VerificationStatus.UNKNOWN,
        VerificationStatus.INTERRUPTED,
        VerificationStatus.BLOCKED,
        VerificationStatus.FAILED,
        VerificationStatus.NOT_RUN,
    )
    for status in status_priority:
        if any(item.status is status for item in matching):
            return status.value, tuple(item.id for item in matching)
    return VerificationStatus.PASSED.value, tuple(item.id for item in matching)


def _criteria(
    plan: TaskPlanSnapshot | None,
    verifications: Sequence[VerificationResultV1],
    acceptance: TaskAcceptanceDecisionV1 | None,
) -> tuple[ResultCriterionView, ...]:
    if plan is None or plan.integration_spec is None:
        return ()
    decisions = {} if acceptance is None else {item.criterion_key: item for item in acceptance.criteria}
    result: list[ResultCriterionView] = []
    for criterion in plan.integration_spec.criteria:
        verification_status, result_ids = _criterion_status(
            criterion.check_keys, verifications
        )
        if criterion.mode.value == "manual":
            observed = CriterionObservedStatus.PENDING.value
        elif verification_status == VerificationStatus.PASSED.value:
            observed = CriterionObservedStatus.PASSED.value
        else:
            observed = (
                CriterionObservedStatus.FAILED.value
                if verification_status
                in {
                    VerificationStatus.FAILED.value,
                    VerificationStatus.UNKNOWN.value,
                    VerificationStatus.INTERRUPTED.value,
                    VerificationStatus.BLOCKED.value,
                }
                else CriterionObservedStatus.PENDING.value
            )
        decision = decisions.get(criterion.key)
        result.append(
            ResultCriterionView(
                criterion_key=criterion.key,
                text=criterion.text,
                mode=criterion.mode.value,
                check_keys=criterion.check_keys,
                observed_status=observed,
                verification_status=verification_status,
                verification_result_ids=result_ids,
                decision=None if decision is None else decision.decision.value,
                decision_reason=None if decision is None else decision.reason,
            )
        )
    return tuple(result)


def _stage(snapshot: ResultRootSnapshot, criteria: Sequence[ResultCriterionView]) -> str:
    if snapshot.head is None:
        return "legacy"
    operation = snapshot.active_operation
    if operation is not None:
        if operation.status == ResultOperationStatus.UNKNOWN.value:
            return "attention"
        if operation.status in {
            ResultOperationStatus.PENDING.value,
            ResultOperationStatus.RUNNING.value,
        }:
            return "applying" if operation.operation_kind == "apply" else "verifying"
        if operation.status in {
            ResultOperationStatus.BLOCKED.value,
            ResultOperationStatus.FAILED.value,
        }:
            return "attention"
    if snapshot.acceptance is not None:
        return "completed" if snapshot.task.status == "succeeded" else "finalizing"
    if any(item.status in {"requested", "running"} for item in snapshot.current_rework):
        return "reworking"
    if snapshot.current_candidate is not None:
        if any(item.verification_status in {"unknown", "interrupted", "blocked"} for item in criteria):
            return "attention"
        return "awaiting_acceptance"
    return "waiting"


def _blocking_reasons(snapshot: ResultRootSnapshot) -> tuple[str, ...]:
    reasons: list[str] = []
    operation = snapshot.active_operation
    if operation is not None and operation.status in {"unknown", "blocked", "failed"}:
        reasons.append(operation.failure_code or f"result_operation_{operation.status}")
    for result in snapshot.current_verifications:
        if result.status in {
            VerificationStatus.FAILED,
            VerificationStatus.UNKNOWN,
            VerificationStatus.INTERRUPTED,
            VerificationStatus.BLOCKED,
        }:
            reasons.append(result.failure_code or f"verification_{result.status.value}")
    return tuple(dict.fromkeys(reasons))


def _actions(snapshot: ResultRootSnapshot, stage: str) -> tuple[ResultActionView, ...]:
    if snapshot.head is None or snapshot.current_candidate is None:
        return ()
    candidate = snapshot.current_candidate
    head = snapshot.head
    if snapshot.active_operation is not None:
        if snapshot.active_operation.status == ResultOperationStatus.UNKNOWN.value:
            return (
                ResultActionView(
                    action="inspect_result_operation",
                    command_type="InspectResultOperation",
                    target_type="result_operation",
                    target_id=snapshot.active_operation.operation_id,
                    expected_version=snapshot.active_operation.version,
                    expected_result_version=head.result_version,
                    expected_fingerprint=candidate.content_hash,
                    candidate_id=candidate.id,
                    candidate_hash=candidate.content_hash,
                    plan_content_hash=candidate.plan_content_hash,
                    requires_reason=True,
                ),
            )
        return ()
    if snapshot.acceptance is not None or stage == "completed":
        return ()
    return (
        ResultActionView(
            action="accept_candidate",
            command_type="CompleteTask",
            target_type="task",
            target_id=snapshot.task.task_id,
            expected_version=snapshot.task.version,
            expected_result_version=head.result_version,
            expected_fingerprint=candidate.content_hash,
            candidate_id=candidate.id,
            candidate_hash=candidate.content_hash,
            plan_content_hash=candidate.plan_content_hash,
            requires_reason=False,
        ),
        ResultActionView(
            action="request_rework",
            command_type="RequestTaskRework",
            target_type="task",
            target_id=snapshot.task.task_id,
            expected_version=head.result_version,
            expected_result_version=head.result_version,
            expected_fingerprint=candidate.content_hash,
            candidate_id=candidate.id,
            candidate_hash=candidate.content_hash,
            plan_content_hash=candidate.plan_content_hash,
            requires_reason=True,
        ),
    )


def _acceptance(value: TaskAcceptanceDecisionV1 | None) -> ResultAcceptanceView | None:
    if value is None:
        return None
    return ResultAcceptanceView(
        acceptance_id=value.id,
        candidate_id=value.candidate_id,
        candidate_hash=value.candidate_hash,
        plan_content_hash=value.plan_content_hash,
        acceptance_basis_hash=value.acceptance_basis_hash,
        result_version=value.result_version,
        criteria=value.criteria,
        actor_type=value.actor_type,
        actor_id=value.actor_id,
        command_id=value.command_id,
        created_at=_as_utc(value.created_at),
    )


def _apply_policy(plan: TaskPlanSnapshot | None) -> ResultApplyPolicyView | None:
    if plan is None or not isinstance(plan.integration_spec, IntegrationSpecV1):
        return None
    policy = plan.integration_spec.apply_policy
    return ResultApplyPolicyView(
        apply_on_accept=policy.apply_on_accept,
        mode=policy.mode.value,
        target_ref=policy.target_ref,
    )


def _operation(value: ResultOperationFact | None) -> ResultOperationView | None:
    if value is None:
        return None
    return ResultOperationView(
        operation_id=value.operation_id,
        operation_kind=value.operation_kind,
        status=value.status,
        attempt=value.attempt,
        failure_code=value.failure_code,
        receipt_ref=value.receipt_ref,
        version=value.version,
        candidate_id=value.candidate_id,
    )


def _artifact(value: ResultArtifactFact) -> ResultArtifactView:
    return ResultArtifactView(
        artifact_id=value.artifact_id,
        artifact_type=value.artifact_type,
        content_hash=value.content_hash,
        size_bytes=value.size_bytes,
        media_type=value.media_type,
        content_available=value.content_available,
    )


def _verification(value: VerificationResultV1) -> ResultVerificationView:
    return ResultVerificationView(
        verification_id=value.id,
        candidate_id=value.candidate_id,
        execution_id=value.execution_id,
        check_key=value.check_key,
        check_version=value.check_version,
        environment_ref=value.environment_ref,
        actual_commit=value.actual_commit,
        actual_tree=value.actual_tree,
        operation_id=value.operation_id,
        attempt=value.attempt,
        status=value.status.value,
        exit_code=value.exit_code,
        started_at=None if value.started_at is None else _as_utc(value.started_at),
        finished_at=None if value.finished_at is None else _as_utc(value.finished_at),
        artifact_ids=value.artifact_ids,
        failure_code=value.failure_code,
    )


def _rework(value: ReworkRecordV1) -> ResultReworkView:
    return ResultReworkView(
        rework_id=value.id,
        candidate_id=value.candidate_id,
        source_issue_keys=value.source_issue_keys,
        target_worker_item_ids=value.target_worker_item_ids,
        impact_worker_item_ids=value.impact_worker_item_ids,
        reason=value.reason,
        mode=value.mode.value,
        round_number=value.round_number,
        request_ids=value.request_ids,
        status=value.status.value,
        created_at=_as_utc(value.created_at),
    )


def _apply(value: ResultApplyFact) -> ResultApplyView:
    return ResultApplyView(
        operation_id=value.operation_id,
        candidate_id=value.candidate_id,
        status=value.status,
        attempt=value.attempt,
        target_ref=value.target_ref,
        accepted_commit=value.accepted_commit,
        receipt_ref=value.receipt_ref,
        failure_code=value.failure_code,
        version=value.version,
        created_at=_as_utc(value.created_at),
    )


__all__ = [
    "ResultAcceptanceBasisSnapshot",
    "ResultAcceptanceBasisView",
    "ResultActionView",
    "ResultApplyFact",
    "ResultApplyPolicyView",
    "ResultApplyView",
    "ResultArtifactFact",
    "ResultArtifactView",
    "ResultCandidateView",
    "ResultCursor",
    "ResultCursorInvalid",
    "ResultDetailSnapshot",
    "ResultHistoryView",
    "ResultOperationFact",
    "ResultOperationView",
    "ResultPage",
    "ResultPageMarker",
    "ResultQueryService",
    "ResultQueryStore",
    "ResultReworkView",
    "ResultRootSnapshot",
    "ResultSourceView",
    "ResultTaskFact",
    "ResultVerificationView",
    "TaskResultDetail",
    "TaskResultOverview",
]
