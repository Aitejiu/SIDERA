from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_decisions import (
    CollaborationDecisionService,
)
from sidera_control_plane.application.collaboration_governance_reconciler import (
    CollaborationGovernanceReconciler,
)
from sidera_control_plane.application.collaboration_reconciler import (
    CollaborationReconciler,
    PreparedCollaborationEvaluation,
)
from sidera_control_plane.application.collaboration_runtime_invoker import (
    CollaborationRuntimeInvoker,
    RuntimeInvocationFailure,
    RuntimeInvocationResult,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeResolver,
    RegistryCollaborationRuntimeFactory,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.collaboration import CollaborationEvaluationStatus
from sidera_control_plane.persistence.collaboration_fact_store import (
    SqlAlchemyCollaborationFactStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


class CollaborationReconcilerPort(Protocol):
    def prepare_next(
        self,
        *,
        owner_id: str,
        now: datetime,
    ) -> PreparedCollaborationEvaluation | None:
        raise NotImplementedError

    def complete_success(
        self,
        prepared: PreparedCollaborationEvaluation,
        result: RuntimeInvocationResult,
        *,
        now: datetime,
    ) -> bool:
        raise NotImplementedError

    def complete_decision(
        self,
        prepared: PreparedCollaborationEvaluation,
        result: RuntimeInvocationResult,
        *,
        now: datetime,
    ) -> Any:
        raise NotImplementedError

    def complete_failure(
        self,
        prepared: PreparedCollaborationEvaluation,
        failure: RuntimeInvocationFailure,
        *,
        now: datetime,
        max_attempts: int,
        backoff_seconds: int,
        backoff_max_seconds: int,
    ) -> Any:
        raise NotImplementedError


class CollaborationRuntimeInvokerPort(Protocol):
    def invoke(
        self,
        prepared: PreparedCollaborationEvaluation,
        *,
        now: datetime,
    ) -> RuntimeInvocationResult | RuntimeInvocationFailure:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class CollaborationWorkerRunSummary:
    evaluated: int = 0
    released: int = 0
    no_change: int = 0
    stale: int = 0
    rejected: int = 0
    duplicate: int = 0
    retried: int = 0
    blocked: int = 0
    failed: int = 0
    lease_lost: int = 0
    recovered: int = 0
    recovery_failed: int = 0
    recovery_failure_codes: tuple[str, ...] = ()

    def with_counts(self, **changes: Any) -> CollaborationWorkerRunSummary:
        values = {field: getattr(self, field) for field in self.__dataclass_fields__}
        values.update(changes)
        return CollaborationWorkerRunSummary(**values)


Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class CollaborationRecoverySweepResult:
    recovered: int = 0
    failed: int = 0
    failure_codes: tuple[str, ...] = ()


RecoverySweep = Callable[[datetime], int | CollaborationRecoverySweepResult | None]
Sleep = Callable[[float], None]


class CollaborationWorker:
    """Run collaboration evaluation phases with no database lease held in I/O."""

    def __init__(
        self,
        *,
        reconciler: CollaborationReconcilerPort,
        invoker: CollaborationRuntimeInvokerPort,
        owner_id: str,
        clock: Clock | None = None,
        max_attempts: int = 5,
        backoff_seconds: int = 2,
        backoff_max_seconds: int = 60,
        monotonic_clock: MonotonicClock | None = None,
        recovery_sweep: RecoverySweep | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if max_attempts < 1 or backoff_seconds < 1 or backoff_max_seconds < 1:
            raise ValueError("retry settings must be positive")
        self._reconciler = reconciler
        self._invoker = invoker
        self._owner_id = owner_id.strip()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._recovery_sweep = recovery_sweep

    def run_once(self, limit: int) -> CollaborationWorkerRunSummary:
        if limit < 1:
            raise ValueError("limit must be positive")
        summary = CollaborationWorkerRunSummary()
        while summary.evaluated < limit:
            now = self._clock()
            prepared = self._reconciler.prepare_next(owner_id=self._owner_id, now=now)
            if prepared is None:
                break
            summary = summary.with_counts(evaluated=summary.evaluated + 1)
            summary = self._evaluate(prepared, summary, now=now)
        return summary

    def run_forever(
        self,
        *,
        batch_size: int,
        poll_seconds: float,
        recovery_sweep_seconds: float,
        sleep: Sleep = time.sleep,
        on_summary: Callable[[CollaborationWorkerRunSummary], None] | None = None,
    ) -> None:
        if poll_seconds <= 0 or recovery_sweep_seconds <= 0:
            raise ValueError("poll and recovery sweep intervals must be positive")
        next_recovery = self._monotonic_clock()
        while True:
            now = self._clock()
            current = self._monotonic_clock()
            recovery_result = CollaborationRecoverySweepResult()
            if self._recovery_sweep is not None and current >= next_recovery:
                raw_result = self._recovery_sweep(now)
                if isinstance(raw_result, CollaborationRecoverySweepResult):
                    recovery_result = raw_result
                elif raw_result is not None:
                    recovery_result = CollaborationRecoverySweepResult(
                        recovered=int(raw_result)
                    )
                next_recovery = current + recovery_sweep_seconds
            summary = self.run_once(batch_size)
            if (
                recovery_result.recovered
                or recovery_result.failed
                or recovery_result.failure_codes
            ):
                summary = summary.with_counts(
                    recovered=recovery_result.recovered,
                    recovery_failed=recovery_result.failed,
                    recovery_failure_codes=recovery_result.failure_codes,
                )
            if on_summary is not None:
                on_summary(summary)
            sleep(poll_seconds)

    def _evaluate(
        self,
        prepared: PreparedCollaborationEvaluation,
        summary: CollaborationWorkerRunSummary,
        *,
        now: datetime,
    ) -> CollaborationWorkerRunSummary:
        try:
            result = self._invoker.invoke(prepared, now=now)
            if isinstance(result, RuntimeInvocationFailure):
                return self._record_failure(prepared, result, summary, now=now)
            if not self._reconciler.complete_success(prepared, result, now=now):
                return summary.with_counts(lease_lost=summary.lease_lost + 1)
            application = self._reconciler.complete_decision(
                prepared,
                result,
                now=self._clock(),
            )
            status = str(getattr(application, "status", "applied"))
            changes = {"released": len(getattr(application, "releases", ()))}
            if status == "no_change":
                changes["no_change"] = 1
            elif status == "stale":
                changes["stale"] = 1
            elif status == "rejected":
                changes["rejected"] = 1
            elif status == "duplicate":
                changes["duplicate"] = 1
            return summary.with_counts(
                **{
                    field: getattr(summary, field) + value
                    for field, value in changes.items()
                }
            )
        except Exception:  # noqa: BLE001
            failure = RuntimeInvocationFailure(
                code="worker_phase_failed",
                response_hash=None,
                diagnostic_summary="collaboration worker phase failed",
                retryable=True,
                duration_ms=0,
            )
            return self._record_failure(prepared, failure, summary, now=self._clock())

    def _record_failure(
        self,
        prepared: PreparedCollaborationEvaluation,
        failure: RuntimeInvocationFailure,
        summary: CollaborationWorkerRunSummary,
        *,
        now: datetime,
    ) -> CollaborationWorkerRunSummary:
        try:
            evaluation = self._reconciler.complete_failure(
                prepared,
                failure,
                now=now,
                max_attempts=self._max_attempts,
                backoff_seconds=self._backoff_seconds,
                backoff_max_seconds=self._backoff_max_seconds,
            )
        except Exception:  # noqa: BLE001
            return summary.with_counts(failed=summary.failed + 1)
        status = getattr(evaluation, "status", None)
        status_value = getattr(status, "value", status)
        return summary.with_counts(
            failed=summary.failed + 1,
            retried=summary.retried
            + int(
                status_value in {"retry_wait", CollaborationEvaluationStatus.RETRY_WAIT}
            ),
            blocked=summary.blocked
            + int(status_value in {"blocked", CollaborationEvaluationStatus.BLOCKED}),
        )


@dataclass(slots=True)
class CollaborationWorkerRuntime:
    worker: CollaborationWorker
    database: Database


def build_collaboration_worker(settings: Settings) -> CollaborationWorkerRuntime:
    database = Database(settings.database_url)
    uow_factory: Callable[[], SqlAlchemyUnitOfWork] = lambda: SqlAlchemyUnitOfWork(
        database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
    )
    runtime_factory = RegistryCollaborationRuntimeFactory(
        {"reference": lambda _instance: ReferenceCollaborationRuntime()}
    )
    resolver_factory = lambda modules: CollaborationRuntimeResolver(
        modules, runtime_factory
    )
    owner_id = f"collaboration-worker:{settings.local_actor_id}"
    governance_reconciler = CollaborationGovernanceReconciler(uow_factory=uow_factory)
    try:
        decision_service = CollaborationDecisionService(
            uow_factory=uow_factory,
            fact_store_factory=_fact_store,
            owner_id=owner_id,
        )
        reconciler = CollaborationReconciler(
            uow_factory=uow_factory,
            fact_store_factory=_fact_store,
            runtime_resolver_factory=resolver_factory,
            decision_service=decision_service,
            governance_reconciler=governance_reconciler,
            evaluation_lease_ttl=timedelta(
                seconds=settings.collaboration_lease_ttl_seconds
            ),
            recovery_limit=settings.collaboration_batch_size,
        )
        invoker = CollaborationRuntimeInvoker(
            runtime_resolver_factory=resolver_factory,
            resolution_uow_factory=uow_factory,
            evaluation_timeout=timedelta(
                seconds=settings.collaboration_evaluation_timeout_seconds
            ),
            max_projection_bytes=settings.collaboration_max_projection_bytes,
            max_decision_bytes=settings.collaboration_max_decision_bytes,
            max_intents=settings.collaboration_max_intents_per_decision,
            max_strategy_state_bytes=settings.collaboration_max_strategy_state_bytes,
            max_diagnostic_bytes=settings.collaboration_max_diagnostic_bytes,
        )

        def recovery_sweep(
            now: datetime,
        ) -> CollaborationRecoverySweepResult:
            with uow_factory() as uow:
                evaluation_recovered = len(
                    uow.collaboration_evaluations.recover_expired(
                        now=now,
                        limit=settings.collaboration_batch_size,
                    )
                )
            governance = governance_reconciler.recover(
                None,
                now=now,
                actor_id=owner_id,
                correlation_id=uuid4(),
                limit=settings.collaboration_batch_size,
            )
            return CollaborationRecoverySweepResult(
                recovered=evaluation_recovered + governance.repaired,
                failed=governance.failed,
                failure_codes=governance.failure_codes,
            )

        return CollaborationWorkerRuntime(
            worker=CollaborationWorker(
                reconciler=reconciler,
                invoker=invoker,
                owner_id=owner_id,
                max_attempts=settings.collaboration_max_attempts,
                backoff_seconds=settings.collaboration_backoff_seconds,
                backoff_max_seconds=settings.collaboration_backoff_max_seconds,
                recovery_sweep=recovery_sweep,
            ),
            database=database,
        )
    except BaseException:
        database.dispose()
        raise


def summary_json(summary: CollaborationWorkerRunSummary) -> str:
    return json.dumps(
        {
            "component": "collaboration-worker",
            "status": "ok",
            "evaluated": summary.evaluated,
            "released": summary.released,
            "no_change": summary.no_change,
            "stale": summary.stale,
            "rejected": summary.rejected,
            "duplicate": summary.duplicate,
            "retried": summary.retried,
            "blocked": summary.blocked,
            "failed": summary.failed,
            "lease_lost": summary.lease_lost,
            "recovered": summary.recovered,
            "recovery_failed": summary.recovery_failed,
            "recovery_failure_codes": list(summary.recovery_failure_codes),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIDERA Collaboration Worker")
    parser.add_argument("--once", action="store_true", help="run one batch and exit")
    args = parser.parse_args(argv)
    runtime: CollaborationWorkerRuntime | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        runtime = build_collaboration_worker(settings)
        if args.once:
            print(
                summary_json(runtime.worker.run_once(settings.collaboration_batch_size))
            )
            return 0
        runtime.worker.run_forever(
            batch_size=settings.collaboration_batch_size,
            poll_seconds=settings.collaboration_poll_seconds,
            recovery_sweep_seconds=settings.collaboration_recovery_sweep_seconds,
            on_summary=lambda summary: print(summary_json(summary)),
        )
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "collaboration-worker",
                    "status": "error",
                    "error": f"{type(exc).__name__}: collaboration worker failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if runtime is not None:
            runtime.database.dispose()


def _fact_store(uow: ControlPlaneUnitOfWork) -> SqlAlchemyCollaborationFactStore:
    session = getattr(uow, "session", None)
    if session is None:
        raise RuntimeError("the collaboration worker requires a SQLAlchemy session")
    return SqlAlchemyCollaborationFactStore(session=session)


if __name__ == "__main__":
    raise SystemExit(main())
