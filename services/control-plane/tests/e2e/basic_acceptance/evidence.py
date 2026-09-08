"""Atomic, bounded evidence records for staged acceptance scenarios."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from .contracts import EvidenceKind, ScenarioEvidence


class EvidenceBuilder:
    def __init__(
        self,
        *,
        run_id: UUID,
        scenario: str,
        evidence_kind: EvidenceKind,
        code_commit: str,
        runtime_fingerprint: str,
    ) -> None:
        self._base = {
            "run_id": run_id,
            "scenario": scenario,
            "evidence_kind": evidence_kind,
            "code_commit": code_commit,
            "runtime_fingerprint": runtime_fingerprint,
        }

    def finish(
        self,
        *,
        real_tests: int,
        skipped: int = 0,
        failed: int = 0,
        incomplete: bool = False,
        check_codes: tuple[str, ...] = (),
        command_ids: tuple[UUID, ...] = (),
        execution_ids: tuple[UUID, ...] = (),
        session_ids: tuple[UUID, ...] = (),
        root_task_id: UUID | None = None,
        failure_code_override: str | None = None,
    ) -> ScenarioEvidence:
        if failed:
            status = "FAIL"
            failure_code = "scenario_assertion_failed"
        elif incomplete:
            status = "INCOMPLETE"
            failure_code = "scenario_incomplete"
        elif real_tests <= 0 or skipped:
            status = "NOT_RUN"
            failure_code = "real_tests_not_observed"
        else:
            status = "PASS"
            failure_code = None
        return ScenarioEvidence(
            **self._base,
            status=status,
            check_codes=check_codes,
            command_ids=command_ids,
            execution_ids=execution_ids,
            session_ids=session_ids,
            root_task_id=root_task_id,
            failure_code=failure_code_override or failure_code,
        )


def write_evidence(path: Path, evidence: ScenarioEvidence) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(evidence.model_dump(mode="json"), handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_evidence(path: Path) -> ScenarioEvidence:
    return ScenarioEvidence.model_validate(json.loads(path.read_text(encoding="utf-8")))
