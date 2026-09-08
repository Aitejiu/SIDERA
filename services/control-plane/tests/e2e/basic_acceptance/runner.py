"""Scenario registry; no arbitrary commands or database fixtures are accepted."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from .contracts import ScenarioEvidence
from .evidence import EvidenceBuilder

SCENARIOS = frozenset(
    {
        "profile",
        "result-inputs",
        "result-integration",
        "user-flow",
        "intervention",
        "revision",
        "recovery",
        "second-adapter",
        "notification",
    }
)


def run_scenario(
    name: str,
    manifest_path: Path,
    *,
    mode: Literal["check", "local", "real"] = "check",
) -> ScenarioEvidence:
    if name not in SCENARIOS:
        raise ValueError(f"unsupported scenario: {name}")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = UUID(str(raw["run_id"]))
    builder = EvidenceBuilder(
        run_id=run_id,
        scenario=name,
        evidence_kind="contract" if mode != "real" else "real_user_flow",
        code_commit=_code_commit(raw),
        runtime_fingerprint=str(raw.get("runtime_fingerprint", "0" * 64)),
    )
    if mode == "check":
        return builder.finish(
            real_tests=0,
            check_codes=("manifest_loaded", "check_mode_no_business_session"),
            failure_code_override="check_mode_no_business_session",
        )
    return builder.finish(
        real_tests=0,
        incomplete=True,
        check_codes=("scenario_handler_not_enabled",),
    )


def _code_commit(manifest: dict[str, object]) -> str:
    value = manifest.get("code_commit")
    if isinstance(value, str) and len(value) == 40:
        return value
    return "0" * 40
