from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CheckStatus = Literal["NOT_RUN", "PASS", "FAIL", "INCOMPLETE"]
EvidenceKind = Literal["simulated_interview", "real_local_verification"]
Readiness = Literal["NOT_READY", "MAINLINE_READY", "INTERVIEW_DEMO_READY"]

OVERALL_REQUIRED_IDS = ("M1", "M2", "M3", "M4", "M5", "M6")


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    status: CheckStatus
    evidence_kind: EvidenceKind
    evidence_paths: tuple[str, ...] = ()
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("check_id_required")
        if self.status not in {"NOT_RUN", "PASS", "FAIL", "INCOMPLETE"}:
            raise ValueError("check_status_invalid")
        if self.evidence_kind not in {"simulated_interview", "real_local_verification"}:
            raise ValueError("evidence_kind_invalid")
        if any(not path.strip() for path in self.evidence_paths):
            raise ValueError("evidence_path_invalid")
        if self.status == "PASS" and not self.evidence_paths:
            raise ValueError("passing_check_requires_evidence")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "evidence_kind": self.evidence_kind,
            "evidence_paths": list(self.evidence_paths),
            "failure_code": self.failure_code,
        }


def evaluate_readiness(
    results: tuple[CheckResult, ...], required_ids: tuple[str, ...]
) -> Readiness:
    """Evaluate only observed, evidenced checks; never infer coverage."""
    required = tuple(required_ids)
    if not required or len(set(required)) != len(required):
        return "NOT_READY"
    if any(not item.strip() for item in required):
        return "NOT_READY"
    observed: dict[str, list[CheckResult]] = {}
    for result in results:
        observed.setdefault(result.id, []).append(result)
    for required_id in required:
        candidates = observed.get(required_id, [])
        if len(candidates) != 1 or candidates[0].status != "PASS":
            return "NOT_READY"
        if any(not _evidence_path_is_file(path) for path in candidates[0].evidence_paths):
            return "NOT_READY"
    if any(len(items) > 1 for items in observed.values()):
        return "NOT_READY"
    required_set = set(required)
    if set(OVERALL_REQUIRED_IDS).issubset(required_set):
        action_ids = {item for item in required_set if item.startswith("F")}
        if action_ids:
            return "INTERVIEW_DEMO_READY"
        return "MAINLINE_READY"
    return "MAINLINE_READY"


def evidence_path_is_valid(path: str, roots: tuple[Path, ...]) -> bool:
    """Check an evidence path without following it outside registered roots."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False
    return any(_is_within(resolved, root.resolve(strict=False)) for root in roots)


def _evidence_path_is_file(path: str) -> bool:
    candidate = Path(path).expanduser()
    return candidate.is_absolute() and candidate.is_file() and not candidate.is_symlink()


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
