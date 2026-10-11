from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError


class SchedulingBlockStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"


class SchedulingBlockReason(StrEnum):
    WAITING_CAPACITY = "waiting_capacity"
    NO_ELIGIBLE_WORKER = "no_eligible_worker"
    PINNED_WORKER_UNAVAILABLE = "pinned_worker_unavailable"
    RUNTIME_CONFIGURATION_MISSING = "runtime_configuration_missing"
    RUNTIME_CONFIGURATION_INVALID = "runtime_configuration_invalid"
    ADAPTER_MODULE_UNAVAILABLE = "adapter_module_unavailable"
    WORKSPACE_MODULE_UNAVAILABLE = "workspace_module_unavailable"
    RUNTIME_CAPABILITY_MISMATCH = "runtime_capability_mismatch"
    ADAPTER_POLICY_REJECTED = "adapter_policy_rejected"
    CONTEXT_ASSEMBLY_BLOCKED = "context_assembly_blocked"
    CONTEXT_ADAPTER_UNSUPPORTED = "context_adapter_unsupported"
    CONTEXT_PACKAGE_MISSING = "context_package_missing"
    CONTEXT_PACKAGE_PENDING = "context_package_pending"
    CONTEXT_PACKAGE_BLOCKED = "context_package_blocked"
    CONTEXT_PACKAGE_EXPIRED = "context_package_expired"
    CONTEXT_PACKAGE_REVOKED = "context_package_revoked"
    CONTEXT_PACKAGE_CORRUPT = "context_package_corrupt"
    CONTEXT_PACKAGE_STALE = "context_package_stale"
    RESULT_INPUT_UNAVAILABLE = "result_input_unavailable"
    RESULT_INPUT_INTEGRITY_FAILED = "result_input_integrity_failed"
    RESULT_INPUT_FORMAT_UNSUPPORTED = "result_input_format_unsupported"
    RESULT_INPUT_BUDGET_EXCEEDED = "result_input_budget_exceeded"
    HANDOFF_IN_PROGRESS = "handoff_in_progress"
    HANDOFF_TARGET_STALE = "handoff_target_stale"
    PLAN_TRANSITION_BLOCKED = "plan_transition_blocked"
    PLAN_GENERATION_STALE = "plan_generation_stale"


_SCHEDULING_REASON_LABELS = {
    "waiting_capacity": "Native capacity unavailable",
    "no_eligible_worker": "暂无可用执行节点",
    "pinned_worker_unavailable": "固定执行节点不可用",
    "runtime_configuration_missing": "运行环境未配置",
    "runtime_configuration_invalid": "运行环境配置无效",
    "adapter_module_unavailable": "适配器不可用",
    "workspace_module_unavailable": "工作区提供器不可用",
    "runtime_capability_mismatch": "运行能力不匹配",
    "adapter_policy_rejected": "适配器策略已过滤",
    "context_assembly_blocked": "上下文包组装被阻塞",
    "context_adapter_unsupported": "执行节点不支持上下文包",
    "context_package_missing": "上下文包不存在",
    "context_package_pending": "上下文包尚未准备好",
    "context_package_blocked": "上下文包组装失败",
    "context_package_expired": "上下文包已过期",
    "context_package_revoked": "上下文包已撤销",
    "context_package_corrupt": "上下文包已损坏",
    "context_package_stale": "上下文包与执行版本不一致",
    "result_input_unavailable": "固定结果内容暂不可用",
    "result_input_integrity_failed": "固定结果内容完整性失败",
    "result_input_format_unsupported": "固定结果内容格式不支持",
    "result_input_budget_exceeded": "固定结果内容超出预算",
    "handoff_in_progress": "执行正在进行 Worker 切换",
    "handoff_target_stale": "Worker 切换目标已失效",
    "plan_transition_blocked": "WorkerItem 正在进行计划切换",
    "plan_generation_stale": "执行请求属于旧计划代",
}


def scheduling_block_summary(
    reason_code: str | SchedulingBlockReason,
    reason_details: Mapping[str, object],
) -> str:
    """Create a safe, user-facing summary without exposing runtime configuration."""

    code = reason_code.value if isinstance(reason_code, SchedulingBlockReason) else str(reason_code)
    candidate_count = reason_details.get("candidate_count")
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool):
        candidate_count = None

    raw_counts = reason_details.get("candidate_issue_counts")
    counts: Mapping[str, object]
    if isinstance(raw_counts, Mapping):
        counts = {str(key): value for key, value in raw_counts.items()}
    else:
        counts = {
            key: reason_details[key]
            for key in _SCHEDULING_REASON_LABELS
            if key in reason_details
        }
    parts = [
        f"{_SCHEDULING_REASON_LABELS[key]} {value} 个"
        for key, value in sorted(counts.items(), key=lambda item: str(item[0]))
        if key in _SCHEDULING_REASON_LABELS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    ]
    if candidate_count is not None and parts:
        return f"{candidate_count} 个候选执行节点暂不可用：" + "，".join(parts)
    if candidate_count is not None and code == "runtime_configuration_missing":
        return f"{candidate_count} 个候选执行节点未配置运行环境"
    if candidate_count is not None:
        return f"{candidate_count} 个候选执行节点暂不可用"
    return _SCHEDULING_REASON_LABELS.get(code, "执行暂时无法分配")


@dataclass(frozen=True, slots=True)
class SchedulingIssue:
    reason_code: SchedulingBlockReason
    reason_details: Mapping[str, object]
    requires_operator: bool

    def __post_init__(self) -> None:
        try:
            reason_code = SchedulingBlockReason(self.reason_code)
        except (TypeError, ValueError) as exc:
            raise ValidationError("unsupported scheduling block reason") from exc
        if not isinstance(self.requires_operator, bool):
            raise ValidationError("requires_operator must be a boolean")
        if not isinstance(self.reason_details, Mapping):
            raise ValidationError("scheduling reason details must be a JSON object")
        frozen_details = _freeze_mapping(self.reason_details)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "reason_details", frozen_details)

    @property
    def fingerprint(self) -> str:
        return scheduling_fingerprint(
            self.reason_code,
            self.reason_details,
            requires_operator=self.requires_operator,
        )


@dataclass(slots=True)
class SchedulingBlock:
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    status: SchedulingBlockStatus
    reason_code: SchedulingBlockReason
    reason_details: Mapping[str, object]
    fingerprint: str
    requires_operator: bool
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    resolved_at: datetime | None
    version: int

    @classmethod
    def open(
        cls,
        *,
        block_id: UUID,
        workspace_id: UUID,
        execution_id: UUID,
        issue: SchedulingIssue,
        now: datetime,
    ) -> SchedulingBlock:
        if now.tzinfo is None:
            raise ValidationError("scheduling block time must be timezone-aware")
        return cls(
            id=block_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
            status=SchedulingBlockStatus.ACTIVE,
            reason_code=issue.reason_code,
            reason_details=issue.reason_details,
            fingerprint=issue.fingerprint,
            requires_operator=issue.requires_operator,
            first_seen_at=now,
            last_seen_at=now,
            occurrence_count=1,
            resolved_at=None,
            version=1,
        )

    def observe(self, *, issue: SchedulingIssue, now: datetime) -> None:
        self._require_active()
        if self.fingerprint != issue.fingerprint:
            raise ValidationError(
                "scheduling block fingerprint changed; close the episode first"
            )
        if now.tzinfo is None:
            raise ValidationError("scheduling block time must be timezone-aware")
        if now < self.last_seen_at:
            raise ValidationError("scheduling block time cannot move backwards")
        self.last_seen_at = now
        self.occurrence_count += 1
        self.version += 1

    def resolve(self, *, now: datetime) -> None:
        self._require_active()
        if now.tzinfo is None:
            raise ValidationError("scheduling block time must be timezone-aware")
        if now < self.last_seen_at:
            raise ValidationError("scheduling block time cannot move backwards")
        self.status = SchedulingBlockStatus.RESOLVED
        self.resolved_at = now
        self.version += 1

    def _require_active(self) -> None:
        if self.status is not SchedulingBlockStatus.ACTIVE:
            raise InvalidTransition("scheduling block episode is already resolved")


def scheduling_fingerprint(
    reason_code: SchedulingBlockReason,
    reason_details: Mapping[str, object],
    *,
    requires_operator: bool,
) -> str:
    try:
        canonical_reason = SchedulingBlockReason(reason_code).value
    except (TypeError, ValueError) as exc:
        raise ValidationError("unsupported scheduling block reason") from exc
    if not isinstance(requires_operator, bool):
        raise ValidationError("requires_operator must be a boolean")
    if not isinstance(reason_details, Mapping):
        raise ValidationError("scheduling reason details must be a JSON object")
    canonical = {
        "reason_code": canonical_reason,
        "reason_details": _json_ready(reason_details),
        "requires_operator": requires_operator,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("scheduling reason detail keys must be strings")
        frozen[key] = _freeze_value(item)
    return MappingProxyType(frozen)


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValidationError("scheduling reason details must contain JSON values")


def _json_ready(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValidationError("scheduling reason details must contain JSON values")
