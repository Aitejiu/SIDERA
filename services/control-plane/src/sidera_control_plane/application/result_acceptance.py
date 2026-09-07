from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from re import fullmatch
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.task_commands import TaskCommandHandler
from sidera_control_plane.domain.code_results import IntegrationCandidateV1
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.plan_revisions import plan_snapshot_content_hash
from sidera_control_plane.domain.result_integration import IntegrationSpecV1
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.domain.result_verification import (
    CriterionDecisionType,
    CriterionDecisionV1,
    CriterionObservedStatus,
    TaskAcceptanceDecisionV1,
    VerificationResultV1,
    evaluate_criteria,
)

_ACCEPTANCE_NAMESPACE = uuid5(NAMESPACE_URL, "sidera:task-acceptance:v1")
_ACCEPTANCE_PAYLOAD_FIELDS = frozenset(
    {
        "candidate_id",
        "candidate_hash",
        "acceptance_basis_hash",
        "expected_result_version",
        "criteria",
    }
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ResultAcceptanceService:
    """Record an exact user decision for the current result candidate."""

    command_types = frozenset({"CompleteTask"})

    def __init__(
        self,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        *,
        local_actor_type: str = "local",
        local_actor_id: str = "operator",
    ) -> None:
        self._clock = clock
        self._local_actor_type = local_actor_type
        self._local_actor_id = local_actor_id

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.command_type != "CompleteTask":
            raise ResultError("acceptance_command_unsupported", {})
        if envelope.target_id is None:
            raise ResultError("acceptance_target_required", {})

        head = uow.results.get_head(
            envelope.workspace_id, envelope.target_id, for_update=True
        )
        if head is None:
            # Result-enabled roots use this service. Legacy tasks retain the original
            # waiting -> succeeded command contract until they opt into result heads.
            return TaskCommandHandler._mutate_task(uow, envelope)
        return self.accept(uow, envelope)

    def accept(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        if envelope.target_id is None or envelope.expected_version is None:
            raise ResultError("acceptance_target_required", {})
        if (
            envelope.actor_type != self._local_actor_type
            or envelope.actor_id != self._local_actor_id
        ):
            raise ResultError(
                "acceptance_actor_forbidden",
                {"actor_type": self._local_actor_type},
            )

        task = uow.tasks.get(envelope.target_id, envelope.workspace_id, for_update=True)
        if task is None:
            raise ResultError("acceptance_task_not_found", {})
        if task.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, task.version)

        payload = _require_acceptance_contract(envelope.payload)
        head = uow.results.get_head(
            envelope.workspace_id, envelope.target_id, for_update=True
        )
        if head is None:
            raise ResultError("acceptance_result_head_not_found", {})
        candidate_id = _payload_uuid(payload, "candidate_id")
        expected_result_version = _positive_int(
            payload["expected_result_version"], "expected_result_version"
        )
        if head.current_candidate_id is None:
            raise ResultError("acceptance_candidate_required", {})
        if (
            candidate_id != head.current_candidate_id
            or expected_result_version != head.result_version
        ):
            raise ResultError(
                "acceptance_stale",
                {
                    "candidate_id": str(head.current_candidate_id),
                    "result_version": head.result_version,
                },
            )
        if head.current_acceptance_id is not None:
            raise ResultError(
                "acceptance_already_recorded",
                {"acceptance_id": str(head.current_acceptance_id)},
            )

        candidate = uow.results.get_record(
            "candidate", candidate_id, envelope.workspace_id
        )

        if not isinstance(candidate, IntegrationCandidateV1):
            raise ResultError("acceptance_candidate_required", {})
        if candidate.root_task_id != head.root_task_id:
            raise ResultError("acceptance_root_mismatch", {})
        if candidate.workspace_id != task.workspace_id:
            raise ResultError("acceptance_root_mismatch", {})
        candidate_hash = _hash_text(payload["candidate_hash"], "candidate_hash")
        if candidate_hash != candidate.content_hash:
            raise ResultError("acceptance_candidate_hash_mismatch", {})

        plan = uow.plans.get_current_for_root(head.root_task_id, envelope.workspace_id)
        if plan is None or plan.id != candidate.plan_snapshot_id:
            raise ResultError("acceptance_plan_stale", {})
        if candidate.plan_content_hash != plan_snapshot_content_hash(plan):
            raise ResultError("acceptance_plan_hash_mismatch", {})
        spec = plan.integration_spec
        if not isinstance(spec, IntegrationSpecV1):
            raise ResultError("integration_spec_required", {})

        decisions = _parse_decisions(payload["criteria"])
        expected_keys = {criterion.key for criterion in spec.criteria}
        decision_keys = {decision.criterion_key for decision in decisions}
        missing = sorted(expected_keys - decision_keys)
        if missing:
            raise ResultError("acceptance_criteria_missing", {"criterion_keys": missing})
        unknown = sorted(decision_keys - expected_keys)
        if unknown:
            raise ResultError("acceptance_criteria_unknown", {"criterion_keys": unknown})

        verification_results = uow.results.list_verification_results(
            candidate.id, envelope.workspace_id
        )
        try:
            evaluations = evaluate_criteria(spec.criteria, verification_results, decisions)
        except ValidationError as exc:
            if "accept_passed requires" in str(exc):
                raise ResultError("acceptance_verification_failed", {}) from exc
            raise ResultError("acceptance_decision_invalid", {"reason": str(exc)}) from exc
        evaluations_by_key = {evaluation.criterion_key: evaluation for evaluation in evaluations}
        for decision in decisions:
            evaluation = evaluations_by_key[decision.criterion_key]
            if (
                decision.decision
                in {
                    CriterionDecisionType.ACCEPT_PASSED,
                    CriterionDecisionType.MANUAL_PASSED,
                }
                and evaluation.observed_status is CriterionObservedStatus.FAILED
            ):
                raise ResultError("acceptance_verification_failed", {})

        submitted_basis_hash = _hash_text(
            payload["acceptance_basis_hash"], "acceptance_basis_hash"
        )
        actual_basis_hash = build_acceptance_basis_hash(
            candidate,
            verification_results,
            decisions,
            result_version=head.result_version,
            integration_spec=spec,
        )
        if submitted_basis_hash != actual_basis_hash:
            raise ResultError(
                "acceptance_basis_mismatch",
                {"expected": actual_basis_hash},
            )

        acceptance = TaskAcceptanceDecisionV1(
            id=uuid5(_ACCEPTANCE_NAMESPACE, f"command:{envelope.id}"),
            workspace_id=envelope.workspace_id,
            root_task_id=head.root_task_id,
            candidate_id=candidate.id,
            candidate_hash=candidate.content_hash,
            plan_content_hash=candidate.plan_content_hash,
            acceptance_basis_hash=actual_basis_hash,
            result_version=head.result_version,
            criteria=decisions,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            command_id=envelope.id,
            created_at=_aware_now(self._clock()),
        )
        stored = uow.results.add_immutable("acceptance", acceptance)
        if not isinstance(stored, TaskAcceptanceDecisionV1):
            raise ResultError("acceptance_record_conflict", {})

        task_events: list[EventData] = []
        previous_task_status = task.status.value
        previous_task_version = task.version
        from sidera_control_plane.domain.task import TaskStatus

        if task.status is not TaskStatus.WAITING:
            try:
                task.await_acceptance()
            except InvalidTransition as exc:
                raise ResultError("acceptance_task_not_ready", {}) from exc
            uow.tasks.save(task, previous_task_version)
            task_events.append(
                EventData(
                    id=uuid5(_ACCEPTANCE_NAMESPACE, f"task-wait:{envelope.id}"),
                    workspace_id=task.workspace_id,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    aggregate_version=task.version,
                    event_type="TaskWaitingForDecision",
                    payload={
                        "status": task.status.value,
                        "previous_status": previous_task_status,
                        "acceptance_id": str(stored.id),
                    },
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    causation_id=envelope.id,
                    correlation_id=envelope.correlation_id,
                )
            )

        next_head = replace(
            head,
            current_acceptance_id=stored.id,
            result_version=head.result_version + 1,
        )
        uow.results.save_head(next_head, expected_version=head.result_version)
        acceptance_event = EventData(
            id=uuid5(_ACCEPTANCE_NAMESPACE, f"record:{envelope.id}"),
            workspace_id=envelope.workspace_id,
            aggregate_type="result_acceptance",
            aggregate_id=head.root_task_id,
            aggregate_version=next_head.result_version,
            event_type="TaskAcceptanceRecorded",
            payload={
                "root_task_id": str(head.root_task_id),
                "acceptance_id": str(stored.id),
                "candidate_id": str(stored.candidate_id),
                "candidate_hash": stored.candidate_hash,
                "acceptance_basis_hash": stored.acceptance_basis_hash,
                "result_version": stored.result_version,
                "criteria": [item.to_mapping() for item in stored.criteria],
                "status": task.status.value,
                "previous_status": previous_task_status,
            },
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        for event in (*task_events, acceptance_event):
            uow.events.append_with_outbox(event)
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=task.id,
            aggregate_version=task.version,
            event_ids=tuple(event.id for event in (*task_events, acceptance_event)),
            data={
                "status": "accepted",
                "acceptance_id": str(stored.id),
                "candidate_id": str(stored.candidate_id),
                "result_version": stored.result_version,
                "task_status": task.status.value,
            },
        )


def build_acceptance_basis_hash(
    candidate: object,
    verification_results: Sequence[VerificationResultV1],
    criteria: Sequence[CriterionDecisionV1],
    *,
    result_version: int,
    integration_spec: IntegrationSpecV1,
) -> str:
    if not isinstance(candidate, IntegrationCandidateV1):
        raise TypeError("candidate must be an integration candidate")
    if type(result_version) is not int or result_version < 1:
        raise ValueError("result_version must be positive")
    payload = {
        "schema_version": 1,
        "candidate": candidate.to_mapping(),
        "verification_results": [
            result.to_mapping()
            for result in sorted(
                verification_results,
                key=lambda item: (item.check_key, item.attempt, str(item.id)),
            )
        ],
        "criteria": [
            decision.to_mapping()
            for decision in sorted(criteria, key=lambda item: item.criterion_key)
        ],
        "result_version": result_version,
        "integration_spec": integration_spec.to_mapping(),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_acceptance_contract(payload: Mapping[str, object]) -> Mapping[str, object]:
    fields = set(payload)
    missing = sorted(_ACCEPTANCE_PAYLOAD_FIELDS - fields)
    unknown = sorted(fields - _ACCEPTANCE_PAYLOAD_FIELDS)
    if missing or unknown:
        raise ResultError(
            "acceptance_contract_required",
            {"missing": missing, "unknown": unknown},
        )
    return payload


def _parse_decisions(value: object) -> tuple[CriterionDecisionV1, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ResultError("acceptance_criteria_invalid", {})
    if not value:
        raise ResultError("acceptance_criteria_missing", {})
    decisions: list[CriterionDecisionV1] = []
    for item in value:
        if isinstance(item, Mapping) and item.get("decision") == "exception_accepted":
            reason = item.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ResultError("acceptance_exception_reason_required", {})
        try:
            decisions.append(CriterionDecisionV1.from_mapping(item))
        except ValidationError as exc:
            if "exception acceptance requires" in str(exc):
                raise ResultError("acceptance_exception_reason_required", {}) from exc
            raise ResultError("acceptance_criteria_invalid", {"reason": str(exc)}) from exc
    if len({item.criterion_key for item in decisions}) != len(decisions):
        raise ResultError("acceptance_criteria_invalid", {"reason": "duplicate criterion"})
    return tuple(decisions)


def _payload_uuid(payload: Mapping[str, object], key: str) -> UUID:
    value = payload.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise ResultError("acceptance_contract_required", {"field": key})


def _hash_text(value: object, label: str) -> str:
    if not isinstance(value, str) or fullmatch(_SHA256_PATTERN, value) is None:
        raise ResultError("acceptance_contract_required", {"field": label})
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResultError("acceptance_contract_required", {"field": label})
    return value


def _aware_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("acceptance clock must return an aware datetime")
    return value.astimezone(UTC)


__all__ = ["ResultAcceptanceService", "build_acceptance_basis_hash"]
