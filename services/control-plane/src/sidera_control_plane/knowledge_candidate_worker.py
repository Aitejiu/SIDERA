from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.adapters.knowledge_http import (
    KnowledgeAdapterError,
    KnowledgeHttpAdapter,
)
from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.knowledge_candidate_submission import (
    CandidateProposalSpool,
    CandidateProposalSpoolError,
    FileCandidateProposalSpool,
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSourceSuggestionV1,
    KnowledgeCandidateSubmission,
    KnowledgeCandidateSubmissionService,
    KnowledgeCandidateSubmissionStatus,
)
from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    KnowledgeCandidateCatalogPort,
    KnowledgeCommandStatus,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork

logger = logging.getLogger(__name__)

_COMMAND_NAMESPACE = NAMESPACE_URL
_QUARANTINE_RETENTION = timedelta(hours=24)
_DEFAULT_RETRY_DELAY = timedelta(seconds=15)
_SAFE_ERROR = re.compile(r"[^a-z0-9_.:-]+")


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateWorkerResult:
    claimed: int
    submitted: int
    retried: int
    rejected: int
    discarded: int
    failed: int


class _PermanentCandidateError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class KnowledgeCandidateSubmissionWorker:
    """Submit durable candidate references without making them task gates."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        catalog: KnowledgeCandidateCatalogPort,
        spool: CandidateProposalSpool,
        owner_id: str = "knowledge-candidate-worker-local",
        lease_seconds: int = 60,
        retry_delay_seconds: int = 15,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if lease_seconds < 1 or retry_delay_seconds < 1:
            raise ValueError("candidate worker timing limits are invalid")
        self._uow_factory = uow_factory
        self._catalog = catalog
        self._spool = spool
        self._owner_id = owner_id.strip()
        self._lease_seconds = lease_seconds
        self._retry_delay = timedelta(seconds=retry_delay_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._events = KnowledgeCandidateSubmissionService(spool)

    def run_once(self, limit: int = 10) -> KnowledgeCandidateWorkerResult:
        if limit < 1:
            raise ValueError("candidate worker limit must be positive")
        now = self._clock()
        self._gc_quarantine(now)
        claimed = self._claim(limit, now)
        submitted = 0
        retried = 0
        rejected = 0
        discarded = 0
        failed = 0
        for submission in claimed:
            try:
                disposition = self._process(submission)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error(
                    "knowledge_candidate_submission_failed",
                    extra={
                        "workspace_id": str(submission.workspace_id),
                        "submission_id": str(submission.id),
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            submitted += int(disposition == "submitted")
            retried += int(disposition == "retry")
            rejected += int(disposition == "rejected")
            discarded += int(disposition == "discarded")
        return KnowledgeCandidateWorkerResult(
            claimed=len(claimed),
            submitted=submitted,
            retried=retried,
            rejected=rejected,
            discarded=discarded,
            failed=failed,
        )

    def _claim(
        self, limit: int, now: datetime
    ) -> tuple[KnowledgeCandidateSubmission, ...]:
        with self._uow_factory() as uow:
            return uow.knowledge_candidate_submissions.claim_next(
                owner=self._owner_id,
                now=now,
                lease_ttl=timedelta(seconds=self._lease_seconds),
                limit=limit,
            )

    def _process(self, submission: KnowledgeCandidateSubmission) -> str:
        command_id = uuid5(
            _COMMAND_NAMESPACE,
            f"sidera.knowledge-candidate-command:{submission.id}",
        )
        try:
            proposal = self._spool.read(
                submission.content_ref, expected_hash=submission.proposal_hash
            )
        except CandidateProposalSpoolError:
            changed = self._reject(
                submission,
                command_id=command_id,
                error_code="candidate_spool_invalid",
                quarantine=True,
            )
            return "rejected" if changed else "failed"
        try:
            candidate_id = _candidate_id(submission)
            status = self._probe_command_status(submission, command_id)
            disposition = self._status_disposition(
                submission, status, candidate_id, command_id
            )
            if disposition is not None:
                return disposition
            payload = _candidate_payload(proposal, submission, candidate_id)
            status = self._catalog.submit_candidate(
                submission.workspace_id,
                command_id,
                f"sidera.knowledge-candidate:{submission.id}",
                payload,
                correlation_id=submission.id,
                caller_request_id=submission.id,
            )
            disposition = self._status_disposition(
                submission, status, candidate_id, command_id
            )
            if disposition is not None:
                return disposition
            return self._retry(
                submission,
                command_id=command_id,
                error_code="knowledge_command_pending",
            )
        except _PermanentCandidateError as exc:
            changed = self._reject(
                submission,
                command_id=command_id,
                error_code=exc.code,
                quarantine=True,
            )
            return "rejected" if changed else "failed"
        except KnowledgeAdapterError as exc:
            if exc.temporary:
                return self._retry(
                    submission,
                    command_id=command_id,
                    error_code=_safe_error_code(exc.code),
                )
            changed = self._reject(
                submission,
                command_id=command_id,
                error_code=_safe_error_code(exc.code),
                quarantine=True,
            )
            return "rejected" if changed else "failed"
        except (TypeError, ValueError):
            changed = self._reject(
                submission,
                command_id=command_id,
                error_code="candidate_payload_invalid",
                quarantine=True,
            )
            if not changed:
                logger.warning(
                    "knowledge_candidate_submission_ownership_lost",
                    extra={"submission_id": str(submission.id)},
                )
            return "rejected" if changed else "failed"

    def _status_disposition(
        self,
        submission: KnowledgeCandidateSubmission,
        status: KnowledgeCommandStatus,
        candidate_id: UUID,
        command_id: UUID,
    ) -> str | None:
        if status.command_id != command_id or status.workspace_id != submission.workspace_id:
            raise _PermanentCandidateError("knowledge_command_scope_mismatch")
        if status.status == "pending":
            return None
        if status.status == "rejected":
            changed = self._reject(
                submission,
                command_id=command_id,
                error_code=_safe_error_code(status.error_code or "knowledge_command_rejected"),
                quarantine=True,
            )
            return "rejected" if changed else "failed"
        observed = _result_candidate_id(status.result)
        if observed != candidate_id:
            raise _PermanentCandidateError("knowledge_candidate_id_mismatch")
        changed = self._submit(
            submission,
            command_id=command_id,
            candidate_id=candidate_id,
        )
        if changed:
            self._delete_after_terminal(submission)
        return "submitted" if changed else "failed"

    def _probe_command_status(
        self, submission: KnowledgeCandidateSubmission, command_id: UUID
    ) -> KnowledgeCommandStatus:
        try:
            return self._catalog.get_command_status(
                submission.workspace_id,
                command_id,
                correlation_id=submission.id,
                caller_request_id=submission.id,
            )
        except KnowledgeAdapterError as exc:
            if exc.status_code != 404:
                raise
            # A first delivery has no remote command yet; proceed with the
            # deterministic command ID so the submit call remains idempotent.
            return KnowledgeCommandStatus(
                command_id=command_id,
                workspace_id=submission.workspace_id,
                status="pending",
                result=None,
                error_code=None,
            )

    def _submit(
        self,
        submission: KnowledgeCandidateSubmission,
        *,
        command_id: UUID,
        candidate_id: UUID,
    ) -> bool:
        now = self._clock()
        with self._uow_factory() as uow:
            current = uow.knowledge_candidate_submissions.get(
                submission.id, submission.workspace_id, for_update=True
            )
            if not self._owned(current, submission):
                return False
            assert current is not None
            previous_version = current.version
            self._ensure_dispatch_command(uow, current, command_id)
            current.mark_submitted(
                now=now, command_id=command_id, candidate_id=candidate_id
            )
            uow.knowledge_candidate_submissions.save(current, previous_version)
            self._events.record_event(
                uow,
                submission=current,
                event_type="KnowledgeCandidateSubmissionUpdated",
                actor_id=self._owner_id,
                correlation_id=submission.id,
            )
        return True

    def _retry(
        self,
        submission: KnowledgeCandidateSubmission,
        *,
        command_id: UUID,
        error_code: str,
    ) -> str:
        now = self._clock()
        with self._uow_factory() as uow:
            current = uow.knowledge_candidate_submissions.get(
                submission.id, submission.workspace_id, for_update=True
            )
            if not self._owned(current, submission):
                return "failed"
            assert current is not None
            previous_version = current.version
            current.retry(
                now=now,
                next_attempt_at=now + self._retry_delay,
                error_code=error_code,
            )
            self._ensure_dispatch_command(uow, current, command_id)
            uow.knowledge_candidate_submissions.save(current, previous_version)
            self._events.record_event(
                uow,
                submission=current,
                event_type="KnowledgeCandidateSubmissionUpdated",
                actor_id=self._owner_id,
                correlation_id=submission.id,
            )
        return "retry"

    def _reject(
        self,
        submission: KnowledgeCandidateSubmission,
        *,
        command_id: UUID,
        error_code: str,
        quarantine: bool,
    ) -> bool:
        now = self._clock()
        with self._uow_factory() as uow:
            current = uow.knowledge_candidate_submissions.get(
                submission.id, submission.workspace_id, for_update=True
            )
            if not self._owned(current, submission):
                return False
            assert current is not None
            previous_version = current.version
            self._ensure_dispatch_command(uow, current, command_id)
            current.mark_rejected(
                now=now,
                command_id=command_id,
                error_code=_safe_error_code(error_code),
            )
            uow.knowledge_candidate_submissions.save(current, previous_version)
            self._events.record_event(
                uow,
                submission=current,
                event_type="KnowledgeCandidateSubmissionUpdated",
                actor_id=self._owner_id,
                correlation_id=submission.id,
            )
        if quarantine:
            self._quarantine_after_terminal(submission)
        return True

    @staticmethod
    def _ensure_dispatch_command(
        uow: ControlPlaneUnitOfWork,
        submission: KnowledgeCandidateSubmission,
        command_id: UUID,
    ) -> None:
        if submission.knowledge_command_id is not None:
            if submission.knowledge_command_id != command_id:
                raise _PermanentCandidateError("knowledge_command_scope_mismatch")
            return
        envelope = CommandEnvelope(
            id=command_id,
            workspace_id=submission.workspace_id,
            command_type="KnowledgeCandidateSubmissionDispatch",
            target_type="knowledge_candidate_submission",
            actor_type="system",
            actor_id="knowledge-candidate-worker",
            target_id=submission.id,
            payload={
                "submission_id": str(submission.id),
                "proposal_hash": submission.proposal_hash,
            },
            expected_version=None,
            idempotency_key=f"knowledge-candidate-dispatch:{submission.id}",
            correlation_id=submission.id,
        )
        existing = uow.commands.find_by_id(submission.workspace_id, command_id)
        if existing is not None:
            if (
                existing.target_type != envelope.target_type
                or existing.request_hash != hash_request(envelope)
            ):
                raise _PermanentCandidateError("knowledge_command_scope_mismatch")
            submission.knowledge_command_id = command_id
            return
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=command_id,
                aggregate_id=submission.id,
                aggregate_version=submission.version,
                event_ids=(),
                data={"submission_id": str(submission.id)},
            ),
        )
        submission.knowledge_command_id = command_id

    @staticmethod
    def _owned(
        current: KnowledgeCandidateSubmission | None,
        claimed: KnowledgeCandidateSubmission,
    ) -> bool:
        return bool(
            current is not None
            and current.status is KnowledgeCandidateSubmissionStatus.SUBMITTING
            and current.lease_owner == claimed.lease_owner
            and current.lease_owner is not None
        )

    def _delete_after_terminal(self, submission: KnowledgeCandidateSubmission) -> None:
        try:
            self._spool.delete(submission.content_ref)
        except CandidateProposalSpoolError:
            logger.warning(
                "knowledge_candidate_spool_cleanup_failed",
                extra={"submission_id": str(submission.id)},
            )

    def _quarantine_after_terminal(
        self, submission: KnowledgeCandidateSubmission
    ) -> None:
        try:
            quarantine = getattr(self._spool, "quarantine", None)
            if callable(quarantine):
                quarantine(submission.content_ref, now=self._clock())
            else:
                self._spool.delete(submission.content_ref)
        except (CandidateProposalSpoolError, AttributeError):
            logger.warning(
                "knowledge_candidate_spool_quarantine_failed",
                extra={"submission_id": str(submission.id)},
            )

    def _gc_quarantine(self, now: datetime) -> None:
        try:
            gc_quarantine = getattr(self._spool, "gc_quarantine", None)
            if callable(gc_quarantine):
                gc_quarantine(now=now, retention=_QUARANTINE_RETENTION)
        except (CandidateProposalSpoolError, AttributeError):
            logger.warning("knowledge_candidate_quarantine_gc_failed")


def _candidate_id(submission: KnowledgeCandidateSubmission) -> UUID:
    return uuid5(
        _COMMAND_NAMESPACE,
        f"sidera.knowledge-candidate:{submission.producer_type}:{submission.producer_id}:{submission.proposal_hash}",
    )


def _candidate_payload(
    proposal: KnowledgeCandidateProposalV1,
    submission: KnowledgeCandidateSubmission,
    candidate_id: UUID,
) -> dict[str, object]:
    sources = [_source_payload(item, candidate_id) for item in proposal.source_suggestions]
    sources.extend(
        _reference_source(candidate_id, "artifact", value)
        for value in proposal.artifact_refs
    )
    sources.extend(
        _reference_source(candidate_id, "evidence", value)
        for value in proposal.evidence_refs
    )
    provenance: dict[str, object] = {
        "producer_type": submission.producer_type,
        "producer_id": str(submission.producer_id),
    }
    for key, value in (
        ("task_id", submission.task_id),
        ("subtask_id", submission.subtask_task_id),
        ("worker_item_id", submission.worker_item_id),
        ("execution_id", submission.execution_id),
    ):
        if value is not None:
            provenance[key] = str(value)
    return {
        "candidate_schema_version": 1,
        "candidate_id": str(candidate_id),
        "operation": proposal.operation,
        "target_page_id": (
            str(proposal.target_page_id) if proposal.target_page_id is not None else None
        ),
        "expected_revision_id": (
            str(proposal.expected_revision_id)
            if proposal.expected_revision_id is not None
            else None
        ),
        "expected_revision_hash": proposal.expected_revision_hash,
        "title": proposal.title,
        "slug": proposal.slug,
        "summary": proposal.summary,
        "tags": list(proposal.tags),
        "sections": [item.to_mapping() for item in proposal.sections],
        "relations": [item.to_mapping() for item in proposal.relations],
        "sources": sources,
        "provenance": provenance,
        "supersedes_candidate_id": (
            str(proposal.supersedes_candidate_id)
            if proposal.supersedes_candidate_id is not None
            else None
        ),
    }


def _source_payload(
    source: object, _candidate_id: UUID
) -> dict[str, object]:
    # The proposal type validates the shape; keeping conversion here limits the
    # Knowledge-specific source vocabulary to the submission boundary.
    if not isinstance(source, KnowledgeCandidateSourceSuggestionV1):
        raise _PermanentCandidateError("candidate_source_invalid")
    allowed = {"manual", "markdown_file", "task", "artifact", "evidence", "external_url"}
    source_type = source.source_type if source.source_type in allowed else "manual"
    metadata = dict(source.metadata)
    if source_type == "task" and not {
        "task_id",
        "subtask_id",
    }.issubset(metadata):
        source_type = "manual"
    if source_type in {"artifact", "evidence"}:
        key = f"{source_type}_id"
        if _as_uuid(metadata.get(key)) is None:
            source_type = "manual"
    return {
        "source_id": str(source.source_id) if source.source_id is not None else None,
        "source_type": source_type,
        "label": source.label,
        "uri": source.uri,
        "workspace_relative_path": source.workspace_relative_path,
        "git_revision": source.git_revision,
        "digest": source.digest,
        "metadata": metadata,
    }


def _reference_source(
    candidate_id: UUID, source_type: Literal["artifact", "evidence"], reference: str
) -> dict[str, object]:
    parsed = _as_uuid(reference)
    metadata: dict[str, object] = {f"{source_type}_ref": reference}
    if parsed is not None:
        metadata[f"{source_type}_id"] = str(parsed)
    else:
        # Opaque refs are retained as non-operational manual suggestions; the
        # Knowledge Service only accepts UUID operational provenance.
        source_type = "manual"  # type: ignore[assignment]
    return {
        "source_id": str(
            uuid5(_COMMAND_NAMESPACE, f"sidera.candidate-source:{candidate_id}:{reference}")
        ),
        "source_type": source_type,
        "label": f"{source_type}:{reference}",
        "uri": None,
        "workspace_relative_path": None,
        "git_revision": None,
        "digest": None,
        "metadata": metadata,
    }


def _result_candidate_id(result: Mapping[str, object] | None) -> UUID | None:
    if result is None:
        return None
    return _as_uuid(result.get("candidate_id"))


def _as_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _safe_error_code(value: object) -> str:
    normalized = _SAFE_ERROR.sub("_", str(value).strip().lower()).strip("_")
    return (normalized or "knowledge_candidate_worker_error")[:100]


def summary_json(result: KnowledgeCandidateWorkerResult) -> str:
    return json.dumps(
        {
            "component": "knowledge-candidate-worker",
            "status": "ok",
            "claimed": result.claimed,
            "submitted": result.submitted,
            "retried": result.retried,
            "rejected": result.rejected,
            "discarded": result.discarded,
            "failed": result.failed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(slots=True)
class KnowledgeCandidateWorkerRuntime:
    worker: KnowledgeCandidateSubmissionWorker
    database: Database
    catalog: KnowledgeHttpAdapter


def build_knowledge_candidate_worker(
    settings: Settings,
    *,
    owner_id: str | None = None,
) -> KnowledgeCandidateWorkerRuntime:
    database = Database(settings.database_url)
    catalog = KnowledgeHttpAdapter(
        base_url=settings.knowledge_base_url,
        control_plane_token=settings.knowledge_control_plane_token,
        metadata_timeout_seconds=settings.knowledge_metadata_timeout_seconds,
        materialization_timeout_seconds=settings.knowledge_materialization_timeout_seconds,
    )
    try:
        spool = FileCandidateProposalSpool(
            settings.knowledge_candidate_spool_dir,
            quota_bytes=settings.knowledge_candidate_spool_quota_bytes,
        )
        uow_factory = lambda: SqlAlchemyUnitOfWork(
            database,
            projection_change_retention_seconds=settings.projection_change_retention_seconds,
        )
        worker = KnowledgeCandidateSubmissionWorker(
            uow_factory=uow_factory,
            catalog=catalog,
            spool=spool,
            owner_id=owner_id or f"knowledge-candidate-worker:{settings.local_actor_id}",
            lease_seconds=settings.context_assembly_lease_seconds,
        )
    except BaseException:
        catalog.close()
        database.dispose()
        raise
    return KnowledgeCandidateWorkerRuntime(worker=worker, database=database, catalog=catalog)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the SIDERA Knowledge Candidate Submission Worker"
    )
    parser.add_argument("--owner-id", default="knowledge-candidate-worker-local")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0 or args.batch_size < 1:
        parser.error("poll and batch limits must be positive")
    runtime: KnowledgeCandidateWorkerRuntime | None = None
    stop_event = Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_event.set())
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_knowledge_candidate_worker(settings, owner_id=args.owner_id)
        while not stop_event.is_set():
            result = runtime.worker.run_once(args.batch_size)
            print(summary_json(result), flush=True)
            if args.once:
                return 0
            stop_event.wait(args.poll_seconds)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "knowledge-candidate-worker",
                    "status": "error",
                    "error": "knowledge candidate worker failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if runtime is not None:
            runtime.catalog.close()
            runtime.database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
