from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .contracts import ProfileRef, RunStart, StartReceipt


@dataclass(frozen=True)
class OperationRecord:
    profile_ref: ProfileRef
    profile_config_hash: str
    fingerprint: str
    receipt: StartReceipt


@dataclass(frozen=True)
class AdmissionResult:
    receipt: StartReceipt
    created: bool


def request_fingerprint(request: RunStart, profile_config_hash: str) -> str:
    payload = request.model_dump(mode="json", warnings=False)
    payload["profile_config_hash"] = profile_config_hash
    return canonical_fingerprint(payload)


def canonical_fingerprint(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
